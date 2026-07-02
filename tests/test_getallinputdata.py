# Example Usage:
from pathlib import Path

import fimbox

# ── EDIT THIS for each HUC8 you want to process ──────────────────
CURRENT_HUC8 = "03020102"
DEM_TILES_DIR = Path("D:/SI/out/study_area/watershed-data/dem_tiles")
# ─────────────────────────────────────────────────────────────────

test_boundary = Path("./docs/study_boundary/study_area.gpkg")
test_huc8 = "08060202"  # Yazoo River basin, MS


# Run full pipeline for a single HUC8 using the pre-downloaded DEM tile.
# Change CURRENT_HUC8 above and re-run for each HUC in your study area.
def test_preprocess_all_from_boundary():
    dem_path = DEM_TILES_DIR / f"dem_{CURRENT_HUC8}.tif"
    pp = fimbox.getAllInputData(
        huc8=CURRENT_HUC8,
        out_dir="../out",
        buffer_m=5000,
        headwater_buffer_cells=8,
        get_flowlines=True,
        get_catchments=True,
        resolution="medium",
        identifier="nwmmr",
        dem=dem_path,        # use pre-downloaded 10m tile — skips 3DEP download
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
#         out_dir="../out",
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
#         out_dir="../out",
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
#         out_dir="../out",
#         buffer_m=2000,
#         headwater_buffer_cells=8,
#         get_flowlines=False,
#         get_catchments=False,
#     )
#     pp.run()


# Run individual steps
# def test_preprocess_dem_only():
#     pp = fimbox.getAllInputData(boundary=test_boundary, out_dir="../out")
#     pp.run_dem()

# def test_preprocess_nhd_only():
#     pp = fimbox.getAllInputData(boundary=test_boundary, out_dir="../out")
#     pp.run_nhd()

# def test_preprocess_nld_only():
#     pp = fimbox.getAllInputData(boundary=test_boundary, out_dir="../out")
#     pp.run_nld()

# def test_preprocess_osm_only():
#     pp = fimbox.getAllInputData(boundary=test_boundary, out_dir="../out")
#     pp.run_osm()
