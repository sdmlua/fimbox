"""
Tests for bridge DEM processing pipeline.
Step 1 (generateBridgeRaster): streams USGS LiDAR and writes per-bridge .tif
Step 2 (BridgeDEMDiff):        computes lidar_elev - dem_elev and saves bridge_elev_diff.tif
"""

import fimbox

bridge_gpkg = (
    "/Users/Supath/Downloads/SDML/FIMBOX/out/HUC08060202/osm_bridges_subset.gpkg"
)
dem_path = "/Users/Supath/Downloads/SDML/FIMBOX/out/HUC08060202/dem.tif"
out_dir = "/Users/Supath/Downloads/SDML/FIMBOX/out/HUC08060202"


# check which bridges already have rasters vs still pending (safe to run anytime)
def test_bridge_raster_status():
    info = fimbox.generateBridgeRaster(
        bridge_gpkg=bridge_gpkg,
        out_dir=out_dir,
    ).status()
    assert "total" in info


# download LiDAR and build per-bridge elevation tifs
def test_generate_bridge_raster():
    tif_dir = fimbox.generateBridgeRaster(
        bridge_gpkg=bridge_gpkg,
        out_dir=out_dir,
        resolution=10.0,
        buffer_m=10.0,
        n_workers=4,
        # id_col="my_id",  # only needed if gpkg has no 'osmid' column
    ).run()
    print(f"Per-bridge tifs saved to: {tif_dir}")


# compute difference raster
def test_bridge_dem_diff():
    out_path = fimbox.BridgeDEMDiff(
        dem_path=dem_path,
        lidar_tif_dir=f"{out_dir}/bridge_dem/lidar_osm_rasters",
        bridge_gpkg=bridge_gpkg,
        out_dir=out_dir,
        out_name="bridge_elev_diff.tif",
        n_workers=4,
    ).run()
    print(f"Bridge diff raster saved to: {out_path}")


# Run both steps end-to-end (needs pdal + laspy)
# def test_full_pipeline():
#     tif_dir = fimbox.generateBridgeRaster(
#         bridge_gpkg=bridge_gpkg,
#         out_dir=out_dir,
#         resolution=10.0,
#         n_workers=4,
#     ).run()
#
#     out_path = fimbox.BridgeDEMDiff(
#         dem_path=dem_path,
#         lidar_tif_dir=tif_dir,
#         bridge_gpkg=bridge_gpkg,
#         out_dir=out_dir,
#         n_workers=4,
#     ).run()
#     print(f"Done: {out_path}")
