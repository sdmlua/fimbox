# Example Usage:
from pathlib import Path

import fimbox

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]

test_boundary = PKG_ROOT / "docs" / "test_boundary" / "test_smallB.shp"
OUT_DIR = REPO_ROOT / "out"
test_huc8 = "08060202"  # 08060202- Yazoo River basin, MS

# If Just wated to test with NWM reach IDs
test_nwm_ids = [11239459, 11239689, 11235965]
test_nwm_id = test_nwm_ids[0]  # single reach -> branch zero only

# ngen hydrofabric catchment IDs (cat-N, wb-N, or bare N all work)
test_ngen_cat_ids = ["cat-1096367", "cat-1096368"]


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
        source="nwmmedium",  # "nwmhigh" -> NHDPlus HR via pynhd; "ngen" -> NextGen hydrofabric. Lakes always NWM.
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


# Same pipeline as above, but the AOI comes from NWM reach IDs instead of a
# boundary file. Their catchments are merged into one boundary and saved as the
# usual wbd.gpkg, so every later stage behaves just as it does for a boundary run.
#
# buffer_m decides how much hydrography is staged:
#   0 (the default here) -> only the reaches you asked for
#   > 0                  -> the buffered AOI is re-queried, so neighbouring
#                           reaches inside the buffer come too, giving the
#                           upstream area HAND needs to be right
# --> out/nwm_11239455and2more/
# def test_preprocess_all_from_nwm_ids():
#     pp = fimbox.getAllInputData(
#         nwm_ids=test_nwm_ids,  # reach IDs instead of boundary=...
#         out_dir=OUT_DIR,
#         buffer_m=5000,  # 0 -> exactly these reaches; >0 --> also their neighbours
#         headwater_buffer_cells=0,  # pixels to shrink buffer for headwater clip (capped by buffer_m)
#         get_flowlines=True,  # only applies once buffer_m > 0 pulls hydrography
#         get_catchments=True,  # same
#         dem_resolution=10,  # 3DEP DEM resolution in metres (1/3/10/30/60)
#         source="nwmmedium",  # "nwmhigh" -> NHDPlus HR via pynhd; "ngen" -> NextGen hydrofabric. Lakes always NWM.
#         identifier="nwmmr",  # filename prefix for ALL source files; flows download->processing. Default "nwm".
#     )
#     pp.run()


# One reach on its own. No network to split, so only branch zero is built later.
# --> out/nwm_11239455/
# def test_preprocess_single_reach():
#     pp = fimbox.getAllInputData(
#         nwm_ids=[test_nwm_id],
#         out_dir=OUT_DIR,
#         buffer_m=0,                #0 -> just this reach and its catchment
#         headwater_buffer_cells=8,
#         dem_resolution=10,
#         source="nwmmedium",
#         identifier="nwmmr",
#     )
#     pp.run()


# Many AOIs in one call. Every getAllInputData parameter passes straight through
# and applies to each AOI in the batch.
# separate=True -> one AOI per reach
# --> out/nwm_11239455/, out/nwm_11239689/, out/nwm_11235965/
# def test_preprocess_nwm_ids_separate():
#     fimbox.getAllInputDataBatch(
#         nwm_ids=test_nwm_ids,
#         separate=True,
#         out_dir=OUT_DIR,
#         buffer_m=2000,             #each AOI widens, so each pulls its neighbours
#         headwater_buffer_cells=8,
#         dem_resolution=10,
#         get_flowlines=True,
#         get_catchments=True,
#         source="nwmmedium",        #"nwmhigh" -> NHDPlus HR; "ngen" -> NextGen hydrofabric
#         identifier="nwmmr",
#         epsg=5070,
#     )


# Nest the list to group reaches yourself: one AOI per inner list.
# --> out/nwm_11239455and1more/  (both reaches in one AOI)
#     out/nwm_11235965/          (on its own)
# def test_preprocess_nwm_ids_nested():
#     fimbox.getAllInputDataBatch(
#         nwm_ids=[[11239455, 11239689], [11235965]],
#         out_dir=OUT_DIR,
#         buffer_m=2000,
#         headwater_buffer_cells=8,
#         dem_resolution=10,
#         get_flowlines=True,
#         get_catchments=True,
#         source="nwmmedium",
#         identifier="nwmmr",
#         # run=True,                #False builds the AOIs without downloading
#         # continue_on_error=True,  #log and carry on if one AOI fails
#     )


# HUC IDs work the same way, except a flat list is one AOI per HUC (how FIM is
# normally run). Nest them or pass together=True to combine.
# --> out/HUC08060202/, out/HUC08060203/
# def test_preprocess_hucs_batch():
#     fimbox.getAllInputDataBatch(
#         hucs=["08060202", "08060203"],
#         out_dir=OUT_DIR,
#         buffer_m=2000,             #HUC/boundary runs default to 2000 m anyway
#         headwater_buffer_cells=8,
#         dem_resolution=10,
#         get_flowlines=True,
#         get_catchments=True,
#         source="nwmmedium",
#         identifier="nwmmr",
#         # together=True,           #one combined AOI -> out/HUC08060202and1more/
#         # run=True,                #False builds the AOIs without downloading
#         # continue_on_error=True,  #log and carry on if one AOI fails
#     )


# NextGen (ngen) hydrofabric instead of NWM. source="ngen" swaps only the
# flowlines/catchments; DEM, levees, OSM, gages and lakes are unchanged. The data
# is read from the community parquet mirror, so only the AOI's rows are fetched --
# no 4.9 GB CONUS GeoPackage. Streams carry feature_id from the NWM comid, so the
# existing NWM streamflow sources still drive FIM against ngen geometry.
#
# Any AOI works: a boundary file, a HUC8, ngen cat IDs, or NWM feature IDs.
# --> out/test_smallB/
# def test_preprocess_ngen_from_boundary():
#     pp = fimbox.getAllInputData(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         buffer_m=5000,
#         source="ngen",             #NextGen hydrofabric flowlines + catchments
#         identifier="ngen",         #--> ngen_subset_streams.gpkg etc.
#     )
#     pp.run()


# Same, from a HUC8: every catchment whose centroid falls inside the HUC.
# --> out/HUC08060202/
# def test_preprocess_ngen_from_huc8():
#     pp = fimbox.getAllInputData(
#         huc8=test_huc8,
#         out_dir=OUT_DIR,
#         source="ngen",
#         identifier="ngen",
#     )
#     pp.run()


# From ngen catchment IDs. The AOI is the dissolved footprint of everything
# upstream of them; subset_type decides how far that reaches.
# --> out/ngen_cat-1096367and1more/
# def test_preprocess_ngen_from_cat_ids():
#     pp = fimbox.getAllInputData(
#         cat_ids=test_ngen_cat_ids,  # implies source="ngen"
#         out_dir=OUT_DIR,
#         subset_type="nexus",       #everything draining into the outlet nexus
#         # subset_type="catchment", #stop at the selected catchments themselves
#         identifier="ngen",
#     )
#     pp.run()


# From NWM feature IDs (comids), resolved to ngen catchments via network.hf_id --
# so the same reach IDs an NWM run takes also work here.
# def test_preprocess_ngen_from_feature_ids():
#     pp = fimbox.getAllInputData(
#         feature_ids=test_nwm_ids,  # or nwm_ids=... with source="ngen"
#         out_dir=OUT_DIR,
#         source="ngen",
#         identifier="ngen",
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
