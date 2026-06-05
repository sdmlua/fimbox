# Example Usage:
import fimbox

test_boundary = (
    "/Users/Supath/Downloads/SDML/FIMBOX/fimbox/docs/test_boundary/test_smallB.shp"
)
test_huc8 = "08060202"  # Yazoo River basin, MS


# # Combined preprocessing pipeline tests
# # Run full pipeline from a boundary shapefile
# def test_preprocess_all_from_boundary():
#     pp = fimbox.getAllInputData(
#         boundary=test_boundary,
#         out_dir="../out",
#         buffer_m=2000,  # metres to buffer boundary for data downloads
#         headwater_buffer_cells=8,  # pixels to shrink buffer for headwater clip
#     )
#     pp.run()


# Run full pipeline from a HUC8 ID
# get_flowlines / get_catchments default to True (downloads everything,
# including OSM bridges). Set either to False to skip that dataset and use
# your own instead.
def test_preprocess_all_from_huc8():
    pp = fimbox.getAllInputData(
        huc8=test_huc8,
        out_dir="../out",
        buffer_m=2000,
        headwater_buffer_cells=8,
        get_flowlines=True,  # set False to use your own flowlines and corresponding catchments
        get_catchments=True,  # set False to skip NWM catchments--> use your own in later steps
    )
    pp.run()


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
