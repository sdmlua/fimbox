"""
Author: Supath Dhital
Date Updated: May 2026


Branch processing tests.

Run order:
  1. test_branch_derivation  — level paths, branch polygons, branch list
  2. test_branch_zero_full   — DEM clip, AGREE, pit-fill, D8 flowdir
  3. test_create_hand        — full HAND generation (flow accum → split reaches)
"""

import logging
import os
from pathlib import Path

import pytest

# single steps IMPORTS
from fimbox import (
    BranchDerivation,
    BranchZero,
    CreateHAND,
)

log = logging.getLogger(__name__)

# imports used only by the B-series CreateHAND step tests below.
# BranchZero substeps (StreamBooleanRasterizer, HydroenforceDEM, FlowdirDEM,
# HeadwaterRasterizer, LevelPathBooleanRasterizer, rasterize_3d_levee_lines,
# burn_levee_elevations) are exercised indirectly via test_step_Z1, so they
# are not imported here.
from fimbox import (
    FlowAccDEM,
    ThalwegAdjustment,
    D8SlopeDEM,
    StreamNetReaches,
    split_derived_reaches,
)

# AOI parameters — point this at any user-supplied AOI working directory.
# aoi_code is recorded on every hydroTable row; a generic string is fine
# (HUC IDs work unchanged for legacy datasets).
OUT_DIR = Path("/Users/Supath/Downloads/SDML/FIMBOX/out/test_smallB")
AOI_CODE = "08060202"

# Tunable CreateHAND parameters- All have sensible defaults in CreateHAND itself.
PARAMS_CREATE_HAND = dict(
    cost_distance_tolerance=50.0,  # m, lateral cost distance
    lateral_elevation_threshold=3,  # m, lateral thalweg drop cap
    max_split_distance_m=2000.0,  # m, split-reach max length
    slope_min=0.0001,  # rise/run floor
    lakes_buffer_dist_m=100.0,  # m, lake-boundary buffer
    # SRC / crosswalk
    mannings_n=0.06,  # channel roughness
    stage_min_m=0.0,  # SRC stage ladder start
    stage_interval_m=0.3048,  # SRC stage step (1 ft)
    stage_max_m=25.2984,  # SRC stage ladder end (~83 ft)
    min_catchment_area=0.25,  # km^2, short-reach replace threshold
    min_stream_length=0.5,  # km, short-reach replace threshold
    crosswalk_max_distance_m=100.0,  # m, midpoint-to-NWM-flowline cap
)

DEM = OUT_DIR / "dem.tif"
STREAMS = OUT_DIR / "nwm_subset_streams.gpkg"
BOUNDARY_BUF = OUT_DIR / "wbd_buffered.gpkg"
CATCHMENTS = OUT_DIR / "nwm_catchments_proj_subset.gpkg"
HEADWATERS = (
    OUT_DIR / "nwm_headwater_points_subset.gpkg"
    if (OUT_DIR / "nwm_headwater_points_subset.gpkg").is_file()
    else OUT_DIR / "nwm_headwaters.gpkg"
)
LEVELPATH_EXT = OUT_DIR / "nwm_subset_streams_levelPaths_extended.gpkg"
BRIDGE_DIFF = OUT_DIR / "bridge_elev_diff.tif"
NLD_LEVEES = OUT_DIR / "3d_nld_subset_levees_burned.gpkg"

# optional files
WBD8_CLP = OUT_DIR / "wbd8_clp.gpkg"
LAKES = OUT_DIR / "nwm_lakes_proj_subset.gpkg"
LEVEE_AREAS = OUT_DIR / "LeveeProtectedAreas_subset.gpkg"
LEVEE_LP_CSV = OUT_DIR / "levee_levelpaths.csv"

# branch-zero derived paths
BRANCH_DIR = OUT_DIR / "branches" / "0"
BRANCH_ID = "0"
DEM_BRANCH = BRANCH_DIR / f"dem_{BRANCH_ID}.tif"
FLOWDIR = BRANCH_DIR / f"flowdir_d8_burned_filled_{BRANCH_ID}.tif"
HW_RASTER = BRANCH_DIR / f"headwaters_{BRANCH_ID}.tif"
STREAM_BOOL = BRANCH_DIR / f"flows_grid_boolean_{BRANCH_ID}.tif"
DEM_BURNED = BRANCH_DIR / f"dem_burned_{BRANCH_ID}.tif"
DEM_FILLED = BRANCH_DIR / f"dem_burned_filled_{BRANCH_ID}.tif"

# REM + filtered catchment paths
REM = BRANCH_DIR / f"rem_{BRANCH_ID}.tif"
REM_ZEROED = BRANCH_DIR / f"rem_zeroed_masked_{BRANCH_ID}.tif"
CATCH_POLY = BRANCH_DIR / f"gw_catchments_reaches_{BRANCH_ID}.gpkg"
FILT_CATCH = (
    BRANCH_DIR / f"gw_catchments_reaches_filtered_addedAttributes_{BRANCH_ID}.gpkg"
)
FILT_FLOWS = BRANCH_DIR / f"demDerived_reaches_split_filtered_{BRANCH_ID}.gpkg"
FILT_TIF = (
    BRANCH_DIR / f"gw_catchments_reaches_filtered_addedAttributes_{BRANCH_ID}.tif"
)

# SRC / crosswalk / hydroTable outputs (steps 16-21)
SLOPES_MASKED = BRANCH_DIR / f"slopes_d8_dem_meters_masked_{BRANCH_ID}.tif"
STAGE_TXT = BRANCH_DIR / f"stage_{BRANCH_ID}.txt"
CATCHLIST_TXT = BRANCH_DIR / f"catch_list_{BRANCH_ID}.txt"
SRC_BASE_CSV = BRANCH_DIR / f"src_base_{BRANCH_ID}.csv"
XWALK_CATCH = (
    BRANCH_DIR
    / f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{BRANCH_ID}.gpkg"
)
XWALK_FLOWS = (
    BRANCH_DIR
    / f"demDerived_reaches_split_filtered_addedAttributes_crosswalked_{BRANCH_ID}.gpkg"
)
SRC_FULL_CSV = BRANCH_DIR / f"src_full_crosswalked_{BRANCH_ID}.csv"
SRC_JSON = BRANCH_DIR / f"src_{BRANCH_ID}.json"
XWALK_CSV = BRANCH_DIR / f"crosswalk_table_{BRANCH_ID}.csv"
HYDRO_TABLE = BRANCH_DIR / f"hydroTable_{BRANCH_ID}.csv"
ROADS_CSV = BRANCH_DIR / f"osm_roads_fimpact_{BRANCH_ID}.csv"
BRIDGES_GPKG = BRANCH_DIR / f"osm_bridge_centroids_{BRANCH_ID}.gpkg"

# HAND generation derived paths
FLOWACCUM = BRANCH_DIR / f"flowaccum_d8_burned_filled_{BRANCH_ID}.tif"
STREAM_PIX = BRANCH_DIR / f"demDerived_streamPixels_{BRANCH_ID}.tif"
THALWEG_ADJ = BRANCH_DIR / f"dem_lateral_thalweg_adj_{BRANCH_ID}.tif"
FLOWDIR_STR = BRANCH_DIR / f"flowdir_d8_burned_filled_flows_{BRANCH_ID}.tif"
THALWEG_COND = BRANCH_DIR / f"dem_thalwegCond_{BRANCH_ID}.tif"
SLOPES_D8 = BRANCH_DIR / f"slopes_d8_dem_{BRANCH_ID}.tif"
STREAM_ORDER = BRANCH_DIR / f"streamOrder_{BRANCH_ID}.tif"
SN_CATCH = BRANCH_DIR / f"sn_catchments_reaches_{BRANCH_ID}.tif"
DEM_REACHES = BRANCH_DIR / f"demDerived_reaches_{BRANCH_ID}.gpkg"
SPLIT_REACHES = BRANCH_DIR / f"demDerived_reaches_split_{BRANCH_ID}.gpkg"
SPLIT_PTS = BRANCH_DIR / f"demDerived_reaches_split_points_{BRANCH_ID}.gpkg"
GW_REACHES = BRANCH_DIR / f"gw_catchments_reaches_{BRANCH_ID}.tif"
PIXEL_PTS = BRANCH_DIR / f"flows_points_pixels_{BRANCH_ID}.gpkg"
GW_PIXELS = BRANCH_DIR / f"gw_catchments_pixels_{BRANCH_ID}.tif"


# # Single steps — run in the file order so each step's output feeds the next.
# def test_branch_derivation():
#     """Derive level paths, branch polygons, and branch list from staged NWM data."""
#     result = BranchDerivation(
#         out_dir=OUT_DIR,
#         branch_id_attribute="levpa_id",
#         reach_id_attribute="ID",
#         branch_buffer_distance_meters=1000.0,
#     ).run()

#     assert result.dissolved_levelpaths.exists(), "dissolved levelpaths not written"
#     assert result.branch_polygons.exists(), "branch polygons not written"
#     assert result.branch_list.exists(), "branch list file not written"
#     assert len(result.branch_dataframe) > 0, "branch dataframe is empty"

#     log.info(f"Level paths     --> {result.levelpaths}")
#     log.info(f"Dissolved       --> {result.dissolved_levelpaths}")
#     log.info(f"Branch polygons --> {result.branch_polygons}")
#     log.info(f"Branch list     --> {result.branch_list}")
#     log.info(f"Branch count: {len(result.branch_dataframe)}")


# def test_branch_zero_full():
#     """Run the complete branch-zero preprocessing pipeline."""
#     outputs = BranchZero(
#         dem_path=DEM,
#         streams_gpkg=STREAMS,
#         boundary_gpkg=BOUNDARY_BUF,
#         out_dir=OUT_DIR,
#         bridge_elev_diff_path=BRIDGE_DIFF if BRIDGE_DIFF.exists() else None,
#         headwaters_gpkg=HEADWATERS if HEADWATERS.exists() else None,
#         levelpaths_extended_gpkg=LEVELPATH_EXT if LEVELPATH_EXT.exists() else None,
#         agree_buffer_m=15.0,
#         agree_smooth_drop=10.0,
#         agree_sharp_drop=1000.0,
#         branch_zero_id=BRANCH_ID,
#     ).run()

#     assert outputs["dem"].exists(), "clipped DEM not written"
#     assert outputs["flows_grid_boolean"].exists(), "stream boolean grid not written"
#     assert outputs["dem_burned"].exists(), "AGREE DEM not written"
#     assert outputs["dem_burned_filled"].exists(), "pit-filled DEM not written"
#     assert outputs["flowdir_d8"].exists(), "D8 flow direction not written"

#     log.info(f"Branch-zero outputs ({len(outputs)} files):")
#     for k, v in outputs.items():
#         log.info(f"  {k:35s} --> {v.name}")


# def test_create_hand():
#     """
#     Run the full HAND generation pipeline (22 steps) for branch zero.
#     All standard input paths are resolved from OUT_DIR and BRANCH_DIR automatically.
#     Tunable parameters live in ``PARAMS_CREATE_HAND`` at the top of this file.
#     Requires: test_branch_zero_full outputs to exist.
#     """
#     assert DEM_BRANCH.exists(), f"Run test_branch_zero_full first — {DEM_BRANCH} missing"
#     assert FLOWDIR.exists(), f"flowdir missing — run test_branch_zero_full first"

#     outputs = CreateHAND(
#         aoi_dir=OUT_DIR,
#         branch_dir=BRANCH_DIR,
#         branch_id=BRANCH_ID,
#         aoi_code=AOI_CODE,
#         levee_protected_areas_gpkg=LEVEE_AREAS if LEVEE_AREAS.exists() else None,
#         levee_levelpaths_csv=LEVEE_LP_CSV if LEVEE_LP_CSV.exists() else None,
#         lakes_gpkg=LAKES if LAKES.exists() else None,
#         boundary_gpkg=WBD8_CLP if WBD8_CLP.exists() else None,
#         **PARAMS_CREATE_HAND,
#     ).run()

#     log.info(f"CreateHAND outputs ({len(outputs)} files):")
#     for k, v in outputs.items():
#         log.info(f"  {k:35s} --> {v.name}  {'ok' if v.exists() else 'MISSING'}")

#     # HAND base outputs
#     assert THALWEG_COND.exists(), "dem_thalwegCond not produced"
#     assert SLOPES_D8.exists(), "slopes_d8 not produced"
#     assert DEM_REACHES.exists(), "demDerived_reaches not produced"
#     assert SPLIT_REACHES.exists(), "demDerived_reaches_split not produced"
#     assert SPLIT_PTS.exists(), "demDerived_reaches_split_points not produced"
#     assert GW_REACHES.exists(), "gw_catchments_reaches not produced"
#     assert PIXEL_PTS.exists(), "flows_points_pixels not produced"
#     assert GW_PIXELS.exists(), "gw_catchments_pixels not produced"
#     assert REM.exists(), "rem not produced"
#     assert REM_ZEROED.exists(), "rem_zeroed_masked not produced"
#     assert CATCH_POLY.exists(), "gw_catchments_reaches gpkg not produced"
#     assert FILT_CATCH.exists(), "filtered catchments not produced"
#     assert FILT_FLOWS.exists(), "filtered flows not produced"
#     assert FILT_TIF.exists(), "filtered catchments tif not produced"

#     # SRC, crosswalk with hydroTable
#     assert SLOPES_MASKED.exists(), "slopes mask not produced"
#     assert STAGE_TXT.exists(), "stage list not produced"
#     assert CATCHLIST_TXT.exists(), "catchlist file not produced"
#     assert SRC_BASE_CSV.exists(), "SRC base CSV not produced"
#     assert XWALK_CATCH.exists(), "crosswalked catchments not produced"
#     assert XWALK_FLOWS.exists(), "crosswalked flows not produced"
#     assert SRC_FULL_CSV.exists(), "SRC full CSV not produced"
#     assert SRC_JSON.exists(), "SRC JSON not produced"
#     assert XWALK_CSV.exists(), "crosswalk table not produced"
#     assert HYDRO_TABLE.exists(), "hydroTable not produced"

#     # hydroTable sanity: required columns + monotonic stage→discharge per HydroID
#     import pandas as pd

#     ht = pd.read_csv(
#         HYDRO_TABLE,
#         dtype={"aoi_code": str, "HydroID": str, "feature_id": str},
#     )
#     required = {
#         "HydroID", "feature_id", "aoi_code",
#         "stage", "discharge_cms", "ManningN", "SLOPE", "LENGTHKM",
#     }
#     missing = required - set(ht.columns)
#     assert not missing, f"hydroTable missing columns: {missing}"
#     assert (ht["stage"] >= 0).all(), "negative stage values in hydroTable"
#     assert (ht["discharge_cms"] >= 0).all(), "negative discharge in hydroTable"
#     aoi_unique = set(ht["aoi_code"].unique())
#     assert AOI_CODE in aoi_unique or any(
#         AOI_CODE in v for v in aoi_unique
#     ), f"aoi_code column {aoi_unique} does not contain test AOI_CODE={AOI_CODE!r}"
#     log.info(
#         f"hydroTable rows: {len(ht)}, HydroIDs: {ht['HydroID'].nunique()}, "
#         f"feature_ids: {ht['feature_id'].nunique()}, "
#         f"aoi_code: {sorted(aoi_unique)}"
#     )


# =============================================================================
# INDIVIDUAL STEP-BY-STEP TESTS
# Running these in file order rebuilds the full per-branch pipeline
# Layers:
#   Z0        BranchDerivation             — level paths + branch_list.csv
#   Z1        BranchZero                   — DEM clip + AGREE + pit-fill + D8
#                                            (wraps stream raster, headwater
#                                             raster, optional levelpath raster,
#                                             optional levee burn, AGREE,
#                                             pit-fill, flowdir)
#   B02..B21  CreateHAND steps 2-21        — one test per CreateHAND step


# Stage Z — bootstrap. Together they produce every input the B-series tests need.
def test_step_Z0_branch_derivation():
    """Derive level paths, branch polygons, and branch list from staged NWM data."""
    result = BranchDerivation(
        out_dir=OUT_DIR,
        branch_id_attribute="levpa_id",
        reach_id_attribute="ID",
        branch_buffer_distance_meters=1000.0,
    ).run()
    assert result.dissolved_levelpaths.exists(), "dissolved levelpaths not written"
    assert result.branch_polygons.exists(), "branch polygons not written"
    assert result.branch_list.exists(), "branch list file not written"
    assert len(result.branch_dataframe) > 0, "branch dataframe is empty"
    log.info(f"branch count: {len(result.branch_dataframe)}")


def test_step_Z1_branch_zero_full():
    """Run BranchZero: DEM clip, stream rasterize, optional headwater/levelpath/levee
    rasters, AGREE conditioning, pit-fill, and D8 flowdir for branch 0.

    This single call wraps the substeps BranchZero already folds together
    (StreamBooleanRasterizer, HeadwaterRasterizer, optional
    LevelPathBooleanRasterizer, optional rasterize_3d_levee_lines +
    burn_levee_elevations, HydroenforceDEM, WhiteboxTools pit-fill,
    FlowdirDEM). Calling the substeps individually would duplicate work the
    class already orchestrates correctly.
    """
    outputs = BranchZero(
        dem_path=DEM,
        streams_gpkg=STREAMS,
        boundary_gpkg=BOUNDARY_BUF,
        out_dir=OUT_DIR,
        bridge_elev_diff_path=BRIDGE_DIFF if BRIDGE_DIFF.exists() else None,
        levee_gpkg_path=NLD_LEVEES if NLD_LEVEES.exists() else None,
        headwaters_gpkg=HEADWATERS if HEADWATERS.exists() else None,
        levelpaths_extended_gpkg=LEVELPATH_EXT if LEVELPATH_EXT.exists() else None,
        agree_buffer_m=15.0,
        agree_smooth_drop=10.0,
        agree_sharp_drop=1000.0,
        branch_zero_id=BRANCH_ID,
    ).run()
    for key, p in outputs.items():
        log.info(f"  {key:35s} --> {p.name}")
    assert DEM_BRANCH.exists(), "dem_0.tif missing"
    assert STREAM_BOOL.exists(), "flows_grid_boolean_0.tif missing"
    assert DEM_BURNED.exists(), "dem_burned_0.tif missing"
    assert DEM_FILLED.exists(), "dem_burned_filled_0.tif missing"
    assert FLOWDIR.exists(), "flowdir_d8_burned_filled_0.tif missing"


# Stage B — CreateHAND steps 2..21, one isolated test each.
def test_step_B02_flow_accumulation():
    """CreateHAND step 2: D8 flow accumulation + stream-pixel mask."""
    assert FLOWDIR.exists(), "FLOWDIR missing — run step_A6 first"
    if not HW_RASTER.exists():
        log.warning("skipping flow accumulation — no headwater raster")
        return
    fa_out, sp_out = FlowAccDEM(
        flowdir=FLOWDIR,
        headwaters=HW_RASTER,
        out_flowaccum=FLOWACCUM,
        out_stream_pixels=STREAM_PIX,
        threshold=1.0,
    ).run()
    import rasterio

    with rasterio.open(str(sp_out)) as src:
        stream_count = int((src.read(1) == 1).sum())
    log.info(f"stream pixels: {stream_count}")
    assert fa_out.exists() and sp_out.exists() and stream_count > 0


def test_step_B03_thalweg_adjustment():
    """CreateHAND step 3: lateral thalweg minimum + flow-conditioned DEM."""
    for p in (DEM_BRANCH, STREAM_PIX, FLOWDIR):
        assert p.exists(), f"missing: {p}"
    result = ThalwegAdjustment(
        dem=DEM_BRANCH,
        stream_pixels=STREAM_PIX,
        flowdir=FLOWDIR,
        out_thalweg_adj=THALWEG_ADJ,
        out_flowdir_streams=FLOWDIR_STR,
        out_thalweg_cond=THALWEG_COND,
        cost_distance_tolerance=50.0,
        lateral_elevation_threshold=3,
    ).run()
    assert result["thalweg_adj"].exists() and result["thalweg_cond"].exists()


def test_step_B04_d8_slopes():
    """CreateHAND step 4: D8 slope raster (rise/run from thalweg-adjusted DEM)."""
    assert THALWEG_ADJ.exists() and FLOWDIR.exists()
    import numpy as np, rasterio

    out = D8SlopeDEM(
        dem=THALWEG_ADJ, flowdir=FLOWDIR, out_path=SLOPES_D8, slope_min=0.0001
    ).run()
    with rasterio.open(str(out)) as src:
        d = src.read(1)
        nd = src.nodata
        valid = d[(d != nd) & np.isfinite(d)] if nd is not None else d[np.isfinite(d)]
    log.info(f"slope range: [{valid.min():.6f}, {valid.max():.6f}]")
    # slope_min is clamped at 1e-4 in float32; allow a single-precision epsilon
    # of tolerance (~1e-7) so the test doesn't fail on the float32 representation
    # of 1e-4 (which is 9.9999997e-05).
    assert float(valid.min()) >= 0.0001 - 1e-7


def test_step_B05_streamnet_reaches():
    """CreateHAND step 5: vectorise stream network into reach polylines."""
    for p in (FLOWDIR, THALWEG_COND, FLOWACCUM, STREAM_PIX):
        assert p.exists(), f"missing: {p}"
    result = StreamNetReaches(
        flowdir=FLOWDIR,
        dem_thalweg_cond=THALWEG_COND,
        flowaccum=FLOWACCUM,
        stream_pixels=STREAM_PIX,
        out_dir=BRANCH_DIR,
        branch_id=BRANCH_ID,
    ).run()
    import geopandas as gpd

    reaches = gpd.read_file(str(result["demDerived_reaches"]))
    log.info(f"reaches: {len(reaches)}")
    assert len(reaches) > 0


def test_step_B06_split_reaches():
    """CreateHAND step 6: split reaches at length limit + lake boundaries."""
    for p in (DEM_REACHES, THALWEG_COND, STREAMS):
        assert p.exists(), f"missing: {p}"
    split_gpkg, pts_gpkg = split_derived_reaches(
        reaches_gpkg=DEM_REACHES,
        dem_thalweg_cond=THALWEG_COND,
        nwm_streams_gpkg=STREAMS,
        out_split_gpkg=SPLIT_REACHES,
        out_points_gpkg=SPLIT_PTS,
        wbd8_clp_gpkg=WBD8_CLP if WBD8_CLP.exists() else None,
        lakes_gpkg=LAKES if LAKES.exists() else None,
        max_length=2000.0,
        slope_min=0.0001,
        lakes_buffer_dist=100.0,
    )
    import geopandas as gpd

    split = gpd.read_file(str(split_gpkg))
    log.info(f"split reaches: {len(split)} columns={list(split.columns)}")
    assert (
        len(split) > 0 and "HydroID" in split.columns and "NextDownID" in split.columns
    )


def test_step_B07_gage_watershed_reaches():
    """CreateHAND step 7: reverse-D8 walk labelling each pixel by its HydroID."""
    from fimbox import GageCatchments

    for p in (FLOWDIR, SPLIT_PTS):
        assert p.exists(), f"missing: {p}"
    GageCatchments(
        flowdir=FLOWDIR,
        outlet_points=SPLIT_PTS,
        out_path=GW_REACHES,
    ).run()
    assert GW_REACHES.exists()


def test_step_B08_stream_pixel_points():
    """CreateHAND step 8: vectorise stream-pixel centroids (one point per stream pixel)."""
    from fimbox import stream_pixel_points

    assert STREAM_PIX.exists()
    stream_pixel_points(stream_pixels=STREAM_PIX, out_gpkg=PIXEL_PTS)
    assert PIXEL_PTS.exists()


def test_step_B09_gage_watershed_pixels():
    """CreateHAND step 9: reverse-D8 walk labelling each pixel by NWM feature_id."""
    from fimbox import GageCatchments

    for p in (FLOWDIR, PIXEL_PTS):
        assert p.exists(), f"missing: {p}"
    GageCatchments(
        flowdir=FLOWDIR,
        outlet_points=PIXEL_PTS,
        out_path=GW_PIXELS,
    ).run()
    assert GW_PIXELS.exists()


def test_step_B10_outlet_backpool_mitigation():
    """CreateHAND step 10: trim oversized outlet catchments (no-op for branch 0)."""
    from fimbox import OutletBackpoolMitigate

    for p in (SPLIT_REACHES, GW_PIXELS, GW_REACHES, SPLIT_PTS, STREAMS, THALWEG_COND):
        assert p.exists(), f"missing: {p}"
    OutletBackpoolMitigate(
        branch_dir=BRANCH_DIR,
        catchment_pixels_path=GW_PIXELS,
        catchment_reaches_path=GW_REACHES,
        split_flows_gpkg=SPLIT_REACHES,
        split_points_gpkg=SPLIT_PTS,
        nwm_streams_gpkg=STREAMS,
        dem_path=THALWEG_COND,
        slope_min=0.0001,
    ).run()
    # No new file is asserted — backpool mitigation modifies the existing
    # gw_catchments_pixels/reaches rasters in place for non-zero branches only.
    assert GW_PIXELS.exists() and GW_REACHES.exists()


def test_step_B11_make_rem():
    """CreateHAND step 11: HAND = pixel_elev - nearest_stream_pixel_elev.

    Note: the raw REM **can** be negative (pixels lower than the nearest
    downstream stream pixel — happens near floodplain edges and where the
    D8 walk crosses meander cutoffs). Negative values get clipped to zero
    in step 12 (``rem_zeroed_masked``). This test only asserts the raster
    was produced and contains finite values — it does NOT enforce
    non-negativity, which is a step-12 invariant.
    """
    from fimbox import MakeREM

    for p in (THALWEG_COND, GW_PIXELS, STREAM_PIX):
        assert p.exists(), f"missing: {p}"
    out = MakeREM(
        dem_thalweg_cond=THALWEG_COND,
        gw_catchments_pixels=GW_PIXELS,
        stream_pixels=STREAM_PIX,
        out_rem=REM,
    ).run()
    import rasterio, numpy as np

    with rasterio.open(str(out)) as src:
        data = src.read(1)
        nd = src.nodata
        valid = data[data != nd] if nd is not None else data.ravel()
    log.info(
        f"REM range: [{float(valid.min()):.2f}, {float(valid.max()):.2f}] "
        f"({(valid < 0).sum()} negative pixels — clipped by step 12)"
    )
    assert out.exists() and valid.size > 0 and np.isfinite(valid).all()


def test_step_B11b_rem_nonnegative_after_zero_mask():
    """Cross-check: after step 12 (rem_zeroed_masked), the REM raster must be
    non-negative and contain no NaN pixels. The reference formula
    ``(A * (A>=0) * (B>0))`` with an explicit NoDataValue treats NaN inputs as
    zero; the fimbox port now matches that behaviour by rewriting NaN to the
    nodata sentinel before the multiply.

    Lives next to B11 so a failure here points at the zero-mask logic, not at
    MakeREM itself. Skipped silently if step 12 hasn't run yet (run B12 first).
    """
    import numpy as np
    import rasterio

    if not REM_ZEROED.exists():
        log.warning("skipping non-negativity check — run step_B12 first")
        return
    with rasterio.open(str(REM_ZEROED)) as src:
        data = src.read(1)
        nd = src.nodata
        # Strip both the nodata sentinel and any NaN before the min() so the
        # test catches the actual data range, not an IEEE NaN propagating.
        if nd is not None:
            valid_mask = (data != nd) & ~np.isnan(data)
        else:
            valid_mask = ~np.isnan(data)
        valid = data[valid_mask]
    nan_count = int(np.isnan(data).sum())
    log.info(f"REM zero-mask: {valid.size} valid pixels, {nan_count} NaN pixels")
    assert valid.size > 0
    assert (
        nan_count == 0
    ), f"step 12 leaked {nan_count} NaN pixels into the masked REM raster"
    assert (
        float(valid.min()) >= 0.0
    ), f"step 12 left negatives in REM: min={valid.min()}"


def test_step_B12_rem_zeroed_masked():
    """CreateHAND step 12: clip negative HAND to 0 + mask outside catchments."""
    from fimbox import rem_zeroed_masked

    for p in (REM, GW_REACHES):
        assert p.exists(), f"missing: {p}"
    rem_zeroed_masked(REM, GW_REACHES, REM_ZEROED)
    assert REM_ZEROED.exists()


def test_step_B13_polygonize_catchments():
    """CreateHAND step 13: rasterised catchments --> per-HydroID polygon gpkg."""
    # Helper lives inside create_hand.py as a private function; import it explicitly.
    from fimbox.preprocessing.calculate_branch.create_hand import (
        _polygonize_catchments,
    )

    assert GW_REACHES.exists()
    _polygonize_catchments(GW_REACHES, CATCH_POLY)
    import geopandas as gpd

    gdf = gpd.read_file(str(CATCH_POLY))
    log.info(f"polygonised: {len(gdf)} catchments")
    assert CATCH_POLY.exists() and "HydroID" in gdf.columns and len(gdf) > 0


def test_step_B14_filter_catchments():
    """CreateHAND step 14: drop slivers + attach flow attributes per HydroID."""
    from fimbox import FilterCatchments

    for p in (CATCH_POLY, SPLIT_REACHES):
        assert p.exists(), f"missing: {p}"
    out_catch, out_flows = FilterCatchments(
        catchments_gpkg=CATCH_POLY,
        flows_gpkg=SPLIT_REACHES,
        out_catchments=FILT_CATCH,
        out_flows=FILT_FLOWS,
        aoi_code=AOI_CODE,
        boundary_gpkg=WBD8_CLP if WBD8_CLP.exists() else None,
    ).run()
    import geopandas as gpd

    catches = gpd.read_file(str(out_catch))
    flows = gpd.read_file(str(out_flows))
    log.info(f"filtered catchments: {len(catches)}  flows: {len(flows)}")
    assert len(catches) > 0 and "areasqkm" in catches.columns
    assert len(flows) > 0 and "HydroID" in flows.columns


def test_step_B15_rasterize_filtered_catchments():
    """CreateHAND step 15: burn HydroID back onto the reference raster grid."""
    from fimbox.preprocessing.calculate_branch.create_hand import (
        _rasterize_catchments,
    )

    for p in (FILT_CATCH, GW_REACHES):
        assert p.exists(), f"missing: {p}"
    _rasterize_catchments(FILT_CATCH, GW_REACHES, FILT_TIF)
    assert FILT_TIF.exists()


def test_step_B16_mask_slopes_to_catchments():
    """CreateHAND step 16: clip D8 slopes to the filtered catchment mask."""
    from fimbox import mask_slopes_to_catchments

    for p in (SLOPES_D8, FILT_TIF):
        assert p.exists(), f"missing: {p}"
    mask_slopes_to_catchments(SLOPES_D8, FILT_TIF, SLOPES_MASKED)
    assert SLOPES_MASKED.exists()


def test_step_B17_stages_and_catchlist():
    """CreateHAND step 17: write the stage ladder + per-HydroID metadata text files."""
    from fimbox import make_stages_and_catchlist

    for p in (FILT_FLOWS, FILT_CATCH):
        assert p.exists(), f"missing: {p}"
    make_stages_and_catchlist(
        flows_gpkg=FILT_FLOWS,
        catchments_gpkg=FILT_CATCH,
        out_stages=STAGE_TXT,
        out_catchlist=CATCHLIST_TXT,
        stages_min=0.0,
        stages_interval=0.3048,
        stages_max=25.2984,
    )
    assert STAGE_TXT.exists() and CATCHLIST_TXT.exists()


def test_step_B18_build_src_base():
    """CreateHAND step 18: synthetic rating curve base table (TauDEM-style geometry)."""
    from fimbox import build_src_base

    for p in (REM_ZEROED, FILT_TIF, SLOPES_MASKED, CATCHLIST_TXT, STAGE_TXT):
        assert p.exists(), f"missing: {p}"
    build_src_base(
        hand_raster=REM_ZEROED,
        catch_raster=FILT_TIF,
        slope_raster=SLOPES_MASKED,
        catchlist_txt=CATCHLIST_TXT,
        stages_txt=STAGE_TXT,
        out_csv=SRC_BASE_CSV,
    )
    import pandas as pd

    df = pd.read_csv(SRC_BASE_CSV)
    log.info(f"src_base: {len(df)} rows  HydroIDs={df['CatchId'].nunique()}")
    assert SRC_BASE_CSV.exists() and len(df) > 0


def test_step_B19_add_crosswalk():
    """CreateHAND step 19: NWM crosswalk + Manning's hydraulics + hydroTable."""
    from fimbox import add_crosswalk

    for p in (FILT_CATCH, FILT_FLOWS, SRC_BASE_CSV, STREAMS):
        assert p.exists(), f"missing: {p}"
    add_crosswalk(
        catchments_gpkg=FILT_CATCH,
        flows_gpkg=FILT_FLOWS,
        src_base_csv=SRC_BASE_CSV,
        nwm_streams_gpkg=STREAMS,
        out_catchments_gpkg=XWALK_CATCH,
        out_flows_gpkg=XWALK_FLOWS,
        out_src_csv=SRC_FULL_CSV,
        out_src_json=SRC_JSON,
        out_crosswalk_csv=XWALK_CSV,
        out_hydro_csv=HYDRO_TABLE,
        aoi_code=AOI_CODE,
        boundary_gpkg=WBD8_CLP if WBD8_CLP.exists() else None,
        mannings_n=0.06,
        min_catchment_area=0.25,
        min_stream_length=0.5,
        max_distance_m=100.0,
        small_segments_csv=BRANCH_DIR / f"small_segments_{BRANCH_ID}.csv",
    )
    import pandas as pd

    ht = pd.read_csv(HYDRO_TABLE, dtype={"aoi_code": str, "HydroID": str})
    log.info(f"hydroTable: {len(ht)} rows  HydroIDs={ht['HydroID'].nunique()}")
    assert HYDRO_TABLE.exists() and (ht["discharge_cms"] >= 0).all()


def test_step_B20_heal_bridges_osm():
    """CreateHAND step 20: raise HAND at OSM bridge decks (in-place REM update)."""
    from fimbox import heal_bridges_osm

    bridges_gpkg = OUT_DIR / "osm_bridges_subset.gpkg"
    if not bridges_gpkg.exists():
        log.warning("skipping bridge heal — no OSM bridges gpkg")
        return
    for p in (REM_ZEROED, XWALK_CATCH):
        assert p.exists(), f"missing: {p}"
    bridge_diff = OUT_DIR / "bridge_elev_diff.tif"
    heal_bridges_osm(
        hand_raster=REM_ZEROED,
        bridges_gpkg=bridges_gpkg,
        catchments_gpkg=XWALK_CATCH,
        out_centroids_gpkg=BRIDGES_GPKG,
        bridge_diff_raster=bridge_diff if bridge_diff.exists() else None,
    )
    assert BRIDGES_GPKG.exists()


def test_step_B21_process_roads_fimpact():
    """CreateHAND step 21: sample HAND along OSM roads to derive flood thresholds."""
    from fimbox import process_roads_fimpact

    roads_gpkg = OUT_DIR / "osm_roads_subset.gpkg"
    if not roads_gpkg.exists():
        log.warning("skipping road FIMpact — no OSM roads gpkg")
        return
    for p in (REM_ZEROED, XWALK_CATCH):
        assert p.exists(), f"missing: {p}"
    process_roads_fimpact(
        hand_raster=REM_ZEROED,
        roads_gpkg=roads_gpkg,
        catchments_gpkg=XWALK_CATCH,
        out_csv=ROADS_CSV,
    )
    assert ROADS_CSV.exists()


# Stage C — branch-zero post-CreateHAND steps
# (download USGS gauges --> AOI-level assignment --> branch-zero crosswalk --> cleanup)

# AOI-level path to the staged USGS gages gpkg
USGS_GAGES = OUT_DIR / "usgs_gages.gpkg"
USGS_SUBSET = OUT_DIR / "usgs_subset_gages.gpkg"
USGS_SUBSET_BZERO = OUT_DIR / f"usgs_subset_gages_{BRANCH_ID}.gpkg"
NWM_LEVELPATHS = OUT_DIR / "nwm_subset_streams_levelPaths.gpkg"


def test_step_C20_download_usgs_gages():
    """Download USGS gauge points inside the AOI from the ArcGIS Online
    FeatureServer. Writes ``usgs_gages.gpkg`` at the AOI root, with the columns
    ``assign_gages_to_branches`` expects: ``location_id``, ``feature_id``,
    ``aoi_id``, ``source``, geometry.
    """
    from fimbox import DownloadUSGSGages

    # Use the buffered boundary so gauges just outside the WBD are still
    # captured (they may snap to streams that drain into the AOI).
    boundary = BOUNDARY_BUF if BOUNDARY_BUF.exists() else WBD8_CLP
    assert boundary.exists(), f"missing boundary: {boundary}"

    gdf = DownloadUSGSGages().download(
        boundary=boundary,
        aoi_id=AOI_CODE,
        out_dir=OUT_DIR,
        out_name="usgs_gages.gpkg",
        out_layer="usgs_gages",
    )
    log.info(f"USGS gauges downloaded: {len(gdf)} features --> {USGS_GAGES.name}")
    # Empty AOI (no gauges in CONUS layer) is acceptable; only assert the
    # file exists when at least one feature came back.
    if len(gdf) > 0:
        assert USGS_GAGES.exists()
        assert {"location_id", "feature_id", "aoi_id", "source"}.issubset(gdf.columns)


def test_step_C21_assign_gages_to_branches():
    """Stage 1 of the gage crosswalk: tag every gage with a ``feature_id`` +
    ``levpa_id`` (= branch id) and write the AOI-wide + branch-zero gpkgs.

    Skips if either ``usgs_gages.gpkg`` (from C20) or
    ``nwm_subset_streams_levelPaths.gpkg`` (from BranchDerivation in Z0) is
    missing — both prerequisites get logged so a failure points at the
    right upstream step.
    """
    from fimbox import assign_gages_to_branches

    if not USGS_GAGES.exists():
        log.warning(
            "skipping gage assignment — usgs_gages.gpkg missing (run step_C20 first)"
        )
        return
    if not NWM_LEVELPATHS.exists():
        log.warning(
            "skipping gage assignment — nwm_subset_streams_levelPaths.gpkg missing "
            "(run step_Z0_branch_derivation first)"
        )
        return

    assign_gages_to_branches(
        usgs_gages_gpkg=USGS_GAGES,
        nwm_streams_levelpaths_gpkg=NWM_LEVELPATHS,
        aoi_id=AOI_CODE,
        out_dir=OUT_DIR,
        # DownloadUSGSGages writes "aoi_id"; the default filter column ("HUC8")
        # would not find anything in that gpkg.
        aoi_filter_column="aoi_id",
        branch_zero_id=BRANCH_ID,
    )
    # When the AOI actually contains gauges both files exist; on empty AOIs
    # neither is written and the function returns None (logged a warning).
    if USGS_SUBSET.exists():
        log.info(
            f"AOI-wide gages --> {USGS_SUBSET.name} | "
            f"branch-zero --> {USGS_SUBSET_BZERO.name}"
        )
        assert USGS_SUBSET_BZERO.exists()


def test_step_C22_usgs_crosswalk_branch_zero():
    """Stage 2 of the gage crosswalk for branch zero.

    Snaps every branch-zero gage to its DEM-derived thalweg and samples the
    DEM + thalweg-conditioned DEM to populate ``dem_elevation`` and
    ``dem_adj_elevation`` on the gage table. Output:
    ``branches/0/usgs_elev_table.csv``.

    Prerequisites: ``usgs_subset_gages_0.gpkg`` (from C21) and the per-branch
    CreateHAND outputs (from Z1 + the B-series). Skips silently when the
    branch-zero gage gpkg isn't on disk, so an AOI with no gauges does not
    fail the suite.
    """
    from fimbox import run_branch_crosswalk

    if not USGS_SUBSET_BZERO.exists():
        log.warning(
            "skipping USGS crosswalk — usgs_subset_gages_0.gpkg missing "
            "(run step_C20 + step_C21 first to produce it)"
        )
        return
    bzero_gages = USGS_SUBSET_BZERO

    # dem_meters_{B}.tif is the inundation-mapping name; fimbox writes dem_{B}.tif
    # via BranchZero. Use whichever exists.
    dem_b = BRANCH_DIR / f"dem_meters_{BRANCH_ID}.tif"
    if not dem_b.exists():
        dem_b = DEM_BRANCH

    for p in (XWALK_CATCH, FILT_FLOWS, dem_b, THALWEG_COND):
        assert p.exists(), f"missing: {p}"

    out = run_branch_crosswalk(
        aoi_gages_gpkg=bzero_gages,
        branch_catchments_gpkg=XWALK_CATCH,
        branch_flows_gpkg=FILT_FLOWS,
        dem_path=dem_b,
        dem_thalweg_path=THALWEG_COND,
        branch_id=BRANCH_ID,
        out_dir=BRANCH_DIR,
    )
    usgs_table = BRANCH_DIR / "usgs_elev_table.csv"
    log.info(f"USGS crosswalk wrote: {[p for p in out.values() if p]}")
    # usgs_elev_table.csv only exists when the AOI has gages — log either way.
    if usgs_table.exists():
        import pandas as pd

        df = pd.read_csv(usgs_table)
        log.info(f"usgs_elev_table.csv rows: {len(df)}")


def test_step_C23_outputs_cleanup_branch_zero():
    """Apply the deny-list cleanup to ``branches/0/``.

    Default behaviour deletes every intermediate raster + vector listed in
    --> fimbox/config/deny_branch_zero.lst.
    """
    import os

    from fimbox import remove_deny_list_files

    deny_path = (
        Path(__file__).resolve().parent.parent / "config" / "deny_branch_zero.lst"
    )
    assert deny_path.is_file(), f"deny list missing: {deny_path}"

    # API sanity checks that always run (never touch real files).
    assert remove_deny_list_files(BRANCH_DIR, "NONE", BRANCH_ID) == 0
    assert remove_deny_list_files(BRANCH_DIR, "none", BRANCH_ID) == 0

    if os.environ.get("FIMBOX_KEEP_BRANCH_ZERO"):
        n_patterns = sum(
            1
            for L in deny_path.read_text().splitlines()
            if L.strip() and not L.lstrip().startswith("#")
        )
        log.info(
            f"step_C23: skipping cleanup (FIMBOX_KEEP_BRANCH_ZERO set). "
            f"{deny_path.name} has {n_patterns} active patterns; "
            "unset the env var to enable cleanup."
        )
        return

    # The branch-0 directory may be empty when only later steps have been
    # populated, or when an earlier C23 run already cleaned it. Skip cleanly
    # if there's nothing to do.
    if not BRANCH_DIR.exists():
        log.warning(f"skipping cleanup — branch dir {BRANCH_DIR} missing")
        return

    n = remove_deny_list_files(
        src_dir=BRANCH_DIR,
        deny_list=deny_path,
        branch_id=BRANCH_ID,
        verbose=True,
    )
    log.info(f"step_C23: removed {n} files from {BRANCH_DIR}")


def test_step_C24_calculate_allbranches(tmp_path):
    """Fast wrapper check without launching real branch workers."""
    from fimbox import AOIProcessingConfig, calculate_allbranches

    aoi_dir = tmp_path / "aoi"
    aoi_dir.mkdir()
    # Match BranchDerivation's actual output: branch_ids.lst (one id per line).
    # Empty file = branch-zero-only run, which is what this wrapper test exercises.
    branch_list_path = aoi_dir / "branch_ids.lst"
    branch_list_path.write_text("")

    deny_unit_list = tmp_path / "deny_unit.lst"
    deny_unit_list.write_text("temporary_{}.tif\n")
    removable = aoi_dir / f"temporary_{AOI_CODE}.tif"
    removable.write_bytes(b"x")

    result = calculate_allbranches(
        AOIProcessingConfig(
            aoi_dir=aoi_dir,
            aoi_id=AOI_CODE,
            branch_list_path=branch_list_path,
            n_workers=1,
        ),
        delete_deny_list=True,
        deny_unit_list=deny_unit_list,
        branch_ids_csv=aoi_dir / "branch_ids.csv",
    )

    assert result.n_branch_zero_recorded == 1
    assert result.n_non_zero_recorded == 0
    assert result.n_unit_files_removed == 1
    assert not removable.exists()


def test_step_C25_calculate_allbranches_live_run():
    """Live run for the real non-zero branch loop.

    Set FIMBOX_KEEP_UNIT=1 to skip AOI-level cleanup.
    Set FIMBOX_SKIP_ALLBRANCHES=1 to skip this test (e.g. during quick CI
    smoke runs); by default it always runs.
    """
    from fimbox import AOIProcessingConfig, calculate_allbranches

    if os.environ.get("FIMBOX_SKIP_ALLBRANCHES"):
        pytest.skip("FIMBOX_SKIP_ALLBRANCHES set — skipping live branch loop")

    # BranchDerivation writes branch_ids.lst
    branch_list_path = OUT_DIR / "branch_ids.lst"

    deny_unit_list = Path(__file__).resolve().parent.parent / "config" / "deny_unit.lst"
    assert deny_unit_list.is_file(), f"deny_unit.lst missing: {deny_unit_list}"

    cfg = AOIProcessingConfig(
        aoi_dir=OUT_DIR,
        aoi_id=AOI_CODE,
        branch_list_path=branch_list_path,
        n_workers=int(os.environ.get("FIMBOX_BRANCH_WORKERS", "1")),
        delete_deny_list=True,
    )

    delete_deny_list = True
    result = calculate_allbranches(
        cfg,
        delete_deny_list=delete_deny_list,
        deny_unit_list=deny_unit_list if delete_deny_list else None,
        branch_ids_csv=OUT_DIR / "branch_ids.csv",
    )

    assert result.n_branch_zero_recorded == 1
    assert result.branch_ids_csv.exists(), "branch_ids.csv was not created"
    assert result.n_non_zero_recorded == sum(
        1 for r in result.branch_results if r.status == "ok"
    )
