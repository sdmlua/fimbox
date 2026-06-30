# Example Usage:
from pathlib import Path

import fimbox

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]

test_boundary = PKG_ROOT / "docs" / "test_boundary" / "test_smallB.shp"
OUT_DIR = REPO_ROOT / "out1"
test_huc8 = "08060202"  # Yazoo River basin, MS


# Combined preprocessing pipeline tests
# Run full pipeline from a boundary shapefile
def test_preprocess_all_from_boundary():
    pp = fimbox.getAllInputData(
        boundary=test_boundary,
        out_dir=OUT_DIR,
        buffer_m=5000,  # metres to buffer boundary for data downloads
        headwater_buffer_cells=8,  # pixels to shrink buffer for headwater clip
        get_flowlines=True,  # set False to use your own flowlines and corresponding catchments
        get_catchments=True,  # set False to skip NWM catchments--> use
        resolution="medium",  # "high" -> NHDPlus HR flowlines/catchments via pynhd; "medium" (default) -> NWM. Lakes always NWM.
        identifier="nwmmr",  # filename prefix for ALL source files; flows download->processing. Default "nwm".
    )
    pp.run()


# Bring your own flowlines / catchments / DEM (any column names, any source).
# Pass the file paths + field maps; flowlines/catchments are normalised to the
# pipeline schema (streams: ID, order_, levpa_id, feature_id[=ID]; catchments: ID),
# the DEM is reprojected/clipped/hole-filled, and all files are saved under the
# chosen identifier prefix so the whole pipeline picks them up automatically.
# def test_preprocess_byo_inputs():
#     pp = fimbox.getAllInputData(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         flowlines="path/to/my_flowlines.gpkg",
#         catchments="path/to/my_catchments.gpkg",
#         stream_fields={"ID": "nhdplusid", "order_": "streamorde", "levpa_id": "levelpathi"},
#         catchment_fields={"ID": "nhdplusid"},  # must match the flowline reach id
#         dem="path/to/my_dem.tif",  # reprojected, clipped, and hole-filled like a downloaded DEM
#         identifier="3dhp",  # files saved as 3dhp_subset_streams.gpkg etc.; whole pipeline follows it
#     )
#     pp.run()


# # Run full pipeline from a HUC8 ID
# # get_flowlines / get_catchments default to True (downloads everything,
# # including OSM bridges). Set either to False to skip that dataset and use
# # your own instead.
# def test_preprocess_all_from_huc8():
#     pp = fimbox.getAllInputData(
#         huc8=test_huc8,
#         out_dir=OUT_DIR,
#         buffer_m=2000,
#         headwater_buffer_cells=8,
#         get_flowlines=True,  # set False to use your own flowlines and corresponding catchments
#         get_catchments=True,  # set False to skip NWM catchments--> use your own in later steps
#     )
#     pp.run()


# Same pipeline, but bring your own flowlines/catchments
# (skips the NWM flowline + catchment downloads; everything else still runs)
# def test_preprocess_all_byo_flowlines_catchments():
#     pp = fimbox.getAllInputData(
#         huc8=test_huc8,
#         out_dir=OUT_DIR,
#         buffer_m=2000,
#         headwater_buffer_cells=8,
#         get_flowlines=False,
#         get_catchments=False,
#     )
#     pp.run()


# Run individual steps
# def test_preprocess_dem_only():
#     pp = fimbox.getAllInputData(boundary=test_boundary, out_dir=OUT_DIR)
#     pp.run_dem()

# def test_preprocess_nhd_only():
#     pp = fimbox.getAllInputData(boundary=test_boundary, out_dir=OUT_DIR)
#     pp.run_nhd()

# def test_preprocess_nld_only():
#     pp = fimbox.getAllInputData(boundary=test_boundary, out_dir=OUT_DIR)
#     pp.run_nld()

# def test_preprocess_osm_only():
#     pp = fimbox.getAllInputData(boundary=test_boundary, out_dir=OUT_DIR)
#     pp.run_osm()
