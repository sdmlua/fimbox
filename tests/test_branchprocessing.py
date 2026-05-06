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

# input paths
OUT_DIR = Path("/Users/supath/Downloads/MSResearch/FIMBOX/out/HUC03020202")

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
    Run the full HAND generation pipeline for branch zero.
    All standard input paths are resolved from OUT_DIR and BRANCH_DIR automatically.
    Requires: test_branch_zero_full outputs to exist.
    """
    assert DEM_BRANCH.exists(), f"Run test_branch_zero_full first — {DEM_BRANCH} missing"
    assert FLOWDIR.exists(),    f"flowdir missing — run test_branch_zero_full first"

    outputs = CreateHAND(
        aoi_dir=OUT_DIR,
        branch_dir=BRANCH_DIR,
        branch_id=BRANCH_ID,
        levee_protected_areas_gpkg=LEVEE_AREAS if LEVEE_AREAS.exists() else None,
        levee_levelpaths_csv=LEVEE_LP_CSV if LEVEE_LP_CSV.exists() else None,
        lakes_gpkg=LAKES if LAKES.exists() else None,
        wbd8_clp_gpkg=WBD8_CLP if WBD8_CLP.exists() else None,
    ).run()

    print(f"\nCreateHAND outputs ({len(outputs)} files):")
    for k, v in outputs.items():
        print(f"  {k:35s}: {v.name}  {'ok' if v.exists() else 'MISSING'}")

    assert THALWEG_COND.exists(),  "dem_thalwegCond not produced"
    assert SLOPES_D8.exists(),     "slopes_d8 not produced"
    assert DEM_REACHES.exists(),   "demDerived_reaches not produced"
    assert SPLIT_REACHES.exists(), "demDerived_reaches_split not produced"
    assert SPLIT_PTS.exists(),     "demDerived_reaches_split_points not produced"
    assert GW_REACHES.exists(),    "gw_catchments_reaches not produced"
    assert PIXEL_PTS.exists(),     "flows_points_pixels not produced"
    assert GW_PIXELS.exists(),     "gw_catchments_pixels not produced"


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

# def test_levee_rasterize_and_burn():
#     if not NLD_LEVEES.exists():
#         return
#     out_r = BRANCH_DIR / "nld_rasterized_elev_test.tif"
#     out_d = BRANCH_DIR / "dem_levee_burned_test.tif"
#     rasterize_3d_levee_lines(NLD_LEVEES, DEM, out_r)
#     burn_levee_elevations(DEM, out_r, out_d)
#     assert out_r.exists() and out_d.exists()
