"""
Author: Supath Dhital
Date Updated: May 2026

FEMA NFHL floodplain adjustment for branch-level burned DEMs.

Port of inundation-mapping's ``adjust_floodplains.py``. After AGREE DEM
burning, this step lowers DEM elevations *inside* the upstream catchment
polygon of a branch's levelpaths, with the depression shape controlled by
Euclidean distance from the burned stream raster. Areas mapped as FEMA
flood zones (NFHL ``combined`` layer) are excluded so the adjustment does
not double-count regulatory floodplain depressions.

Math
----
For each pixel inside the upstream-catchment polygon and within
``distance_threshold`` metres of a burned stream cell, the adjustment is:

    adj = ((threshold - dist) / threshold) ** slope_exponent * z_factor
    new_dem = dem - adj

i.e. the maximum drop ``z_factor`` is applied at the stream, decaying to
zero at ``distance_threshold`` according to a power-law (``slope_exponent``).

Inputs
------
input_file (flows_grid_boolean_{id}.tif)
    1/0 burned stream raster; source for Euclidean distance.
dem_file (dem_burned_{id}.tif)
    AGREE-burned DEM; the raster being adjusted.
nwm_catchments (nwm_catchments_proj_subset.gpkg)
    AOI-wide NWM catchments (with ``ID`` column).
nwm_streams (nwm_subset_streams.gpkg)
    AOI-wide NWM streams (with ``ID`` and ``to`` for upstream walk).
nwm_levelpaths (nwm_subset_streams_levelPaths.gpkg)
    Levelpath-attributed streams (with ``ID`` and ``levpa_id``).
branch_polygons (branch_polygons.gpkg)
    Per-branch processing polygons from BranchDerivation.
fema_flood_zones_file (nfhl_{aoi}.gpkg)
    FEMA NFHL geopackage. ``combined`` layer = mapped flood zones; optional
    ``availability`` layer = areas where NFHL exists (mapped or not).

Outputs
-------
distance_file (flows_grid_boolean_euclidean_distance_{id}.tif)
    Distance-to-stream raster (m), masked to the area being adjusted.
output_file (dem_burned_adjusted_{id}.tif)
    Adjusted DEM ready for pit-fill.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio as rio
from rasterio.mask import mask
from shapely.geometry import mapping

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def adjust_floodplains(
    input_file: PathLike,
    dem_file: PathLike,
    nwm_catchments: PathLike,
    nwm_streams: PathLike,
    nwm_levelpaths: PathLike,
    distance_file: PathLike,
    output_file: PathLike,
    distance_threshold: float,
    slope_exponent: float,
    z_factor: float,
    branch_polygons: PathLike,
    branch_id: str,
    fema_flood_zones_file: Optional[PathLike] = None,
    fema_flood_zones_layer: str = "combined",
    wbt_path: Optional[str] = None,
) -> Optional[Path]:
    """Adjust DEM elevations inside the branch's upstream catchment using a
    distance-decayed depression. Returns ``output_file`` on success or
    ``None`` when there is no area to adjust (e.g. all branch overlaps a
    mapped FEMA flood zone)."""

    # WhiteboxTools is only needed for the Euclidean distance pass — import
    # lazily so the rest of fimbox does not require it.
    import whitebox

    wbt = whitebox.WhiteboxTools()
    wbt.set_verbose_mode(False)
    wbt.set_whitebox_dir(wbt_path or os.environ.get("WBT_PATH", ""))

    input_file = str(input_file)
    dem_file = str(dem_file)
    distance_file = Path(distance_file)
    output_file = Path(output_file)
    branch_id = str(branch_id)

    wbt.set_working_dir(str(distance_file.parent))
    wbt.euclidean_distance(input_file, str(distance_file))

    catchments = gpd.read_file(nwm_catchments)
    streams = gpd.read_file(nwm_streams)
    levelpaths = gpd.read_file(nwm_levelpaths)
    branch_polys = gpd.read_file(branch_polygons)
    branch_poly = branch_polys[branch_polys["levpa_id"].astype(str) == branch_id]

    levelpaths["ID"] = levelpaths["ID"].astype(int)
    levelpaths = levelpaths[levelpaths["levpa_id"].astype(str) == branch_id]
    if levelpaths.empty:
        log.warning(f"adjust_floodplains: no levelpaths for branch {branch_id}")
        return None

    seed_ids = levelpaths["ID"].tolist()
    upstream_streams = _get_upstream_streams(seed_ids, streams)
    upstream_catchments = catchments[
        catchments["ID"].isin(upstream_streams["ID"].tolist())
    ].dissolve()

    if upstream_catchments.empty:
        log.warning(f"adjust_floodplains: no upstream catchments for branch {branch_id}")
        return None

    with rio.open(distance_file) as src, rio.open(dem_file) as dem_src:
        profile = src.profile
        distance = src.read(1)
        dem = dem_src.read(1)
        dem_nodata = dem_src.nodata

        distance_mask = _build_distance_mask(
            src, distance, branch_poly, fema_flood_zones_file, fema_flood_zones_layer
        )
        if distance_mask is None:
            log.info(
                f"adjust_floodplains branch {branch_id}: FEMA flood zones cover "
                f"the entire branch — no adjustment applied."
            )
            return None

        # Restrict to inside the upstream catchment
        geometries = [mapping(geom) for geom in upstream_catchments.geometry]
        distance_clipped, _ = mask(src, geometries, crop=False, nodata=np.nan)
        distance_clipped = distance_clipped[0]

    distance = np.where(
        ~np.isnan(distance_clipped) & (distance_mask <= distance_threshold),
        distance_mask,
        np.nan,
    )

    with rio.open(distance_file, "w", **profile) as dst:
        dst.write(distance.astype(rio.float32), 1)

    adjustment = np.where(
        distance < distance_threshold,
        ((distance_threshold - distance) / distance_threshold) ** slope_exponent * z_factor,
        0,
    )
    adjustment[np.isnan(adjustment)] = 0
    adjustment[np.isnan(dem)] = np.nan

    new_dem = dem - adjustment
    new_dem[new_dem < -5000] = dem_nodata

    profile.update(dtype=rio.float32, nodata=dem_nodata)
    with rio.open(output_file, "w", **profile) as dst:
        dst.write(new_dem.astype(rio.float32), 1)

    log.info(f"Adjusted DEM (branch {branch_id}) --> {output_file.name}")
    return output_file


def _get_upstream_streams(hydro_ids, streams_df):
    """Recursive walk: collect every stream draining (directly or transitively)
    into one of the seed IDs."""
    upstream = streams_df[streams_df["ID"].isin(hydro_ids)]
    for hid in hydro_ids:
        direct = streams_df[streams_df["to"] == hid]
        if not direct.empty:
            upstream = pd.concat([upstream, direct])
            upstream = pd.concat(
                [upstream, _get_upstream_streams(direct["ID"].tolist(), streams_df)]
            )
    return upstream.drop_duplicates()


def _build_distance_mask(
    src,
    distance,
    branch_poly,
    fema_flood_zones_file,
    fema_flood_zones_layer,
):
    """Mask the distance raster to areas where the floodplain adjustment is
    valid: outside FEMA-mapped flood zones but inside FEMA availability.
    Returns ``None`` when nothing remains to be adjusted."""
    if not fema_flood_zones_file or not Path(fema_flood_zones_file).exists():
        return distance

    layers = gpd.list_layers(fema_flood_zones_file)["name"].tolist()
    availability = gpd.read_file(fema_flood_zones_file, layer="combined")

    if "availability" in layers:
        avail = gpd.read_file(fema_flood_zones_file, layer="availability")
        availability = (
            pd.concat([availability, avail])
            .drop_duplicates(subset="geometry", keep=False)
            .dissolve()
        )

    if fema_flood_zones_layer in layers:
        zones = gpd.read_file(fema_flood_zones_file, layer=fema_flood_zones_layer)
        availability = gpd.overlay(availability, zones, how="difference")

    availability = gpd.clip(availability, branch_poly)
    if availability.empty:
        return None

    geometries = [mapping(geom) for geom in availability.geometry]
    masked, _ = mask(src, geometries, crop=False, nodata=np.nan, invert=True)
    return masked[0]


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(description="Adjust burned DEM using NFHL floodplains.")
    parser.add_argument("-i", "--input-file", required=True)
    parser.add_argument("-e", "--distance-file", required=True)
    parser.add_argument("-d", "--dem-file", required=True)
    parser.add_argument("-o", "--output-file", required=True)
    parser.add_argument("-t", "--distance-threshold", type=float, required=True)
    parser.add_argument("-s", "--slope-exponent", type=float, required=True)
    parser.add_argument("-z", "--z-factor", type=float, required=True)
    parser.add_argument("-p", "--branch-polygons", required=True)
    parser.add_argument("-b", "--branch-id", required=True)
    parser.add_argument("-f", "--fema-flood-zones-file", default=None)
    parser.add_argument("-l", "--fema-flood-zones-layer", default="combined")
    parser.add_argument("-c", "--nwm-catchments", required=True)
    parser.add_argument("-n", "--nwm-streams", required=True)
    parser.add_argument("-lp", "--nwm-levelpaths", required=True)
    args = parser.parse_args()
    adjust_floodplains(**vars(args))
