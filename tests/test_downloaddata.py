# Example Usage:
import logging
import fimbox

NHDboundary = "/Users/Supath/Downloads/SDML/FIMBOX/data_s3/BoundaryUnit.shp"  # will go to the AWS S3 later [for new nhddownload-no need]
test_boundary = "/Users/Supath/Downloads/SDML/FIMBOX/Sample_Data/Big_Boundary_MS.shp"

# # Testing the entire NHDPlus data extraction process along with National Flood Hazard Layer data extraction
# # This is OLDER VERSION using EPA AWS S3 Bucket which will get for whole HUC6 region--> not very effective
# def test_getNHDdata():
#     nhd_data = fimbox.getNHDPlusData(
#         NHDglobalBoundary = NHDboundary,     #Contains all NHDPlus VPU/RPU boundaries
#         # inputs_dir = None,                   #Directory to save input data, if None, direct folder directory will be created
#         boundary_path = test_boundary,          #Path to the boundary shapefile for which NHDPlus data is to be extracted, OR HUC8 ID
#         # huc8: Optional[str] = None,
#         # epsg: Optional[int] = None,
#         # out_dir: Optional[str] = None,    #Directory to save output data, if None, direct folder directory will be created
#         # auto_run= True
#     )
#     # nhd_data.process_flowlines()
#     print(f"Process successful!")

# def test_get_nfhl():
#     fimbox.DownloadFEMANFHL(
#         boundary=test_boundary,
#         out_dir="../out",
#         out_name="fema_nfhl.gpkg",
#         # log_path=None,
#     )

# def test_download_nld():
#     fimbox.DownloadNLD(
#         boundary=test_boundary,
#         out_dir="../out",
#         lines_name="NLD_Lines.gpkg",       # default; override as needed
#         polys_name="NLD_Polygons.gpkg",    # default; override as needed
#     )

# def test_get_nhddata():
#     fimbox.NWMFlowlinesDownloader().download(
#         boundary=test_boundary,
#         out_dir="../out",
#         out_name="nwm_subset_streams.gpkg",
#         out_layer="flowlines",
#     )

# def test_get_catchments():
#     fimbox.NWMCatchmentsDownloader().download(
#         boundary=test_boundary,
#         out_dir="../out",
#         out_name="nwm_subset_catchments.gpkg",
#         out_layer="catchments",
#     )

# def test_get_lakes():
#     fimbox.NWMLakesDownloader().download(
#         boundary=test_boundary,
#         out_dir="../out",
#         out_name="nwm_subset_lakes.gpkg",
#         out_layer="lakes",
#     )

# #Get all NHD Plus Data
# def test_get_nhd_all():
#     fimbox.getNHDPlusData(
#         boundary=test_boundary,
#         out_dir="../out",
#         download_flowlines=True,
#         download_catchments=True,
#         download_lakes=True,
#     )

# def test_get_dem():
#     fimbox.DEMProcessor(
#         boundary=test_boundary,
#         output_dir="../out",
#         out_name="dem.tif",             # default is 3dep_dem_10m.tif
#         resolution=10,
#     )

def test_get_osm_roads():
    fimbox.DownloadOSMRoads().download(
        boundary=test_boundary,
        out_dir="../out",
        out_name="osm_roads.gpkg",
        out_layer="osm_roads",
    )

# def test_get_osm_bridges():
#     fimbox.DownloadOSMBridges().download(
#         boundary=test_boundary,
#         out_dir="../out",
#         out_name="osm_bridges.gpkg",
#         out_layer="osm_bridges",
#     )


# # Get the HUC8 information
# def test_get_huc8_info():
#     huc8_info = fimbox.getHUC8Info(
#         boundary=test_boundary,
#         calc_overlap=True,
#         save=True,
#         out_dir="../out",
#     )
#     logging.getLogger(__name__).info(f"HUC8 info:\n{huc8_info}")


# USGS gauge points — downloads from the ArcGIS Online FeatureServer.
# Uncomment to run live; the smoke test below always runs.

# def test_download_usgs_gages():
#     """Download USGS gauges inside the test boundary into ../out/usgs_gages.gpkg."""
#     gdf = fimbox.DownloadUSGSGages().download(
#         boundary=test_boundary,
#         aoi_id="08060202",
#         out_dir="../out",
#         out_name="usgs_gages.gpkg",
#         out_layer="usgs_gages",
#     )
#     log = logging.getLogger(__name__)
#     log.info(f"USGS gauges downloaded: {len(gdf)} features")
#     assert {"location_id", "feature_id", "aoi_id", "source"}.issubset(gdf.columns)


# def test_usgs_gages_signature():
#     """Smoke test: DownloadUSGSGages is exported and has the documented API."""
#     import inspect

#     assert hasattr(fimbox, "DownloadUSGSGages")
#     sig = inspect.signature(fimbox.DownloadUSGSGages.download)
#     expected = {"boundary", "aoi_id", "out_dir", "out_name", "out_layer"}
#     assert expected.issubset(sig.parameters.keys()), (
#         f"DownloadUSGSGages.download missing kwargs: {expected - set(sig.parameters)}"
#     )
