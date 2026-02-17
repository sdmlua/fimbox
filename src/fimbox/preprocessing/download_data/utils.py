"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date: Jan 2026

Description: This contains small utilities modules for the NHDPlus data preprocessing.
"""

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
import argparse


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


# CLI support for standalone testing
if __name__ == "__main__":
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
    print(f"Successfully derived {len(result)} headwater points to {args.output}")
