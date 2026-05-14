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
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from tqdm import tqdm

log = logging.getLogger(__name__)


class ESRI_REST:
    """
    A robust utility for querying ESRI Feature Services.
    Handles automatic paging (offset) when datasets exceed the server's transfer limit.
    """

    def __init__(self, url: str, verbose: bool = True):
        self.url = url
        self.verbose = verbose
        self.base_url = url.split("/query")[0]

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
        response = requests.get(self.url, params=count_params, timeout=60)
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
            log.info(f"ESRI query: {total_features} features to download")

        if total_features == 0:
            return gpd.GeoDataFrame()

        with tqdm(
            total=total_features, disable=not self.verbose, desc="Downloading"
        ) as pbar:
            while limit_reached:
                current_params = {**params, "resultOffset": offset}
                resp = requests.get(self.url, params=current_params, timeout=120)
                resp.raise_for_status()

                data = resp.json()
                if "error" in data:
                    raise Exception(
                        f"ESRI Error {data['error']['code']}: {data['error']['message']}"
                    )

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

    def _execute_query_with_z(self, params: dict) -> gpd.GeoDataFrame:
        """
        Like _execute_query but uses f=json + returnZ=true so Z coordinates are
        preserved. Parses ESRI JSON 'paths' directly into shapely LineStrings.
        GeoJSON silently strips Z, so this path is required for levee lines.
        """
        total_features = self._get_metadata(params)
        if total_features == 0:
            return gpd.GeoDataFrame()

        base_params = {**params, "f": "json", "returnZ": "true"}
        if self.verbose:
            log.info(f"ESRI query (with Z): {total_features} features to download")

        results = []
        offset = 0
        limit_reached = True

        with tqdm(
            total=total_features, disable=not self.verbose, desc="Downloading"
        ) as pbar:
            while limit_reached:
                current_params = {**base_params, "resultOffset": offset}
                resp = requests.get(self.url, params=current_params, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise Exception(
                        f"ESRI Error {data['error']['code']}: {data['error']['message']}"
                    )

                features = data.get("features", [])
                if not features:
                    break

                rows = []
                for feat in features:
                    attrs = feat.get("attributes", {})
                    paths = feat.get("geometry", {}).get("paths", [])
                    if not paths:
                        continue
                    lines = [LineString(path) for path in paths if len(path) >= 2]
                    if not lines:
                        continue
                    geom = lines[0] if len(lines) == 1 else MultiLineString(lines)
                    attrs["geometry"] = geom
                    rows.append(attrs)

                if rows:
                    batch = gpd.GeoDataFrame(rows, geometry="geometry")
                    results.append(batch)

                offset += len(features)
                pbar.update(len(features))
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
        out_dir: Optional[str] = None,
        lines_name: Optional[str] = None,
        polys_name: Optional[str] = None,
    ):
        self.epsg = epsg
        self.lines_name = lines_name or "NLD_Lines.gpkg"
        self.polys_name = polys_name or "NLD_Polygons.gpkg"

        self.logger = log

        self.output_dir = Path(out_dir) if out_dir else Path.cwd() / "nld_data"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Process Boundary
        self.boundary_gdf = self._prepare_boundary(boundary, layer_name)

        self.run()

    def _prepare_boundary(self, boundary, layer_name) -> gpd.GeoDataFrame:
        if isinstance(boundary, (str, Path)):
            if not os.path.exists(boundary):
                raise FileNotFoundError(f"Boundary path not found: {boundary}")
            if str(boundary).endswith(".gpkg") and layer_name:
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
        layer_type = "Polygons (Layer 16)" if is_poly else "Lines (Layer 15)"

        # Spatial bbox filter is non-functional on this service — download all, clip locally.
        # Lines use _execute_query_with_z (f=json + returnZ=true) to preserve Z elevation.
        # Polygons use the standard GeoJSON path (no Z needed).
        base_params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4269",
        }

        try:
            log.info(
                f"Downloading NLD {layer_type} (spatial filter unsupported, clipping locally)"
            )
            rest = ESRI_REST(url)
            if is_poly:
                gdf_raw = rest._execute_query(base_params)
            else:
                gdf_raw = rest._execute_query_with_z(base_params)

            if gdf_raw.empty:
                log.warning(f"NLD {layer_type}: service returned no features.")
                return gdf_raw

            if gdf_raw.crs is None:
                gdf_raw = gdf_raw.set_crs("EPSG:4269")

            boundary_4269 = self.boundary_gdf.to_crs("EPSG:4269")
            clipped = gpd.clip(gdf_raw, boundary_4269)

            if clipped.empty:
                log.warning(f"NLD {layer_type}: no features intersect boundary.")
                return clipped

            # Lines: reproject XY but preserve Z — use set_crs + manual reproject
            # to_crs() preserves Z in modern geopandas when the geometry already has it
            return clipped.to_crs(epsg=self.epsg)

        except Exception as e:
            log.error(f"NLD {layer_type} failed: {e}", exc_info=True)
            return gpd.GeoDataFrame()

    def run(self):
        log.info("--- NLD download ---")

        lines = self._query_nld(self.LINE_URL, is_poly=False)
        if not lines.empty:
            lines.to_file(self.output_dir / self.lines_name, driver="GPKG")
            log.info(f"NLD levee lines ({len(lines)} features) --> {self.lines_name}")

        polys = self._query_nld(self.POLY_URL, is_poly=True)
        if not polys.empty:
            polys.to_file(self.output_dir / self.polys_name, driver="GPKG")
            log.info(
                f"NLD leveed-area polygons ({len(polys)} features) --> {self.polys_name}"
            )

        log.info("NLD download complete.")


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="Download NLD levees within a boundary."
    )
    parser.add_argument("--boundary", required=True, help="Path to boundary file")
    parser.add_argument("--layer-name", default=None)
    parser.add_argument("--out-dir", default="nld_data")
    parser.add_argument("--epsg", type=int, default=5070)
    parser.add_argument("--lines-name", default=None)
    parser.add_argument("--polys-name", default=None)
    args = parser.parse_args()
    DownloadNLD(
        boundary=args.boundary,
        layer_name=args.layer_name,
        epsg=args.epsg,
        out_dir=args.out_dir,
        lines_name=args.lines_name,
        polys_name=args.polys_name,
    )
