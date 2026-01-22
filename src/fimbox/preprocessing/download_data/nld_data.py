"""
Author: Supath Dhital
Date Created: January 2026

Description: Downloads and processes USACE National Levee Database (NLD) data, 
which includes levee lines and protected areas, filtered by a user-provided spatial boundary.
"""
import os
import re
import logging
import requests
from pathlib import Path
from datetime import datetime
from typing import Union, Optional

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon, MultiPolygon
from esri import ESRI_REST

class DownloadNLD:
    """
    Downloads and processes USACE National Levee Database (NLD) data 
    filtered by a user-provided spatial boundary.
    """
    
    # USACE NLD API Endpoints
    LINE_URL = "https://ags03.sec.usace.army.mil/server/rest/services/NLD2_PUBLIC/FeatureServer/15/query"
    POLY_URL = "https://ags03.sec.usace.army.mil/server/rest/services/NLD2_PUBLIC/FeatureServer/14/query"

    def __init__(
        self,
        boundary: Union[str, Path, gpd.GeoDataFrame, Polygon, MultiPolygon],
        layer_name: Optional[str] = None,
        epsg: int = 5070,
        output_dir: Optional[str] = None
    ):
        self.epsg = epsg
        self.today = datetime.now().strftime('%y%m%d')
        
        # Setup Logging
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
        self.logger = logging.getLogger(__name__)

        # Setup Directories
        self.output_dir = Path(output_dir) if output_dir else Path.cwd() / "nld_data"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Process Boundary
        self.boundary_gdf = self._prepare_boundary(boundary, layer_name)
        
        # Get Bounding Box for the API query
        self.bbox = self.boundary_gdf.total_bounds

        #Run the download process
        self.run()

    def _prepare_boundary(self, boundary, layer_name) -> gpd.GeoDataFrame:
        """Standardizes input boundary into a GeoDataFrame in the target EPSG."""
        if isinstance(boundary, (str, Path)):
            if not os.path.exists(boundary):
                raise FileNotFoundError(f"Boundary path not found: {boundary}")
            
            # Load GeoPackage or Shapefile
            if str(boundary).endswith('.gpkg') and layer_name:
                gdf = gpd.read_file(boundary, layer=layer_name)
            else:
                gdf = gpd.read_file(boundary)
        
        elif isinstance(boundary, gpd.GeoDataFrame):
            gdf = boundary.copy()
            
        elif isinstance(boundary, (Polygon, MultiPolygon)):
            gdf = gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326")
            
        else:
            raise ValueError("Boundary must be a path (str/Path), GeoDataFrame, or Shapely Geometry.")

        if gdf.crs is None:
            self.logger.warning("Boundary CRS missing. Assuming EPSG:4326.")
            gdf.set_crs(epsg=4326, inplace=True)

        return gdf.to_crs(epsg=self.epsg)

    def _query_nld(self, url: str, is_poly: bool = False) -> gpd.GeoDataFrame:
        """Queries NLD with spatial envelope filtering."""
        
        # Create an ESRI Envelope string from our boundary bbox
        geometry_filter = {
            "xmin": self.bbox[0], "ymin": self.bbox[1],
            "xmax": self.bbox[2], "ymax": self.bbox[3],
            "spatialReference": {"wkid": self.epsg}
        }

        self.logger.info(f"Querying NLD: {url}")
        
        # Arguments for the ESRI REST query
        query_args = {
            "url": url,
            "f": "json",
            "where": "1=1",
            "geometry": str(geometry_filter),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": str(self.epsg),
            "outFields": "*",
            "returnGeometry": "true",
        }

        # Handle 3D Line data vs 2D Polygons
        if not is_poly:
            query_args["returnZ"] = "true"
        else:
            # Polygons are heavy; use a smaller record count to prevent server 500 errors
            query_args["resultRecordCount"] = 2000

        try:
            gdf = ESRI_REST.query(**query_args)
            
            if gdf.empty:
                self.logger.warning("No data returned for this boundary.")
                return gdf

            # Precise Spatial Clip: The API returns everything that touches the BBox.
            return gpd.clip(gdf, self.boundary_gdf)

        except Exception as e:
            self.logger.error(f"API Query failed: {e}")
            return gpd.GeoDataFrame()

    def run(self):
        """Executes the download and save process."""
        
        # Download Lines
        lines_raw = self._query_nld(self.LINE_URL, is_poly=False)
        if not lines_raw.empty:
            out_path = self.output_dir / f"System_Routes_Filtered_{self.today}.gpkg"
            lines_raw.to_file(out_path, driver="GPKG", engine="pyogrio")
            self.logger.info(f"Saved lines to: {out_path}")
        
        # Download Protected Polygons
        polys_raw = self._query_nld(self.POLY_URL, is_poly=True)
        if not polys_raw.empty:
            out_path = self.output_dir / f"Leveed_Areas_Filtered_{self.today}.gpkg"
            polys_raw.to_file(out_path, driver="GPKG", engine="pyogrio")
            self.logger.info(f"Saved polygons to: {out_path}")