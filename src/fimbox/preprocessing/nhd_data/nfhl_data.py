"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date Created: January 2026

Description: Downloads and processes FEMA National Flood Hazard Layer (NFHL) flood hazard zones.
Updated with explicit handler initialization to ensure logs are written to disk.
"""
import logging
import argparse
import requests
import geopandas as gpd
import pandas as pd
from pathlib import Path
from shapely.geometry import Polygon, MultiPolygon
from typing import Union, Optional

class DownloadFEMANFHL:
    # FEMA ArcGIS REST API Constants
    NFHL_BASE_URL = "https://hazards.fema.gov/arcgis/rest/services/FIRMette/NFHLREST_FIRMette/MapServer"
    HAZARD_ZONE_LAYER = 20
    AVAILABILITY_LAYER = 0
    CONUS_CRS = 5070  # NAD83 / Conus Albers

    def __init__(
        self, 
        boundary: Union[str, gpd.GeoDataFrame, Polygon, MultiPolygon],
        output_path: Optional[str] = None,
        log_path: Optional[str] = None
    ):
        if output_path:
            self.output_path = Path(output_path)
        else:
            base_name = Path(boundary).stem if isinstance(boundary, str) else "flood_zones"
            self.output_path = Path.cwd() / "nfhl_data" / f"fema_nfhl_{base_name}.gpkg"

        self.output_dir = self.output_path.parent
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Setup Logging
        self.logger = logging.getLogger("FEMADownloader")
        self.logger.setLevel(logging.INFO)
        
        # Prevent adding multiple handlers if class is re-instantiated
        if not self.logger.handlers:
            actual_log_file = log_path if log_path else self.output_dir / "fema_download.log"
            file_handler = logging.FileHandler(actual_log_file, mode='a') # 'a' for append
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        
        try:
            self.logger.info("--- NFHL Processing Started ---")
            
            # Prepare Geometry
            self.gdf = self._prepare_geometry(boundary)
            
            # Round BBox to 2 decimal places to simplify server-side query
            raw_bbox = self.gdf.total_bounds
            self.bbox = [round(x, 2) for x in raw_bbox]
            
            self.run()
            self.logger.info("--- NFHL Processing Completed Successfully ---")
            
        except Exception as e:
            self.logger.error(f"Critical Failure in NFHL module: {str(e)}", exc_info=True)

    def _prepare_geometry(self, boundary):
        if isinstance(boundary, str):
            gdf = gpd.read_file(boundary)
        elif isinstance(boundary, (Polygon, MultiPolygon)):
            gdf = gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326")
        elif isinstance(boundary, gpd.GeoDataFrame):
            gdf = boundary.copy() 
        else:
            raise ValueError("Unsupported boundary type.")

        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
            
        return gdf.to_crs(epsg=self.CONUS_CRS)

    def _query_fema_api(self, layer_id, where_clause="1=1"):
        """Queries FEMA REST API using POST for stability."""
        xmin, ymin, xmax, ymax = self.bbox
        
        payload = {
            "where": where_clause,
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": self.CONUS_CRS,
            "outSR": self.CONUS_CRS,
            "outFields": "*",
            "returnGeometry": "true",
            "f": "geojson",
            "geometryPrecision": 1 
        }
        
        response = requests.post(f"{self.NFHL_BASE_URL}/{layer_id}/query", data=payload)
        response.raise_for_status()
        
        data = response.json()
        if not data.get("features"):
            return gpd.GeoDataFrame(crs=self.CONUS_CRS)
            
        return gpd.read_file(response.text)

    def _process_layer_geometries(self, gdf, label, fill_holes=False):
        if gdf.empty:
            self.logger.warning(f"No {label} zones found.")
            return

        gdf.loc[:, 'geometry'] = gdf['geometry'].make_valid()
        gdf = gpd.clip(gdf, self.gdf)
        
        gdf = gdf[gdf.geom_type.isin(['Polygon', 'MultiPolygon'])]
        if gdf.empty: 
            self.logger.warning(f"No valid polygons remain for {label} after clipping.")
            return

        dissolved = gdf.dissolve().explode(index_parts=True).reset_index(drop=True)
        
        if fill_holes:
            new_geoms = [Polygon(geom.exterior) for geom in dissolved.geometry if geom is not None]
            dissolved.geometry = new_geoms

        final_gdf = dissolved[~dissolved.geometry.isna()].dissolve().reset_index(drop=True)
        final_gdf = final_gdf.dropna(axis=1, how='all')

        if not final_gdf.empty:
            final_gdf.to_file(self.output_path, layer=label, index=False, driver="GPKG")
            self.logger.info(f"Saved layer: {label} to {self.output_path.name}")

    def run(self):
        # Availability
        self.logger.info(f"Querying Availability for BBox: {self.bbox}")
        availability_raw = self._query_fema_api(self.AVAILABILITY_LAYER, "1=1")
        self._process_layer_geometries(availability_raw, "availability", fill_holes=False)

        # Hazard Zones
        self.logger.info("Querying Hazard Zones (100yr and 500yr)...")
        hazard_where = (
            "(FLD_ZONE LIKE 'A%' OR FLD_ZONE LIKE 'V%') OR "
            "(FLD_ZONE LIKE 'X' AND ZONE_SUBTY = '0.2 PCT ANNUAL CHANCE FLOOD HAZARD')"
        )
        hazard_raw = self._query_fema_api(self.HAZARD_ZONE_LAYER, hazard_where)
        
        if not hazard_raw.empty:
            nfhl_100 = hazard_raw[hazard_raw['FLD_ZONE'].str.startswith(('A', 'V'))].copy()
            self._process_layer_geometries(nfhl_100, "100_year", fill_holes=True)
            
            nfhl_500 = hazard_raw[
                (hazard_raw['FLD_ZONE'] == 'X') & 
                (hazard_raw['ZONE_SUBTY'].str.contains('0.2', na=False))
            ].copy()
            self._process_layer_geometries(nfhl_500, "500_year", fill_holes=True)
            
            self._process_layer_geometries(hazard_raw, "combined", fill_holes=True)
        else:
            self.logger.warning("FEMA API returned zero hazard features for this area.")

#CLI Interface
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query NFHL flood hazard zones.")
    parser.add_argument("-b", "--boundary", required=True)
    parser.add_argument("-o", "--output", help="Output path")
    args = parser.parse_args()
    DownloadFEMANFHL(boundary=args.boundary, output_path=args.output)