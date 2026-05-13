"""
Author: Supath Dhital
Date Updated: May 2026

Compute the minimum-HAND elevation along each OSM road segment so downstream
FIMpact can flag the stage at which any portion of a road first becomes wet.

Steps
-----
1. Split the road centerlines by the crosswalked catchment polygons so each
   output row belongs to a single (osmid, HydroID) pair.
2. For each split segment, sample the HAND raster with ``rasterstats`` and
   compute the minimum non-zero HAND value. Zero is excluded because a road
   may share pixels with stream cells whose HAND is intentionally clamped to 0.
3. Within each (osmid_catchid, HydroID) group, keep only the row with the
   lowest threshold HAND — the geometry has been exploded so multi-part lines
   that get split across catchment boundaries collapse to one record per
   crossing.

Inputs
------
hand_raster     : rem_zeroed_masked_{id}.tif
roads_gpkg      : osm_roads_subset.gpkg
catchments_gpkg : gw_catchments_reaches_filtered_addedAttributes_crosswalked_{id}.gpkg

Output
------
out_csv         : osm_roads_fimpact_{id}.csv
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

import geopandas as gpd
import numpy as np
import rasterio

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def process_roads_fimpact(
    hand_raster: PathLike,
    roads_gpkg: PathLike,
    catchments_gpkg: PathLike,
    out_csv: PathLike,
) -> Path | None:
    """Run the road-FIMpact intersection. Returns ``out_csv`` or None when there's nothing to process."""
    hand_raster = Path(hand_raster)
    roads_gpkg = Path(roads_gpkg)
    catchments_gpkg = Path(catchments_gpkg)
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    if not hand_raster.exists() or not roads_gpkg.exists() or not catchments_gpkg.exists():
        log.info("process_roads_fimpact: missing input(s), skipping")
        return None

    branch_id = out_csv.parent.name

    with rasterio.open(str(hand_raster)) as hand_ds:
        hand_profile = hand_ds.profile
        hand_array = hand_ds.read(1)
        hand_crs = hand_ds.crs

    roads = gpd.read_file(str(roads_gpkg))
    if roads.empty:
        log.info("process_roads_fimpact: roads file is empty")
        return None
    if "catchment_id" in roads.columns:
        roads = roads.drop(columns=["catchment_id"])
    if roads.crs != hand_crs:
        roads = roads.to_crs(hand_crs)

    catchments = gpd.read_file(str(catchments_gpkg), engine="fiona")
    keep_cols = [
        c for c in ("HydroID", "feature_id", "order_") if c in catchments.columns
    ]
    catchments = catchments[keep_cols + ["geometry"]]
    if catchments.crs != hand_crs:
        catchments = catchments.to_crs(hand_crs)

    catchments["HydroID"] = catchments["HydroID"].astype(int).astype(str)
    if "feature_id" in catchments.columns:
        catchments["feature_id"] = catchments["feature_id"].astype(int).astype(str)

    split = gpd.overlay(roads, catchments, how="intersection").explode(
        index_parts=True
    ).reset_index(drop=True)
    if split.empty:
        log.info("process_roads_fimpact: no road–catchment intersections for branch %s", branch_id)
        return None

    split["branch"] = branch_id

    try:
        from rasterstats import zonal_stats
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "process_roads_fimpact requires the 'rasterstats' package. "
            "Install with: pip install rasterstats"
        ) from exc
    stats = zonal_stats(
        split["geometry"],
        hand_array,
        affine=hand_profile["transform"],
        nodata=hand_profile.get("nodata"),
        all_touched=True,
        stats=[],
        add_stats={"min_ex0": _min_hand_excluding_zero},
    )
    split["threshold_hand"] = [s.get("min_ex0") for s in stats]
    split = split.dropna(subset=["threshold_hand"])
    if split.empty:
        log.info("process_roads_fimpact: no roads with valid HAND values")
        return None

    # Per (osmid_catchid, HydroID) keep the segment with the lowest threshold.
    if "osmid_catchid" in split.columns:
        min_idx = split.groupby(["osmid_catchid", "HydroID"])["threshold_hand"].idxmin()
        split = split.loc[min_idx]

    # ``huc8`` / ``aoi_code`` may or may not be present depending on the input
    # OSM roads source; we coerce whichever IDs exist to string for the CSV.
    str_cols = [
        c for c in ("osmid", "huc8", "aoi_code", "HydroID", "feature_id", "branch")
        if c in split.columns
    ]
    split[str_cols] = split[str_cols].astype(str)

    split = split.drop(columns="geometry")
    split.to_csv(str(out_csv), index=False)
    log.info("process_roads_fimpact: wrote %d rows → %s", len(split), out_csv.name)
    return out_csv


def _min_hand_excluding_zero(values) -> float:
    """Min over the zonal stats values, dropping nodata and 0 cells."""
    data = np.ma.filled(values.astype(float), np.nan)
    valid = data[(data != 0) & (~np.isnan(data))]
    return float(np.min(valid)) if valid.size > 0 else np.nan
