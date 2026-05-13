"""
Branch processing tests.

Run order:
  1. test_branch_derivation  — level paths, branch polygons, branch list
  2. test_branch_zero_full   — DEM clip, AGREE, pit-fill, D8 flowdir
  3. test_create_hand        — full HAND generation (flow accum → split reaches)
"""

from pathlib import Path

from fimbox import (
    BranchDerivation,
    BranchZero,
    CreateHAND,
)

# imports used only by individual component tests below
# from fimbox import (
#     HydroenforceDEM, FlowdirDEM,
#     StreamBooleanRasterizer, LevelPathBooleanRasterizer, HeadwaterRasterizer,
#     rasterize_3d_levee_lines, burn_levee_elevations,
#     FlowAccDEM, ThalwegAdjustment, D8SlopeDEM, StreamNetReaches, split_derived_reaches,
# )

# AOI parameters — point this at any user-supplied AOI working directory.
# aoi_code is recorded on every hydroTable row; a generic string is fine
# (HUC IDs work unchanged for legacy datasets).
OUT_DIR  = Path("/Users/Supath/Downloads/SDML/FIMBOX/out/HUC08060202")
AOI_CODE = "08060202"

# Tunable CreateHAND parameters — change here to play with the pipeline without
# editing the call site below. All have sensible defaults in CreateHAND itself.
PARAMS_CREATE_HAND = dict(
    # geometry / topology
    cost_distance_tolerance     = 50.0,     # m, lateral cost distance
    lateral_elevation_threshold = 3,        # m, lateral thalweg drop cap
    max_split_distance_m        = 2000.0,   # m, split-reach max length
    slope_min                   = 0.0001,   # rise/run floor
    lakes_buffer_dist_m         = 100.0,    # m, lake-boundary buffer
    # SRC / crosswalk
    mannings_n                  = 0.06,     # channel roughness
    stage_min_m                 = 0.0,      # SRC stage ladder start
    stage_interval_m            = 0.3048,   # SRC stage step (1 ft)
    stage_max_m                 = 25.2984,  # SRC stage ladder end (~83 ft)
    min_catchment_area          = 0.25,     # km^2, short-reach replace threshold
    min_stream_length           = 0.5,      # km, short-reach replace threshold
    crosswalk_max_distance_m    = 100.0,    # m, midpoint-to-NWM-flowline cap
)

DEM           = OUT_DIR / "dem.tif"
STREAMS       = OUT_DIR / "nwm_subset_streams.gpkg"
BOUNDARY_BUF  = OUT_DIR / "wbd_buffered.gpkg"
CATCHMENTS    = OUT_DIR / "nwm_catchments_proj_subset.gpkg"
HEADWATERS    = OUT_DIR / "nwm_headwater_points_subset.gpkg"
LEVELPATH_EXT = OUT_DIR / "nwm_subset_streams_levelPaths_extended.gpkg"
BRIDGE_DIFF   = OUT_DIR / "bridge_elev_diff.tif"
NLD_LEVEES    = OUT_DIR / "3d_nld_subset_levees_burned.gpkg"

# optional files
WBD8_CLP      = OUT_DIR / "wbd8_clp.gpkg"
LAKES         = OUT_DIR / "nwm_lakes_proj_subset.gpkg"
LEVEE_AREAS   = OUT_DIR / "LeveeProtectedAreas_subset.gpkg"
LEVEE_LP_CSV  = OUT_DIR / "levee_levelpaths.csv"

# branch-zero derived paths
BRANCH_DIR    = OUT_DIR / "branches" / "0"
BRANCH_ID     = "0"
DEM_BRANCH    = BRANCH_DIR / f"dem_{BRANCH_ID}.tif"
FLOWDIR       = BRANCH_DIR / f"flowdir_d8_burned_filled_{BRANCH_ID}.tif"
HW_RASTER     = BRANCH_DIR / f"headwaters_{BRANCH_ID}.tif"
STREAM_BOOL   = BRANCH_DIR / f"flows_grid_boolean_{BRANCH_ID}.tif"
DEM_BURNED    = BRANCH_DIR / f"dem_burned_{BRANCH_ID}.tif"
DEM_FILLED    = BRANCH_DIR / f"dem_burned_filled_{BRANCH_ID}.tif"

# REM + filtered catchment paths
REM           = BRANCH_DIR / f"rem_{BRANCH_ID}.tif"
REM_ZEROED    = BRANCH_DIR / f"rem_zeroed_masked_{BRANCH_ID}.tif"
CATCH_POLY    = BRANCH_DIR / f"gw_catchments_reaches_{BRANCH_ID}.gpkg"
FILT_CATCH    = BRANCH_DIR / f"gw_catchments_reaches_filtered_addedAttributes_{BRANCH_ID}.gpkg"
FILT_FLOWS    = BRANCH_DIR / f"demDerived_reaches_split_filtered_{BRANCH_ID}.gpkg"
FILT_TIF      = BRANCH_DIR / f"gw_catchments_reaches_filtered_addedAttributes_{BRANCH_ID}.tif"

# SRC / crosswalk / hydroTable outputs (steps 16-21)
SLOPES_MASKED = BRANCH_DIR / f"slopes_d8_dem_meters_masked_{BRANCH_ID}.tif"
STAGE_TXT     = BRANCH_DIR / f"stage_{BRANCH_ID}.txt"
CATCHLIST_TXT = BRANCH_DIR / f"catch_list_{BRANCH_ID}.txt"
SRC_BASE_CSV  = BRANCH_DIR / f"src_base_{BRANCH_ID}.csv"
XWALK_CATCH   = BRANCH_DIR / f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{BRANCH_ID}.gpkg"
XWALK_FLOWS   = BRANCH_DIR / f"demDerived_reaches_split_filtered_addedAttributes_crosswalked_{BRANCH_ID}.gpkg"
SRC_FULL_CSV  = BRANCH_DIR / f"src_full_crosswalked_{BRANCH_ID}.csv"
SRC_JSON      = BRANCH_DIR / f"src_{BRANCH_ID}.json"
XWALK_CSV     = BRANCH_DIR / f"crosswalk_table_{BRANCH_ID}.csv"
HYDRO_TABLE   = BRANCH_DIR / f"hydroTable_{BRANCH_ID}.csv"
ROADS_CSV     = BRANCH_DIR / f"osm_roads_fimpact_{BRANCH_ID}.csv"
BRIDGES_GPKG  = BRANCH_DIR / f"osm_bridge_centroids_{BRANCH_ID}.gpkg"

# HAND generation derived paths
FLOWACCUM     = BRANCH_DIR / f"flowaccum_d8_burned_filled_{BRANCH_ID}.tif"
STREAM_PIX    = BRANCH_DIR / f"demDerived_streamPixels_{BRANCH_ID}.tif"
THALWEG_ADJ   = BRANCH_DIR / f"dem_lateral_thalweg_adj_{BRANCH_ID}.tif"
FLOWDIR_STR   = BRANCH_DIR / f"flowdir_d8_burned_filled_flows_{BRANCH_ID}.tif"
THALWEG_COND  = BRANCH_DIR / f"dem_thalwegCond_{BRANCH_ID}.tif"
SLOPES_D8     = BRANCH_DIR / f"slopes_d8_dem_{BRANCH_ID}.tif"
STREAM_ORDER  = BRANCH_DIR / f"streamOrder_{BRANCH_ID}.tif"
SN_CATCH      = BRANCH_DIR / f"sn_catchments_reaches_{BRANCH_ID}.tif"
DEM_REACHES   = BRANCH_DIR / f"demDerived_reaches_{BRANCH_ID}.gpkg"
SPLIT_REACHES = BRANCH_DIR / f"demDerived_reaches_split_{BRANCH_ID}.gpkg"
SPLIT_PTS     = BRANCH_DIR / f"demDerived_reaches_split_points_{BRANCH_ID}.gpkg"
GW_REACHES    = BRANCH_DIR / f"gw_catchments_reaches_{BRANCH_ID}.tif"
PIXEL_PTS     = BRANCH_DIR / f"flows_points_pixels_{BRANCH_ID}.gpkg"
GW_PIXELS     = BRANCH_DIR / f"gw_catchments_pixels_{BRANCH_ID}.tif"


def test_branch_derivation():
    """Derive level paths, branch polygons, and branch list from staged NWM data."""
    result = BranchDerivation(
        out_dir=OUT_DIR,
        branch_id_attribute="levpa_id",
        reach_id_attribute="ID",
        branch_buffer_distance_meters=1000.0,
    ).run()

    assert result.dissolved_levelpaths.exists(), "dissolved levelpaths not written"
    assert result.branch_polygons.exists(),      "branch polygons not written"
    assert result.branch_list.exists(),          "branch list file not written"
    assert len(result.branch_dataframe) > 0,     "branch dataframe is empty"

    print(f"\nLevel paths    : {result.levelpaths}")
    print(f"Dissolved      : {result.dissolved_levelpaths}")
    print(f"Branch polygons: {result.branch_polygons}")
    print(f"Branch list    : {result.branch_list}")
    print(f"Branch count   : {len(result.branch_dataframe)}")


def test_branch_zero_full():
    """Run the complete branch-zero preprocessing pipeline."""
    outputs = BranchZero(
        dem_path=DEM,
        streams_gpkg=STREAMS,
        boundary_gpkg=BOUNDARY_BUF,
        out_dir=OUT_DIR,
        bridge_elev_diff_path=BRIDGE_DIFF if BRIDGE_DIFF.exists() else None,
        headwaters_gpkg=HEADWATERS if HEADWATERS.exists() else None,
        levelpaths_extended_gpkg=LEVELPATH_EXT if LEVELPATH_EXT.exists() else None,
        agree_buffer_m=15.0,
        agree_smooth_drop=10.0,
        agree_sharp_drop=1000.0,
        branch_zero_id=BRANCH_ID,
    ).run()

    assert outputs["dem"].exists(),               "clipped DEM not written"
    assert outputs["flows_grid_boolean"].exists(), "stream boolean grid not written"
    assert outputs["dem_burned"].exists(),         "AGREE DEM not written"
    assert outputs["dem_burned_filled"].exists(),  "pit-filled DEM not written"
    assert outputs["flowdir_d8"].exists(),         "D8 flow direction not written"

    print("\nBranch-zero outputs:")
    for k, v in outputs.items():
        print(f"  {k:35s}: {v.name}")


def test_create_hand():
    """
    Run the full HAND generation pipeline (22 steps) for branch zero.
    All standard input paths are resolved from OUT_DIR and BRANCH_DIR automatically.
    Tunable parameters live in ``PARAMS_CREATE_HAND`` at the top of this file.
    Requires: test_branch_zero_full outputs to exist.
    """
    assert DEM_BRANCH.exists(), f"Run test_branch_zero_full first — {DEM_BRANCH} missing"
    assert FLOWDIR.exists(),    f"flowdir missing — run test_branch_zero_full first"

    outputs = CreateHAND(
        aoi_dir=OUT_DIR,
        branch_dir=BRANCH_DIR,
        branch_id=BRANCH_ID,
        aoi_code=AOI_CODE,
        levee_protected_areas_gpkg=LEVEE_AREAS if LEVEE_AREAS.exists() else None,
        levee_levelpaths_csv=LEVEE_LP_CSV if LEVEE_LP_CSV.exists() else None,
        lakes_gpkg=LAKES if LAKES.exists() else None,
        boundary_gpkg=WBD8_CLP if WBD8_CLP.exists() else None,
        **PARAMS_CREATE_HAND,
    ).run()

    print(f"\nCreateHAND outputs ({len(outputs)} files):")
    for k, v in outputs.items():
        print(f"  {k:35s}: {v.name}  {'ok' if v.exists() else 'MISSING'}")

    # HAND base outputs
    assert THALWEG_COND.exists(),  "dem_thalwegCond not produced"
    assert SLOPES_D8.exists(),     "slopes_d8 not produced"
    assert DEM_REACHES.exists(),   "demDerived_reaches not produced"
    assert SPLIT_REACHES.exists(), "demDerived_reaches_split not produced"
    assert SPLIT_PTS.exists(),     "demDerived_reaches_split_points not produced"
    assert GW_REACHES.exists(),    "gw_catchments_reaches not produced"
    assert PIXEL_PTS.exists(),     "flows_points_pixels not produced"
    assert GW_PIXELS.exists(),     "gw_catchments_pixels not produced"
    assert REM.exists(),           "rem not produced"
    assert REM_ZEROED.exists(),    "rem_zeroed_masked not produced"
    assert CATCH_POLY.exists(),    "gw_catchments_reaches gpkg not produced"
    assert FILT_CATCH.exists(),    "filtered catchments not produced"
    assert FILT_FLOWS.exists(),    "filtered flows not produced"
    assert FILT_TIF.exists(),      "filtered catchments tif not produced"

    # SRC, crosswalk with hydroTable
    assert SLOPES_MASKED.exists(), "slopes mask not produced"
    assert STAGE_TXT.exists(),     "stage list not produced"
    assert CATCHLIST_TXT.exists(), "catchlist file not produced"
    assert SRC_BASE_CSV.exists(),  "SRC base CSV not produced"
    assert XWALK_CATCH.exists(),   "crosswalked catchments not produced"
    assert XWALK_FLOWS.exists(),   "crosswalked flows not produced"
    assert SRC_FULL_CSV.exists(),  "SRC full CSV not produced"
    assert SRC_JSON.exists(),      "SRC JSON not produced"
    assert XWALK_CSV.exists(),     "crosswalk table not produced"
    assert HYDRO_TABLE.exists(),   "hydroTable not produced"

    # hydroTable sanity: required columns + monotonic stage→discharge per HydroID
    import pandas as pd
    # Read aoi_code/HydroID/feature_id as strings — pandas otherwise infers them
    # numeric and strips leading zeros that legitimately exist in HUC-style codes.
    ht = pd.read_csv(
        HYDRO_TABLE,
        dtype={"aoi_code": str, "HydroID": str, "feature_id": str},
    )
    required = {"HydroID", "feature_id", "aoi_code", "stage", "discharge_cms",
                "ManningN", "SLOPE", "LENGTHKM"}
    missing = required - set(ht.columns)
    assert not missing, f"hydroTable missing columns: {missing}"
    assert (ht["stage"] >= 0).all(), "negative stage values in hydroTable"
    assert (ht["discharge_cms"] >= 0).all(), "negative discharge in hydroTable"
    aoi_unique = set(ht["aoi_code"].unique())
    assert AOI_CODE in aoi_unique or any(AOI_CODE in v for v in aoi_unique), \
        f"aoi_code column {aoi_unique} does not contain test AOI_CODE={AOI_CODE!r}"
    print(f"hydroTable rows: {len(ht)}, HydroIDs: {ht['HydroID'].nunique()}, "
          f"feature_ids: {ht['feature_id'].nunique()}, "
          f"aoi_code: {sorted(aoi_unique)}")


# individual component tests — uncomment to run a single step in isolation

# def test_flow_accumulation():
#     assert FLOWDIR.exists() and HW_RASTER.exists()
#     fa_out, sp_out = FlowAccDEM(
#         flowdir=FLOWDIR, headwaters=HW_RASTER,
#         out_flowaccum=FLOWACCUM, out_stream_pixels=STREAM_PIX, threshold=1.0,
#     ).run()
#     import rasterio
#     with rasterio.open(str(sp_out)) as src:
#         stream_count = int((src.read(1) == 1).sum())
#     print(f"\nStream pixels: {stream_count:,}  nodata={src.nodata}")
#     assert fa_out.exists() and sp_out.exists() and stream_count > 0

# def test_thalweg_adjustment():
#     assert DEM_BRANCH.exists() and STREAM_PIX.exists() and FLOWDIR.exists()
#     result = ThalwegAdjustment(
#         dem=DEM_BRANCH, stream_pixels=STREAM_PIX, flowdir=FLOWDIR,
#         out_thalweg_adj=THALWEG_ADJ, out_flowdir_streams=FLOWDIR_STR,
#         out_thalweg_cond=THALWEG_COND, cost_distance_tolerance=50.0,
#         lateral_elevation_threshold=3,
#     ).run()
#     assert result["thalweg_adj"].exists() and result["thalweg_cond"].exists()

# def test_d8_slopes():
#     assert THALWEG_ADJ.exists() and FLOWDIR.exists()
#     import numpy as np, rasterio
#     out = D8SlopeDEM(dem=THALWEG_ADJ, flowdir=FLOWDIR, out_path=SLOPES_D8, slope_min=0.0001).run()
#     with rasterio.open(str(out)) as src:
#         d = src.read(1); nd = src.nodata
#         valid = d[(d != nd) & np.isfinite(d)] if nd is not None else d[np.isfinite(d)]
#     print(f"\nslopes range: [{valid.min():.6f}, {valid.max():.6f}]")
#     assert float(valid.min()) >= 0.0001

# def test_streamnet_reaches():
#     for p in (FLOWDIR, THALWEG_COND, FLOWACCUM, STREAM_PIX):
#         assert p.exists(), f"missing: {p}"
#     result = StreamNetReaches(
#         flowdir=FLOWDIR, dem_thalweg_cond=THALWEG_COND, flowaccum=FLOWACCUM,
#         stream_pixels=STREAM_PIX, out_dir=BRANCH_DIR, branch_id=BRANCH_ID,
#     ).run()
#     import geopandas as gpd
#     reaches = gpd.read_file(str(result["demDerived_reaches"]))
#     print(f"\nReaches: {len(reaches)} features")
#     assert len(reaches) > 0

# def test_split_reaches():
#     for p in (DEM_REACHES, THALWEG_COND, STREAMS):
#         assert p.exists(), f"missing: {p}"
#     split_gpkg, pts_gpkg = split_derived_reaches(
#         reaches_gpkg=DEM_REACHES, dem_thalweg_cond=THALWEG_COND,
#         nwm_streams_gpkg=STREAMS, out_split_gpkg=SPLIT_REACHES, out_points_gpkg=SPLIT_PTS,
#         wbd8_clp_gpkg=WBD8_CLP if WBD8_CLP.exists() else None,
#         lakes_gpkg=LAKES if LAKES.exists() else None,
#         max_length=2000.0, slope_min=0.0001, lakes_buffer_dist=100.0,
#     )
#     import geopandas as gpd
#     split = gpd.read_file(str(split_gpkg))
#     print(f"\nSplit reaches: {len(split)} segments  columns: {list(split.columns)}")
#     assert len(split) > 0 and "HydroID" in split.columns and "NextDownID" in split.columns

# def test_stream_boolean_rasterizer():
#     out = BRANCH_DIR / "flows_grid_boolean_test.tif"
#     StreamBooleanRasterizer(STREAMS, DEM, out).run()
#     import rasterio
#     with rasterio.open(out) as src:
#         data = src.read(1)
#     assert data.max() == 1 and data.min() == 0

# def test_headwater_rasterizer():
#     HeadwaterRasterizer(HEADWATERS, DEM, BRANCH_DIR / "headwaters_test.tif").run()

# def test_levelpath_boolean_rasterizer():
#     LevelPathBooleanRasterizer(LEVELPATH_EXT, DEM, OUT_DIR / "flows_grid_boolean_test.tif").run()

# def test_hydroenforce_dem():
#     HydroenforceDEM(
#         rivers_raster=STREAM_BOOL, dem=DEM_BRANCH,
#         output_raster=BRANCH_DIR / "dem_burned_test.tif",
#         workspace=BRANCH_DIR, buffer_dist=15.0, smooth_drop=10.0, sharp_drop=1000.0,
#     ).run()

# def test_flowdir_dem():
#     FlowdirDEM(DEM_FILLED, BRANCH_DIR / "flowdir_d8_test.tif").run()

# def test_make_rem():
#     from fimbox import MakeREM
#     for p in (THALWEG_COND, GW_PIXELS, STREAM_PIX):
#         assert p.exists(), f"missing: {p}"
#     out = MakeREM(
#         dem_thalweg_cond=THALWEG_COND,
#         gw_catchments_pixels=GW_PIXELS,
#         stream_pixels=STREAM_PIX,
#         out_rem=REM,
#     ).run()
#     import rasterio, numpy as np
#     with rasterio.open(str(out)) as src:
#         data = src.read(1); nd = src.nodata
#         valid = data[data != nd] if nd is not None else data.ravel()
#     print(f"\nREM range: [{float(valid.min()):.2f}, {float(valid.max()):.2f}]")
#     assert out.exists() and float(valid.min()) >= 0.0

# def test_filter_catchments():
#     from fimbox import FilterCatchments
#     for p in (CATCH_POLY, SPLIT_REACHES):
#         assert p.exists(), f"missing: {p}"
#     out_catch, out_flows = FilterCatchments(
#         catchments_gpkg=CATCH_POLY,
#         flows_gpkg=SPLIT_REACHES,
#         out_catchments=FILT_CATCH,
#         out_flows=FILT_FLOWS,
#         huc_code="08060202",
#         wbd8_clp_gpkg=WBD8_CLP if WBD8_CLP.exists() else None,
#     ).run()
#     import geopandas as gpd
#     catches = gpd.read_file(str(out_catch))
#     flows   = gpd.read_file(str(out_flows))
#     print(f"\nFiltered catchments: {len(catches)}  flows: {len(flows)}")
#     assert len(catches) > 0 and "areasqkm" in catches.columns
#     assert len(flows) > 0 and "HydroID" in flows.columns

# def test_levee_rasterize_and_burn():
#     if not NLD_LEVEES.exists():
#         return
#     out_r = BRANCH_DIR / "nld_rasterized_elev_test.tif"
#     out_d = BRANCH_DIR / "dem_levee_burned_test.tif"
#     rasterize_3d_levee_lines(NLD_LEVEES, DEM, out_r)
#     burn_levee_elevations(DEM, out_r, out_d)
#     assert out_r.exists() and out_d.exists()
