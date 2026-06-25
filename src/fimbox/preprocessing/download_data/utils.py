"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date: Jan 2026

Description: This contains small utilities modules for the NHDPlus data preprocessing.
"""

import json
import logging
import geopandas as gpd
import pandas as pd
import requests
from io import BytesIO
from pathlib import Path
from typing import Optional, Union, List
from shapely.geometry import Point, box
from shapely.ops import unary_union
import argparse

log = logging.getLogger(__name__)


class NHDBoundaryFinder:
    def __init__(self, user_boundary_path, global_boundary_path):
        """
        Immediately identifies and categorizes intersecting VPUs and RPUs.
        """
        # Load datasets
        nhd_gdf = gpd.read_file(global_boundary_path)

        if isinstance(user_boundary_path, gpd.GeoDataFrame):
            user_gdf = user_boundary_path
        else:
            user_gdf = gpd.read_file(user_boundary_path)

        # Align CRS
        if user_gdf.crs != nhd_gdf.crs:
            user_gdf = user_gdf.to_crs(nhd_gdf.crs)

        # Spatial intersection
        intersected = gpd.sjoin(nhd_gdf, user_gdf, how="inner", predicate="intersects")

        # Case-insensitive column helper
        def _get_col(df, candidates):
            for c in df.columns:
                if c.lower() in [cand.lower() for cand in candidates]:
                    return c
            return None

        unit_id_col = _get_col(intersected, ["UnitID"])
        unit_type_col = _get_col(intersected, ["UnitType"])
        drainage_col = _get_col(intersected, ["DrainageID", "DrainageAreaID"])
        unit_name_col = _get_col(intersected, ["UnitName"])

        if not unit_id_col:
            raise KeyError(f"Could not find 'UnitID' in {global_boundary_path}")

        unique_units = intersected.drop_duplicates(subset=[unit_id_col])

        self.vpus = []
        self.rpus = []

        for _, row in unique_units.iterrows():
            u_type = str(row[unit_type_col]).upper() if unit_type_col else ""
            d_id = row[drainage_col] if drainage_col else "Unknown"
            u_id = row[unit_id_col]

            if u_type == "VPU":
                u_name = (
                    str(row[unit_name_col]).replace(" ", "")
                    if unit_name_col
                    else "None"
                )
                self.vpus.append(
                    {"DrainageAreaID": d_id, "UnitID": u_id, "UnitName": u_name}
                )
            elif u_type == "RPU":
                self.rpus.append({"DrainageAreaID": d_id, "UnitID": u_id})


# Derive the headwater from the flowline data
"""
Description: Extracts headwater source points from NHDPlus flowlines.
"""


def find_headwater_points(flowlines_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Derives headwater points from a GeoDataFrame of flowlines.
    1. Explode MultiLineStrings into individual LineStrings.
    2. Collect all start points (upstream) and end points (downstream).
    3. Identify start points that never appear as an end point in the network.

    Parameters:
        flowlines_gdf (gpd.GeoDataFrame): The NHDPlus flowlines (must flow downstream).

    Returns:
        gpd.GeoDataFrame: A Point GeoDataFrame representing headwater locations.
    """
    # Ensure we are working with singlepart geometries
    flows = flowlines_gdf.explode(index_parts=True)

    starting_points = set()
    end_points = set()

    for geom in flows.geometry:
        if geom is None or geom.is_empty:
            continue

        coords = list(geom.coords)
        if len(coords) < 2:
            continue

        # NHDPlus lines are digitized in the direction of flow
        start_node = coords[0]  # Upstream
        end_node = coords[-1]  # Downstream

        starting_points.add(start_node)
        end_points.add(end_node)

    # A headwater is a starting point that is not an endpoint of any other line
    headwater_coords = [Point(sp) for sp in starting_points if sp not in end_points]

    # Create the output GeoDataFrame
    hw_gdf = gpd.GeoDataFrame({"geometry": headwater_coords}, crs=flowlines_gdf.crs)

    return hw_gdf


# HUC8 boundary lookup utility
class HUC8Finder:
    """
    Two-way HUC8 utility:
      - boundary path/GDF  --> list of intersecting HUC8 IDs (+ optional overlap %)
      - single HUC8 string --> GeoDataFrame of that watershed boundary

    Parameters
    ----------
    save : bool
        Write output to disk when True.
    out_dir : str or Path
        Directory for saved files. Defaults to current working directory.
    debug : bool
        Print request details.
    """

    _URL = (
        "https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services"
        "/HUC8_Boundaries/FeatureServer/0"
    )

    def __init__(
        self,
        save: bool = False,
        out_dir: Optional[Union[str, Path]] = None,
        debug: bool = False,
    ):
        from ...logging_utils import default_output_dir

        self.save = save
        self.out_dir = Path(out_dir) if out_dir else default_output_dir()
        self.debug = debug

    def _log(self, msg):
        if self.debug:
            log.info(f"[HUC8] {msg}")

    def _load_boundary(self, boundary, layer=None) -> gpd.GeoDataFrame:
        if isinstance(boundary, gpd.GeoDataFrame):
            return boundary
        path = Path(boundary)
        if path.suffix.lower() in (".tif", ".tiff", ".img", ".vrt"):
            import rasterio

            with rasterio.open(path) as src:
                b = src.bounds
                return gpd.GeoDataFrame(
                    {"geometry": [box(b.left, b.bottom, b.right, b.top)]}, crs=src.crs
                )
        return gpd.read_file(path, layer=layer)

    def _to_rings(self, geom) -> list:
        if geom.geom_type == "Polygon":
            return [list(geom.exterior.coords)]
        return [list(p.exterior.coords) for p in geom.geoms]

    def _post(self, params: dict) -> dict:
        self._log(f"POST {self._URL}/query")
        resp = requests.post(f"{self._URL}/query", data=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _post_geojson(self, params: dict) -> gpd.GeoDataFrame:
        self._log(f"POST (geojson) {self._URL}/query")
        resp = requests.post(f"{self._URL}/query", data=params, timeout=60)
        resp.raise_for_status()
        return gpd.read_file(BytesIO(resp.content))

    # from boundary to intersecting HUC8s
    def from_boundary(
        self,
        boundary,
        layer: Optional[str] = None,
        calc_overlap: bool = False,
    ) -> gpd.GeoDataFrame:
        """
        Return a GeoDataFrame of HUC8 polygons that intersect the boundary.
        Adds an ``overlap_pct`` column when calc_overlap=True.
        Saves to <out_dir>/intersecting_huc8.gpkg when save=True.
        """
        gdf = self._load_boundary(boundary, layer)
        geom_4326 = unary_union(gdf.to_crs(4326).geometry)
        rings = self._to_rings(geom_4326)

        params = {
            "f": "geojson",
            "where": "1=1",
            "geometry": json.dumps(
                {"rings": rings, "spatialReference": {"wkid": 4326}}
            ),
            "geometryType": "esriGeometryPolygon",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": 4326,
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
        }
        result = self._post_geojson(params)
        if result.empty:
            log.warning("No intersecting HUC8 regions found.")
            return result

        if calc_overlap:
            user_albers = gdf.to_crs(5070)
            huc_albers = result.to_crs(5070)
            total_area = unary_union(user_albers.geometry).area
            pcts = []
            for huc_geom in huc_albers.geometry:
                inter = user_albers.geometry.intersection(huc_geom).area.sum()
                pcts.append(round((inter / total_area) * 100, 2))
            result["overlap_pct"] = pcts

        if self.save:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            out_path = self.out_dir / "intersecting_huc8.gpkg"
            result.to_file(out_path, driver="GPKG", index=False)
            log.info(f"Intersecting HUC8s --> {out_path.name}")

        return result

    # from HUC8 ID to boundary geometry
    def from_huc8(self, huc8: str) -> gpd.GeoDataFrame:
        """
        Return the boundary GeoDataFrame for a single HUC8 ID.
        Saves to <out_dir>/HUC<huc8>_boundary.gpkg when save=True.
        """
        huc8 = str(huc8).zfill(8)
        params = {
            "f": "geojson",
            "where": f"HUC8 = '{huc8}'",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
        }
        result = self._post_geojson(params)
        if result.empty:
            raise ValueError(f"HUC8 '{huc8}' not found in the service.")

        if self.save:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            out_path = self.out_dir / f"HUC{huc8}_boundary.gpkg"
            result.to_file(out_path, driver="GPKG", index=False)
            log.info(f"HUC{huc8} boundary --> {out_path.name}")

        return result


def getHUC8Info(
    boundary=None,
    huc8: Optional[str] = None,
    layer: Optional[str] = None,
    calc_overlap: bool = False,
    save: bool = False,
    out_dir: Optional[Union[str, Path]] = None,
) -> gpd.GeoDataFrame:
    """
    Convenience wrapper around HUC8Finder.

    Pass boundary  --> returns intersecting HUC8 polygons.
    Pass huc8      --> returns that watershed's boundary polygon.
    calc_overlap   --> adds overlap_pct column (only with boundary mode).
    save / out_dir --> write result to disk.
    """
    if boundary is None and huc8 is None:
        raise ValueError("Provide either boundary or huc8.")
    finder = HUC8Finder(save=save, out_dir=out_dir)
    if huc8:
        return finder.from_huc8(huc8)
    return finder.from_boundary(boundary, layer=layer, calc_overlap=calc_overlap)


# CLI
if __name__ == "__main__":
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="Derive headwater points from flowlines."
    )
    parser.add_argument(
        "-i", "--input", required=True, help="Path to input flowlines (shp/gpkg)"
    )
    parser.add_argument(
        "-o", "--output", required=True, help="Path to save headwater points"
    )
    parser.add_argument("-l", "--layer", help="Layer name if using GPKG", default=None)

    args = parser.parse_args()

    input_gdf = gpd.read_file(args.input, layer=args.layer)
    result = find_headwater_points(input_gdf)

    result.to_file(args.output)
    log.info(f"Headwater points ({len(result)} features) --> {args.output}")
