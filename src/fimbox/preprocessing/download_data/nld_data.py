"""
Author: Supath Dhital
Date Created: January 2026

Description: Downloads and processes USACE National Levee Database (NLD) data,
which includes levee lines and protected areas, filtered by a user-provided spatial boundary.
"""

import os
import re
import time
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

    # Page size and retry behaviour for ESRI paginated queries. ESRI servers
    # often return malformed JSON on huge single-response payloads (we have
    # observed truncated bodies at ~130 MB on the NLD polygon layer). We cap
    # the per-page record count well below that and halve it on parse/HTTP
    # failure, retrying up to MAX_PAGE_RETRIES times per offset before giving
    # up on the page. MIN_PAGE_SIZE is the smallest we'll shrink to before
    # bubbling the error.
    INITIAL_PAGE_SIZE = 1000
    MIN_PAGE_SIZE = 50
    MAX_PAGE_RETRIES = 4
    RETRY_BACKOFF_S = 2.0

    def _execute_query(self, params: dict) -> gpd.GeoDataFrame:
        """Paginated ESRI Feature Service query with retry-on-truncation.

        Uses ESRI native JSON (``f=json``) and parses ``features[].geometry``
        directly into shapely instead of round-tripping through GeoJSON. This
        avoids the brittle ``gpd.read_file(resp.text)`` path that fails on
        malformed GeoJSON when the server truncates a large response mid-
        feature.

        Resilience layers, in order:
          1. Cap each page at ``INITIAL_PAGE_SIZE`` so the server never has
             to serialize a multi-hundred-MB blob in one shot.
          2. On HTTP / JSON parse failure, halve the page size and retry the
             same offset up to ``MAX_PAGE_RETRIES`` times, sleeping
             ``RETRY_BACKOFF_S * attempt`` seconds between attempts.
          3. Treat ``returnCountOnly==0`` as an empty result (no boundary
             intersection) and return immediately.
        """
        total_features = self._get_metadata(params)
        if total_features == 0:
            return gpd.GeoDataFrame()

        if self.verbose:
            log.info(f"ESRI query: {total_features} features to download")

        base_params = {**params, "f": "json"}
        results: list[gpd.GeoDataFrame] = []
        offset = 0
        page_size = self.INITIAL_PAGE_SIZE

        with tqdm(
            total=total_features, disable=not self.verbose, desc="Downloading"
        ) as pbar:
            while offset < total_features:
                page = self._fetch_page(base_params, offset, page_size)
                # _fetch_page returns the page size it actually used (after
                # shrink-on-retry) so the outer loop adopts the smaller size
                # for subsequent pages once the server has shown it can't
                # handle the larger one.
                features, used_size = page

                if not features:
                    break

                rows = []
                for feat in features:
                    geom = self._esri_geom_to_shapely(feat.get("geometry"))
                    if geom is None:
                        continue
                    attrs = feat.get("attributes", {}) or {}
                    attrs["geometry"] = geom
                    rows.append(attrs)

                if rows:
                    results.append(gpd.GeoDataFrame(rows, geometry="geometry"))

                advanced = len(features)
                offset += advanced
                pbar.update(advanced)
                page_size = used_size  # remember the working size

        if not results:
            return gpd.GeoDataFrame()
        return pd.concat(results, ignore_index=True)

    def _fetch_page(
        self, base_params: dict, offset: int, page_size: int
    ) -> tuple[list, int]:
        """Fetch one page; on failure halve page_size and retry. Returns
        (features, page_size_used).
        """
        attempt = 0
        size = page_size
        last_err: Optional[Exception] = None
        while attempt < self.MAX_PAGE_RETRIES:
            current = {
                **base_params,
                "resultOffset": offset,
                "resultRecordCount": size,
            }
            try:
                resp = requests.get(self.url, params=current, timeout=180)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise RuntimeError(
                        f"ESRI Error {data['error'].get('code')}: "
                        f"{data['error'].get('message')}"
                    )
                return data.get("features", []) or [], size
            except (
                requests.exceptions.RequestException,
                ValueError,  # json.JSONDecodeError subclass
                RuntimeError,
            ) as exc:
                last_err = exc
                attempt += 1
                new_size = max(self.MIN_PAGE_SIZE, size // 2)
                log.warning(
                    "ESRI page failed (offset=%d, size=%d, attempt=%d/%d): %s. "
                    "Retrying with size=%d after %.1fs.",
                    offset, size, attempt, self.MAX_PAGE_RETRIES, exc,
                    new_size, self.RETRY_BACKOFF_S * attempt,
                )
                if new_size == size and attempt >= self.MAX_PAGE_RETRIES:
                    break
                size = new_size
                time.sleep(self.RETRY_BACKOFF_S * attempt)
        raise RuntimeError(
            f"ESRI page failed after {self.MAX_PAGE_RETRIES} retries "
            f"(offset={offset}): {last_err}"
        )

    @staticmethod
    def _esri_geom_to_shapely(geom: Optional[dict]):
        """Translate an ESRI JSON geometry into a shapely object.

        Handles the four shapes ESRI Feature Services return for the layers
        this module queries: point, polyline (``paths``), polygon (``rings``),
        and multipoint. Returns ``None`` for empty or unsupported geometries
        so the caller can drop the row without crashing the whole page.
        """
        if not geom:
            return None
        # Polygon: ESRI uses 'rings' (outer + holes, by orientation)
        if "rings" in geom:
            rings = geom["rings"]
            if not rings:
                return None
            try:
                # First ring is exterior; remaining rings of opposite winding
                # are holes. shapely handles orientation normalization.
                exterior = rings[0]
                holes = rings[1:] if len(rings) > 1 else None
                poly = Polygon(exterior, holes=holes)
                return poly if poly.is_valid else poly.buffer(0)
            except Exception:
                return None
        # Polyline: 'paths' is a list of coordinate sequences
        if "paths" in geom:
            paths = geom["paths"]
            lines = [LineString(p) for p in paths if len(p) >= 2]
            if not lines:
                return None
            return lines[0] if len(lines) == 1 else MultiLineString(lines)
        # Point
        if "x" in geom and "y" in geom:
            from shapely.geometry import Point
            return Point(geom["x"], geom["y"])
        return None

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
