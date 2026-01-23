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
from tqdm import tqdm

class ESRI_REST:
    """
    A robust utility for querying ESRI Feature Services. 
    Handles automatic paging (offset) when datasets exceed the server's transfer limit.
    """

    def __init__(self, url: str, verbose: bool = True):
        self.url = url
        self.verbose = verbose
        self.base_url = url.split('/query')[0]

    @classmethod
    def query(cls, url: str, save_path: str = None, **kwargs) -> gpd.GeoDataFrame:
        """Main entry point. Fetches data and optionally saves to disk."""
        instance = cls(url)
        gdf = instance._execute_query(kwargs)
        
        if save_path:
            gdf.to_file(save_path, driver="GPKG", index=False)
            return None
        return gdf

    def _get_metadata(self, params: dict) -> int:
        """Fetch total feature count for the given query parameters."""
        count_params = {**params, "returnCountOnly": "true", "f": "json"}
        response = requests.get(self.url, params=count_params)
        response.raise_for_status()
        return response.json().get("count", 0)

    def _execute_query(self, params: dict) -> gpd.GeoDataFrame:
        """Handles the pagination loop and GeoDataFrame concatenation."""
        total_features = self._get_metadata(params)
        params["f"] = "geojson"
        
        results = []
        offset = 0
        limit_reached = True

        if self.verbose and total_features > 0:
            print(f"--- ESRI Query Started ---")
            print(f"Total features to download: {total_features}")

        if total_features == 0:
            return gpd.GeoDataFrame()

        with tqdm(total=total_features, disable=not self.verbose, desc="Downloading") as pbar:
            while limit_reached:
                current_params = {**params, "resultOffset": offset}
                resp = requests.get(self.url, params=current_params)
                resp.raise_for_status()
                
                data = resp.json()
                if "error" in data:
                    raise Exception(f"ESRI Error {data['error']['code']}: {data['error']['message']}")

                batch_gdf = gpd.read_file(resp.text)
                if batch_gdf.empty:
                    break
                    
                results.append(batch_gdf)
                offset += len(batch_gdf)
                pbar.update(len(batch_gdf))
                
                limit_reached = data.get("exceededTransferLimit", False)
                if not limit_reached and offset < total_features:
                    limit_reached = True
                elif offset >= total_features:
                    limit_reached = False

        if not results:
            return gpd.GeoDataFrame()

        return pd.concat(results, ignore_index=True)

class DownloadNLD:
    """
    Downloads and processes USACE National Levee Database (NLD) data 
    using the new geospatial.sec.usace.army.mil endpoint.
    """
    
    # UPDATED URLs and LAYER IDs 
    BASE_SERVICE_URL = "https://geospatial.sec.usace.army.mil/dls/rest/services/NLD/Public/FeatureServer"
    LINE_URL = f"{BASE_SERVICE_URL}/15/query" 
    POLY_URL = f"{BASE_SERVICE_URL}/16/query" 
    def __init__(
        self,
        boundary: Union[str, Path, gpd.GeoDataFrame, Polygon, MultiPolygon],
        layer_name: Optional[str] = None,
        epsg: int = 5070,
        output_dir: Optional[str] = None
    ):
        self.epsg = epsg
        self.today = datetime.now().strftime('%y%m%d')
        
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
        self.logger = logging.getLogger(__name__)

        self.output_dir = Path(output_dir) if output_dir else Path.cwd() / "nld_data"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Process Boundary
        self.boundary_gdf = self._prepare_boundary(boundary, layer_name)
        
        # bbox uses standard floats for the API
        raw_bbox = self.boundary_gdf.total_bounds
        self.bbox = [float(x) for x in raw_bbox]

        self.run()

    def _prepare_boundary(self, boundary, layer_name) -> gpd.GeoDataFrame:
        if isinstance(boundary, (str, Path)):
            if not os.path.exists(boundary):
                raise FileNotFoundError(f"Boundary path not found: {boundary}")
            if str(boundary).endswith('.gpkg') and layer_name:
                gdf = gpd.read_file(boundary, layer=layer_name)
            else:
                gdf = gpd.read_file(boundary)
        elif isinstance(boundary, gpd.GeoDataFrame):
            gdf = boundary.copy()
        elif isinstance(boundary, (Polygon, MultiPolygon)):
            gdf = gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326")
        else:
            raise ValueError("Unsupported boundary format.")

        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
        return gdf.to_crs(epsg=self.epsg)

    def _query_nld(self, url: str, is_poly: bool = False) -> gpd.GeoDataFrame:
        geometry_filter = {
            "xmin": self.bbox[0], "ymin": self.bbox[1],
            "xmax": self.bbox[2], "ymax": self.bbox[3],
            "spatialReference": {"wkid": self.epsg}
        }

        layer_type = "Polygons (Layer 16)" if is_poly else "Lines (Layer 15)"
        
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

        if not is_poly:
            query_args["returnZ"] = "true"
        else:
            query_args["resultRecordCount"] = 2000

        try:
            gdf_raw = ESRI_REST.query(**query_args)
            if gdf_raw.empty:
                print(f"DEBUG [{layer_type}]: No data in BBox.")
                return gdf_raw

            # Clip to your specific study area
            return gpd.clip(gdf_raw, self.boundary_gdf)

        except Exception as e:
            self.logger.error(f"Failed to query {layer_type}: {e}")
            return gpd.GeoDataFrame()

    def run(self):
        print("\n" + "-"*50)
        print(f"NLD DOWNLOAD AUDIT - {datetime.now().strftime('%H:%M:%S')}")
        print("-" * 50)
        
        lines = self._query_nld(self.LINE_URL, is_poly=False)
        if not lines.empty:
            lines.to_file(self.output_dir / f"NLD_Lines_{self.today}.gpkg", driver="GPKG")
            print(f"Saved {len(lines)} levee lines.")
        
        polys = self._query_nld(self.POLY_URL, is_poly=True)
        if not polys.empty:
            polys.to_file(self.output_dir / f"NLD_Polygons_{self.today}.gpkg", driver="GPKG")
            print(f"Saved {len(polys)} leveed area polygons.")
        
        print("Process complete.")