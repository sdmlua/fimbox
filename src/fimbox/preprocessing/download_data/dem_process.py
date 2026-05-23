"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date updated: Jan 2026

Description: Module to fetch 3DEP DEM data based on user-defined boundary and region.
It also processes local DEM files if provided or outside CONUS regions.
Everything from resolution to CRS can be specified at initialization and runs automatically.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Union, Tuple, Optional, List
from pathlib import Path
import xarray as xr
import rioxarray
import geopandas as gpd
import numpy as np
from shapely.geometry import box, Polygon, MultiPolygon
from rasterio.enums import Resampling

from ..._skip_if_valid import should_skip


class DEMProcessor:
    def __init__(
        self,
        boundary: Union[str, gpd.GeoDataFrame],
        layer: Optional[str] = None,
        output_dir: str = "./dem_output",
        out_name: Optional[str] = None,
        dem_file: Optional[str] = None,
        resolution: int = 10,
        epsg: Optional[int] = None,
        tile_size_deg: float = 0.25,
        max_workers: Optional[int] = None,
    ):
        self.boundary_input = boundary
        self.layer = layer
        self.dem_file = dem_file
        self.resolution = resolution
        # Parallel-tile fetch knobs. tile_size_deg controls how the AOI is split
        # for concurrent py3dep.get_dem calls; ~0.25 degrees keeps each tile
        # well under 3DEP's per-request limits while staying chunky enough to
        # avoid per-request overhead. max_workers defaults to os.cpu_count()
        # bounded by 8 (3DEP rate-limits aggressive parallelism).
        self.tile_size_deg = tile_size_deg
        self.max_workers = max_workers or min(8, (os.cpu_count() or 4))

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
        self.logger.info(
            f"Initiating DEM processing. Target CRS: EPSG:{self.target_crs}"
        )

        # Resolve the final output path up-front so skip-if-valid can short-circuit
        # before any reprojection / clipping / network work.
        save_path = self.output_dir / (
            self.out_name
            or ("processed_local_dem.tif" if self.dem_file else f"3dep_dem_{self.resolution}m.tif")
        )
        if should_skip(save_path):
            self.logger.info(f"DEM output already valid, skipping: {save_path}")
            return str(save_path)

        # Project boundary for final clipping to the target projected CRS
        gdf_projected = self.gdf_wgs84.to_crs(epsg=self.target_crs)

        # Standard LZW compressed GeoTIFF settings
        export_kwargs = {
            "driver": "GTiff",
            "compress": "lzw",
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
        }

        if self.dem_file:
            self.logger.info(f"Processing local DEM file: {self.dem_file}")
            dem = rioxarray.open_rasterio(self.dem_file)

            dem = dem.rio.reproject(
                f"EPSG:{self.target_crs}",
                resolution=self.resolution,
                resampling=Resampling.bilinear,
            )
            dem = dem.rio.clip(gdf_projected.geometry, gdf_projected.crs, drop=True)
            dem = dem.rio.write_nodata(-999999, encoded=True)
            dem.rio.to_raster(save_path, **export_kwargs)
        else:
            self.logger.info(
                f"Fetching 3DEP DEM data from USGS at {self.resolution}m resolution "
                f"using up to {self.max_workers} parallel tiles..."
            )
            try:
                dem_data = self._fetch_3dep_parallel()

                if self.resolution in [1, 3, 10, 30, 60]:
                    dem_data = dem_data.rio.reproject(f"EPSG:{self.target_crs}")
                else:
                    self.logger.info(
                        f"Non-standard resolution {self.resolution}m. Resampling to target res."
                    )
                    dem_data = dem_data.rio.reproject(
                        f"EPSG:{self.target_crs}",
                        resolution=self.resolution,
                        resampling=Resampling.bilinear,
                    )

                # Mask and set standard nodata value
                dem_data = dem_data.where(dem_data > -90000, -999999)
                dem_data.rio.write_nodata(-999999, inplace=True)

                dem_data.rio.to_raster(save_path, **export_kwargs)
                self.logger.info(f"DEM successfully saved to {save_path}")
            except Exception as e:
                self.logger.error(f"3DEP fetch failed: {e}")
                raise RuntimeError(f"3DEP fetch failed. Error: {e}")

        return str(save_path)

    def _tile_boundary(self) -> List[Polygon]:
        """Split the WGS84 boundary into a grid of tile_size_deg cells, each
        intersected with the boundary geometry. Cells that don't touch the
        boundary are dropped. Returns the list of per-tile geometries that
        will each be fetched in parallel."""
        minx, miny, maxx, maxy = self.boundary_geom.bounds
        step = float(self.tile_size_deg)
        # tiny AOIs: no tiling — one shot is fastest
        if (maxx - minx) <= step and (maxy - miny) <= step:
            return [self.boundary_geom]

        tiles: List[Polygon] = []
        y = miny
        while y < maxy:
            x = minx
            while x < maxx:
                cell = box(x, y, min(x + step, maxx), min(y + step, maxy))
                clipped = cell.intersection(self.boundary_geom)
                if not clipped.is_empty:
                    tiles.append(clipped)
                x += step
            y += step
        return tiles

    def _fetch_3dep_parallel(self) -> xr.DataArray:
        """Fetch 3DEP DEM tiles concurrently and mosaic them.

        py3dep.get_dem releases the GIL during HTTP, so a ThreadPoolExecutor
        gives a near-linear speedup up to the 3DEP rate limit. Tiles are
        retrieved in WGS84 (the native 3DEP CRS) and merged before the caller
        reprojects to the target CRS — merging in lat/lon avoids per-tile
        resampling artifacts along seams.
        """
        import py3dep

        tiles = self._tile_boundary()
        n = len(tiles)
        if n == 1:
            return py3dep.get_dem(
                geometry=tiles[0], resolution=self.resolution, crs=4326
            )

        self.logger.info(f"Splitting AOI into {n} tiles for parallel fetch")

        def _one(geom):
            return py3dep.get_dem(
                geometry=geom, resolution=self.resolution, crs=4326
            )

        results: list = [None] * n
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_idx = {pool.submit(_one, g): i for i, g in enumerate(tiles)}
            done = 0
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                results[idx] = fut.result()
                done += 1
                if done % max(1, n // 10) == 0 or done == n:
                    self.logger.info(f"  tiles fetched: {done}/{n}")

        # Mosaic in WGS84
        from rioxarray.merge import merge_arrays

        merged = merge_arrays(results)
        return merged
