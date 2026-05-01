"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date updated: Jan 2026

Description: Module to fetch 3DEP DEM data based on user-defined boundary and region.
It also processes local DEM files if provided or outside CONUS regions. 
Everything from resolution to CRS can be specified at initialization and runs automatically.
"""
import logging
from typing import Union, Tuple, Optional
from pathlib import Path
import xarray as xr
import rioxarray
import geopandas as gpd
import numpy as np
from shapely.geometry import box, Polygon, MultiPolygon
from rasterio.enums import Resampling 

class DEMProcessor:
    def __init__(
        self,
        boundary: Union[str, gpd.GeoDataFrame],
        layer: Optional[str] = None,
        output_dir: str = "./dem_output",
        out_name: Optional[str] = None,
        dem_file: Optional[str] = None,
        resolution: int = 10,
        epsg: Optional[int] = None
    ):
        self.boundary_input = boundary
        self.layer = layer
        self.dem_file = dem_file
        self.resolution = resolution
        
        self.out_name = out_name
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Setup Logger - Uses "DEMProcessor" name to link to pipeline logger
        self.logger = logging.getLogger("DEMProcessor")
        
        # Load boundary and ensure it is in WGS84
        self.gdf_wgs84 = self._load_gdf().to_crs(epsg=4326)
        
        # Warning Fix: Use union_all() instead of unary_union for newer GeoPandas versions
        self.boundary_geom = self.gdf_wgs84.union_all()
        
        # Use user-provided EPSG or auto-detect via UTM zone calculation
        self.target_crs = epsg if epsg else self._estimate_utm_crs()

        # Automatically execute the processing on instantiation
        self.result_path = self.run()

    def _load_gdf(self) -> gpd.GeoDataFrame:
        """Loads the vector boundary file safely."""
        try:
            if isinstance(self.boundary_input, gpd.GeoDataFrame):
                return self.boundary_input
            
            gdf = gpd.read_file(self.boundary_input, layer=self.layer)
            if gdf.empty:
                raise ValueError("The provided boundary file is empty.")
            return gdf
        except Exception as e:
            self.logger.error(f"Error reading boundary input: {e}")
            raise IOError(f"Error reading boundary file: {e}")

    def _estimate_utm_crs(self) -> int:
        """Calculates UTM EPSG code based on bounds."""
        bounds = self.gdf_wgs84.total_bounds
        lon = (bounds[0] + bounds[2]) / 2
        lat = (bounds[1] + bounds[3]) / 2
        
        zone = int((lon + 180) / 6) + 1
        epsg_base = 32600 if lat >= 0 else 32700
        return epsg_base + zone

    def run(self) -> str:
        """Executes the DEM processing logic."""
        self.logger.info(f"Initiating DEM processing. Target CRS: EPSG:{self.target_crs}")
        
        # Project boundary for final clipping to the target projected CRS
        gdf_projected = self.gdf_wgs84.to_crs(epsg=self.target_crs)

        # Standard LZW compressed GeoTIFF settings
        export_kwargs = {
            "driver": "GTiff", 
            "compress": "lzw", 
            "tiled": True,
            "blockxsize": 256, 
            "blockysize": 256
        }

        if self.dem_file:
            self.logger.info(f"Processing local DEM file: {self.dem_file}")
            dem = rioxarray.open_rasterio(self.dem_file)
            
            dem = dem.rio.reproject(
                f"EPSG:{self.target_crs}", 
                resolution=self.resolution, 
                resampling=Resampling.bilinear
            )
            dem = dem.rio.clip(gdf_projected.geometry, gdf_projected.crs, drop=True)
            dem = dem.rio.write_nodata(-999999, encoded=True)
            save_path = self.output_dir / (self.out_name or "processed_local_dem.tif")
            dem.rio.to_raster(save_path, **export_kwargs)
        else:
            self.logger.info(f"Fetching 3DEP DEM data from USGS at {self.resolution}m resolution...")
            import py3dep
            try:
                standard_res = [1, 3, 10, 30, 60]
                
                if self.resolution in standard_res:
                    dem_data = py3dep.get_dem(
                        geometry=self.boundary_geom, 
                        resolution=self.resolution, 
                        crs=4326 
                    )
                    dem_data = dem_data.rio.reproject(f"EPSG:{self.target_crs}")
                else:
                    self.logger.info(f"Non-standard resolution {self.resolution}m. Fetching at nearest 3DEP res and resampling.")
                    dem_data = py3dep.get_dem(
                        geometry=self.boundary_geom, 
                        resolution=self.resolution, 
                        crs=4326
                    )
                    
                    dem_data = dem_data.rio.reproject(
                        f"EPSG:{self.target_crs}",
                        resolution=self.resolution,
                        resampling=Resampling.bilinear
                    )

                # Mask and set standard nodata value
                dem_data = dem_data.where(dem_data > -90000, -999999)
                dem_data.rio.write_nodata(-999999, inplace=True)
                
                save_path = self.output_dir / (self.out_name or f"3dep_dem_{self.resolution}m.tif")
                dem_data.rio.to_raster(save_path, **export_kwargs)
                self.logger.info(f"DEM successfully saved to {save_path}")
            except Exception as e:
                self.logger.error(f"3DEP fetch failed: {e}")
                raise RuntimeError(f"3DEP fetch failed. Error: {e}")
        
        return str(save_path)