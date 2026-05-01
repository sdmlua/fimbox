"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date Created: January 2026

Description:
Downloads and processes FEMA NFHL flood hazard zones.
Improved to handle large areas via automatic bbox tiling + optional paging, and merges tiles
into one final output per layer.
"""
import logging
import argparse
import math
import time
from datetime import datetime, timezone 
from pathlib import Path
from typing import Union, Optional, List, Tuple

import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon, MultiPolygon


class DownloadFEMANFHL:
    # FEMA ArcGIS REST API Constants
    NFHL_BASE_URL = "https://hazards.fema.gov/arcgis/rest/services/FIRMette/NFHLREST_FIRMette/MapServer"
    HAZARD_ZONE_LAYER = 20
    AVAILABILITY_LAYER = 0
    CONUS_CRS = 5070  # NAD83 / Conus Albers

    def __init__(
        self,
        boundary: Union[str, gpd.GeoDataFrame, Polygon, MultiPolygon],
        out_dir: Optional[str] = None,
        out_name: Optional[str] = None,
        log_path: Optional[str] = None,
        tile_size_m: float = 50_000.0,         # 50 km tiles in EPSG:5070
        tile_count_threshold: int = 5000,      # if feature count > this, tile
        page_size: int = 2000,                 # paging size within each tile (if supported)
        max_pages: int = 500,                  # safety guard
        request_timeout_s: int = 120,
        max_retries: int = 3,
        retry_backoff_s: float = 1.5,
    ):
        self.tile_size_m = float(tile_size_m)
        self.tile_count_threshold = int(tile_count_threshold)
        self.page_size = int(page_size)
        self.max_pages = int(max_pages)
        self.request_timeout_s = int(request_timeout_s)
        self.max_retries = int(max_retries)
        self.retry_backoff_s = float(retry_backoff_s)

        # Output path — out_dir sets the folder, out_name sets the filename
        if out_dir and out_name:
            self.output_path = Path(out_dir) / out_name
        elif out_dir:
            # legacy: out_dir may be a full file path or just a directory
            p = Path(out_dir)
            self.output_path = p if p.suffix else p / (out_name or "fema_nfhl.gpkg")
        else:
            base_name = Path(boundary).stem if isinstance(boundary, str) else "flood_zones"
            self.output_path = Path.cwd() / "nfhl_data" / f"fema_nfhl_{base_name}.gpkg"
        self.output_dir = self.output_path.parent
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Logging
        self.logger = logging.getLogger("FEMADownloader")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            if log_path:
                actual_log_file = Path(log_path)
            else:
                run_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                actual_log_file = self.output_dir / f"fema_download_{run_tag}.log"

            fh = logging.FileHandler(actual_log_file, mode="w") 
            fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
            self.logger.addHandler(fh)
            self.logger.info(f"Logging to: {actual_log_file}")

        # Session (simple retries)
        self.session = requests.Session()

        try:
            self.logger.info("--- NFHL Processing Started ---")

            self.gdf = self._prepare_geometry(boundary)
            raw_bbox = self.gdf.total_bounds  # xmin, ymin, xmax, ymax in CONUS_CRS
            self.bbox = [float(raw_bbox[0]), float(raw_bbox[1]), float(raw_bbox[2]), float(raw_bbox[3])]
            self.bbox = [round(x, 2) for x in self.bbox]

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

    # FEMA query helpers
    def _post_with_retries(self, url: str, data: dict) -> requests.Response:
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.session.post(url, data=data, timeout=self.request_timeout_s)
                return r
            except Exception as e:
                last_exc = e
                sleep_s = self.retry_backoff_s * attempt
                self.logger.warning(f"Request failed (attempt {attempt}/{self.max_retries}): {e}. Sleeping {sleep_s:.1f}s")
                time.sleep(sleep_s)
        raise last_exc

    def _query_count(self, layer_id: int, where_clause: str, bbox: List[float]) -> int:
        xmin, ymin, xmax, ymax = bbox
        payload = {
            "where": where_clause,
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": self.CONUS_CRS,
            "returnCountOnly": "true",
            "f": "json",
        }
        url = f"{self.NFHL_BASE_URL}/{layer_id}/query"
        r = self._post_with_retries(url, payload)
        # If server returns a JSON error (sometimes with 500), log its body
        if r.status_code != 200:
            self.logger.error(f"Count query HTTP {r.status_code}. Body (trunc): {r.text[:2000]}")
            r.raise_for_status()
        j = r.json()
        return int(j.get("count", 0))

    def _query_features_page(
        self,
        layer_id: int,
        where_clause: str,
        bbox: List[float],
        result_offset: int = 0,
        result_record_count: int = 2000,
    ) -> Tuple[gpd.GeoDataFrame, bool]:
        """
        Returns (gdf, exceeded_limit_flag).
        Notes:
          - Some ArcGIS services honor resultOffset/resultRecordCount; some do not.
          - We still attempt paging; if not supported, you typically won't see exceededTransferLimit anyway.
        """
        xmin, ymin, xmax, ymax = bbox
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
            "geometryPrecision": 1,
            "returnExceededLimitFeatures": "true",
            "resultOffset": str(int(result_offset)),
            "resultRecordCount": str(int(result_record_count)),
        }
        url = f"{self.NFHL_BASE_URL}/{layer_id}/query"
        r = self._post_with_retries(url, payload)
        if r.status_code != 200:
            self.logger.error(f"Feature query HTTP {r.status_code}. Body (trunc): {r.text[:2000]}")
            r.raise_for_status()

        data = r.json()
        feats = data.get("features") or []
        exceeded = bool(data.get("exceededTransferLimit", False))

        if not feats:
            return gpd.GeoDataFrame(crs=f"EPSG:{self.CONUS_CRS}"), exceeded

        # GeoJSON FeatureCollection -> GeoDataFrame
        gdf = gpd.GeoDataFrame.from_features(feats, crs=f"EPSG:{self.CONUS_CRS}")
        return gdf, exceeded

    def _query_features_all(self, layer_id: int, where_clause: str, bbox: List[float]) -> gpd.GeoDataFrame:
        """
        Attempts to page results within bbox; if paging isn't supported, you'll typically
        just get the first page. Tiling is the main robustness mechanism.
        """
        all_parts = []
        offset = 0
        for page in range(self.max_pages):
            part, exceeded = self._query_features_page(
                layer_id=layer_id,
                where_clause=where_clause,
                bbox=bbox,
                result_offset=offset,
                result_record_count=self.page_size,
            )
            if not part.empty:
                all_parts.append(part)

            # Stop conditions:
            # - If returned fewer than page size, we likely reached the end (when paging supported).
            # - If empty, also stop.
            # - If paging unsupported, we may always see the same page; tiling avoids that.
            if part.empty or len(part) < self.page_size:
                break

            # Move to next page
            offset += self.page_size

            # If server did not indicate exceeded limit, but we got a full page, still try next page.
            # Guard exists via max_pages.
            if not exceeded and page > 0 and len(part) == self.page_size:
                pass

        if not all_parts:
            return gpd.GeoDataFrame(crs=f"EPSG:{self.CONUS_CRS}")
        return gpd.GeoDataFrame(pd.concat(all_parts, ignore_index=True), crs=f"EPSG:{self.CONUS_CRS}")

    # Tiling helpers
    def _tile_bbox(self, bbox: List[float], tile_size_m: float) -> List[List[float]]:
        xmin, ymin, xmax, ymax = map(float, bbox)
        width = xmax - xmin
        height = ymax - ymin
        nx = max(1, int(math.ceil(width / tile_size_m)))
        ny = max(1, int(math.ceil(height / tile_size_m)))

        dx = width / nx if nx else width
        dy = height / ny if ny else height

        tiles = []
        for ix in range(nx):
            for iy in range(ny):
                x0 = xmin + ix * dx
                x1 = xmin + (ix + 1) * dx
                y0 = ymin + iy * dy
                y1 = ymin + (iy + 1) * dy
                tiles.append([round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)])
        return tiles

    def _dedupe_features(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if gdf.empty:
            return gdf

        # Prefer a stable ID if present
        for id_col in ("OBJECTID", "OBJECTID_1", "FID", "id"):
            if id_col in gdf.columns:
                return gdf.drop_duplicates(subset=[id_col]).reset_index(drop=True)

        # Fallback: geometry hash
        geom_hash = gdf.geometry.apply(lambda g: g.wkb_hex if g is not None else None)
        gdf = gdf.assign(_geom_hash=geom_hash)
        gdf = gdf.drop_duplicates(subset=["_geom_hash"]).drop(columns=["_geom_hash"])
        return gdf.reset_index(drop=True)

    # Geometry processing
    def _process_layer_geometries(self, gdf, label, fill_holes=False):
        if gdf.empty:
            self.logger.warning(f"No {label} zones found.")
            return

        gdf = gdf.copy()
        gdf.loc[:, "geometry"] = gdf["geometry"].make_valid()
        gdf = gpd.clip(gdf, self.gdf)

        gdf = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon"])]
        if gdf.empty:
            self.logger.warning(f"No valid polygons remain for {label} after clipping.")
            return

        dissolved = gdf.dissolve().explode(index_parts=True).reset_index(drop=True)

        if fill_holes:
            new_geoms = [Polygon(geom.exterior) for geom in dissolved.geometry if geom is not None]
            dissolved.geometry = new_geoms

        final_gdf = dissolved[~dissolved.geometry.isna()].dissolve().reset_index(drop=True)
        final_gdf = final_gdf.dropna(axis=1, how="all")

        if not final_gdf.empty:
            final_gdf.to_file(self.output_path, layer=label, index=False, driver="GPKG")
            self.logger.info(f"Saved layer: {label} to {self.output_path.name}")

    # Public run
    def run(self):
        # Availability
        self.logger.info(f"Querying Availability for BBox: {self.bbox}")
        availability_raw = self._query_features_all(self.AVAILABILITY_LAYER, "1=1", self.bbox)
        self._process_layer_geometries(availability_raw, "availability", fill_holes=False)

        # Hazard Zones
        self.logger.info("Querying Hazard Zones (100yr and 500yr)...")
        hazard_where = (
            "(FLD_ZONE LIKE 'A%' OR FLD_ZONE LIKE 'V%') OR "
            "(FLD_ZONE LIKE 'X' AND ZONE_SUBTY = '0.2 PCT ANNUAL CHANCE FLOOD HAZARD')"
        )

        # Decide: tile or single-shot
        try:
            hazard_count = self._query_count(self.HAZARD_ZONE_LAYER, hazard_where, self.bbox)
            self.logger.info(f"Hazard feature count (bbox pre-clip): {hazard_count}")
        except Exception as e:
            # If count fails, default to tiling for safety
            self.logger.warning(f"Count query failed ({e}); defaulting to tiling.")
            hazard_count = self.tile_count_threshold + 1

        use_tiling = hazard_count > self.tile_count_threshold

        if not use_tiling:
            self.logger.info("Area considered 'small' -> single bbox query.")
            hazard_raw = self._query_features_all(self.HAZARD_ZONE_LAYER, hazard_where, self.bbox)
        else:
            tiles = self._tile_bbox(self.bbox, self.tile_size_m)
            self.logger.info(f"Large area -> tiling enabled: {len(tiles)} tiles (tile_size_m={self.tile_size_m:g}).")

            parts = []
            for i, tb in enumerate(tiles, start=1):
                try:
                    self.logger.info(f"Querying hazard tile {i}/{len(tiles)} bbox={tb}")
                    g = self._query_features_all(self.HAZARD_ZONE_LAYER, hazard_where, tb)
                    if not g.empty:
                        parts.append(g)
                except Exception as e:
                    self.logger.warning(f"Tile {i}/{len(tiles)} failed: {e}")

            if parts:
                hazard_raw = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=f"EPSG:{self.CONUS_CRS}")
                hazard_raw = self._dedupe_features(hazard_raw)
            else:
                hazard_raw = gpd.GeoDataFrame(crs=f"EPSG:{self.CONUS_CRS}")

        if hazard_raw.empty:
            self.logger.warning("FEMA API returned zero hazard features for this area (or all tiles failed).")
            return

        # Split + process 
        if "FLD_ZONE" not in hazard_raw.columns:
            self.logger.warning("Hazard result missing FLD_ZONE field; cannot classify 100/500-year zones.")
            self._process_layer_geometries(hazard_raw, "combined", fill_holes=True)
            return

        nfhl_100 = hazard_raw[hazard_raw["FLD_ZONE"].astype(str).str.startswith(("A", "V"))].copy()
        self._process_layer_geometries(nfhl_100, "100_year", fill_holes=True)

        # 500-year X with 0.2% subtype
        if "ZONE_SUBTY" in hazard_raw.columns:
            nfhl_500 = hazard_raw[
                (hazard_raw["FLD_ZONE"].astype(str) == "X")
                & (hazard_raw["ZONE_SUBTY"].astype(str).str.contains("0.2", na=False))
            ].copy()
            self._process_layer_geometries(nfhl_500, "500_year", fill_holes=True)

        self._process_layer_geometries(hazard_raw, "combined", fill_holes=True)


# CLI Interface
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query NFHL flood hazard zones.")
    parser.add_argument("-b", "--boundary", required=True, help="Boundary file path (or supported geometry input in code).")
    parser.add_argument("-o", "--output", help="Output GeoPackage path")
    parser.add_argument("--tile-size-m", type=float, default=50_000.0, help="Tile size (meters) for large areas (EPSG:5070).")
    parser.add_argument("--tile-threshold", type=int, default=5000, help="Feature count threshold to enable tiling.")
    args = parser.parse_args()

    DownloadFEMANFHL(
        boundary=args.boundary,
        out_dir=args.output,
        tile_size_m=args.tile_size_m,
        tile_count_threshold=args.tile_threshold,
    )
