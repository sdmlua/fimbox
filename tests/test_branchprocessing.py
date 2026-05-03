"""
Branch processing tests — BranchDerivation, BranchZero, and individual components.
"""

import fimbox
from fimbox import (
    BranchDerivation,
    BranchZero,
    HydroenforceDEM,
    FlowdirDEM,
    StreamBooleanRasterizer,
    LevelPathBooleanRasterizer,
    HeadwaterRasterizer,
    rasterize_3d_levee_lines,
    burn_levee_elevations,
)
from pathlib import Path

# input data paths 
OUT_DIR = Path("/Users/supath/Downloads/MSResearch/FIMBOX/out/HUC03020202")

DEM              = OUT_DIR / "dem.tif"
STREAMS          = OUT_DIR / "nwm_subset_streams.gpkg"
BOUNDARY         = OUT_DIR / "wbd.gpkg"
BOUNDARY_BUF     = OUT_DIR / "wbd_buffered.gpkg"
CATCHMENTS       = OUT_DIR / "nwm_catchments_proj_subset.gpkg"
HEADWATERS       = OUT_DIR / "nwm_headwater_points_subset.gpkg"
LEVELPATH_EXT    = OUT_DIR / "nwm_subset_streams_levelPaths_extended.gpkg"
BRIDGE_DIFF      = OUT_DIR / "bridge_elev_diff.tif"   
NLD_LEVEES       = OUT_DIR / "3d_nld_subset_levees_burned.gpkg"


# +++Branch Derivation+++
def test_branch_derivation():
    """Derive level paths, branch polygons, and branch list from staged NWM data."""
    bd = BranchDerivation(
        out_dir=OUT_DIR,
        branch_id_attribute="levpa_id",
        reach_id_attribute="ID",
        branch_buffer_distance_meters=1000.0,
    )
    result = bd.run()

    assert result.dissolved_levelpaths.exists(), "dissolved levelpaths not written"
    assert result.branch_polygons.exists(),      "branch polygons not written"
    assert result.branch_list.exists(),          "branch list file not written"
    assert len(result.branch_dataframe) > 0,     "branch dataframe is empty"

    print(f"\nLevel paths    : {result.levelpaths}")
    print(f"Dissolved      : {result.dissolved_levelpaths}")
    print(f"Branch polygons: {result.branch_polygons}")
    print(f"Branch list    : {result.branch_list}")
    print(f"Branch count   : {len(result.branch_dataframe)}")
    if result.levee_levelpaths:
        print(f"Levee-levelpath: {result.levee_levelpaths}")


# +++Branch Zero — full pipeline+++
def test_branch_zero_full():
    """Run the complete branch-zero preprocessing pipeline."""
    bz = BranchZero(
        dem_path=DEM,
        streams_gpkg=STREAMS,
        boundary_gpkg=BOUNDARY_BUF,
        out_dir=OUT_DIR,
        bridge_elev_diff_path=BRIDGE_DIFF,          # optional — comment out if not available
        headwaters_gpkg=HEADWATERS,                  # optional
        levelpaths_extended_gpkg=LEVELPATH_EXT,      # optional
        agree_buffer_m=15.0,
        agree_smooth_drop=10.0,
        agree_sharp_drop=1000.0,
        branch_zero_id="0",
    )
    outputs = bz.run()

    assert outputs["dem"].exists(),                  "clipped DEM not written"
    assert outputs["flows_grid_boolean"].exists(),   "stream boolean grid not written"
    assert outputs["dem_burned"].exists(),           "AGREE DEM not written"
    assert outputs["dem_burned_filled"].exists(),    "pit-filled DEM not written"
    assert outputs["flowdir_d8"].exists(),           "D8 flow direction not written"

    print("\nBranch-zero outputs:")
    for k, v in outputs.items():
        print(f"  {k:30s}: {v}")


# +++Individual component tests+++
# def test_stream_boolean_rasterizer():
#     """Rasterize NWM streams to 1/0 Int32 boolean grid."""
#     import rasterio
#     out = OUT_DIR / "branches/0/flows_grid_boolean_test.tif"
#     StreamBooleanRasterizer(STREAMS, DEM, out).run()
#     with rasterio.open(out) as src:
#         data = src.read(1)
#         assert data.max() == 1
#         assert data.min() == 0
#         print(f"\nStream pixels: {(data == 1).sum()}")


# def test_headwater_rasterizer():
#     """Rasterize NWM headwater points to 1/0 boolean grid."""
#     out = OUT_DIR / "branches/0/headwaters_test.tif"
#     HeadwaterRasterizer(HEADWATERS, DEM, out).run()
#     assert out.exists()
#     print(f"\nHeadwater raster written: {out}")


# def test_levelpath_boolean_rasterizer():
#     """Rasterize extended level path streams to 1/0 boolean grid."""
#     out = OUT_DIR / "flows_grid_boolean_test.tif"
#     LevelPathBooleanRasterizer(LEVELPATH_EXT, DEM, out).run()
#     assert out.exists()
#     print(f"\nLevel path boolean grid written: {out}")


# def test_hydroenforce_dem():
#     """Run AGREE DEM conditioning standalone on the stream boolean grid."""
#     from pathlib import Path
#     import tempfile, shutil
#     streams_bool = OUT_DIR / "branches/0/flows_grid_boolean_0.tif"
#     workspace    = OUT_DIR / "branches/0"
#     out          = OUT_DIR / "branches/0/dem_burned_test.tif"
#     HydroenforceDEM(
#         rivers_raster=streams_bool,
#         dem=DEM,
#         output_raster=out,
#         workspace=workspace,
#         buffer_dist=15.0,
#         smooth_drop=10.0,
#         sharp_drop=1000.0,
#         stream_value=1,         # change if your raster uses a different burn value
#         keep_intermediates=False,
#     ).run()
#     assert out.exists()
#     print(f"\nAGREE DEM written: {out}")


# def test_flowdir_dem():
#     """Compute D8 flow direction from a pit-filled DEM."""
#     pit_filled = OUT_DIR / "branches/0/dem_burned_filled_0.tif"
#     out        = OUT_DIR / "branches/0/flowdir_d8_test.tif"
#     FlowdirDEM(pit_filled, out).run()
#     assert out.exists()
#     print(f"\nD8 flow direction written: {out}")


# def test_levee_rasterize_and_burn():
#     """Rasterize 3D NLD levee lines and burn into DEM (only if levee data exists)."""
#     if not NLD_LEVEES.exists():
#         print("\nSkipped: NLD levee GeoPackage not found")
#         return
#     out_raster = OUT_DIR / "branches/0/nld_rasterized_elev_test.tif"
#     out_dem    = OUT_DIR / "branches/0/dem_levee_burned_test.tif"
#     rasterize_3d_levee_lines(NLD_LEVEES, DEM, out_raster)
#     burn_levee_elevations(DEM, out_raster, out_dem)
#     assert out_raster.exists()
#     assert out_dem.exists()
#     print(f"\nLevee raster : {out_raster}")
#     print(f"Burned DEM   : {out_dem}")


# +++Branch Zero with levees+++

# def test_branch_zero_with_levees():
#     """Full branch-zero pipeline including levee rasterization from 3D GeoPackage."""
#     bz = BranchZero(
#         dem_path=DEM,
#         streams_gpkg=STREAMS,
#         boundary_gpkg=BOUNDARY_BUF,
#         out_dir=OUT_DIR,
#         levee_gpkg_path=NLD_LEVEES,          # 3D levee lines with Z elevation
#         headwaters_gpkg=HEADWATERS,
#         levelpaths_extended_gpkg=LEVELPATH_EXT,
#         agree_buffer_m=15.0,
#         agree_smooth_drop=10.0,
#         agree_sharp_drop=1000.0,
#         keep_agree_intermediates=False,       # set True to inspect AGREE workspace files
#     )
#     outputs = bz.run()
#     assert outputs["dem_burned_filled"].exists()
#     assert outputs["flowdir_d8"].exists()
#     print("\nOutputs:", list(outputs.keys()))
