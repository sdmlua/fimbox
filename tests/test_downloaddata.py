# Example Usage:
from pathlib import Path

import fimbox

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]

test_boundary = PKG_ROOT / "docs" / "test_boundary" / "test_smallB.shp"
OUT_DIR = REPO_ROOT / "out"

# NWM reach IDs (matched on the ID column of the NWM layers).
test_nwm_ids = [11239455, 11239689, 11235965]

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
#         out_dir=OUT_DIR,
#         out_name="fema_nfhl.gpkg",
#         # log_path=None,
#     )

# def test_download_nld():
#     fimbox.DownloadNLD(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         lines_name="NLD_Lines.gpkg",       # default; override as needed
#         polys_name="NLD_Polygons.gpkg",    # default; override as needed
#     )

##This is for the medium range
# def test_get_nhddata():
#     fimbox.NWMFlowlinesDownloader().download(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         out_name="nwm_subset_streams.gpkg",
#         out_layer="flowlines",
#     )

##Medium range
# def test_get_catchments():
#     fimbox.NWMCatchmentsDownloader().download(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         out_name="nwm_subset_catchments.gpkg",
#         out_layer="catchments",
#     )

# def test_get_lakes():
#     fimbox.NWMLakesDownloader().download(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         out_name="nwm_subset_lakes.gpkg",
#         out_layer="lakes",
#     )


# # Get all NHD Plus Data
# def test_get_nhd_all():
#     fimbox.getNHDPlusData(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         download_flowlines=True,
#         download_catchments=True,
#         download_lakes=True,
#         source="nwmmedium",  # "nwmhigh" -> NHDPlus HR via pynhd; "ngen" -> NextGen hydrofabric. Lakes always NWM.
#         identifier="nwmmr",  # filename prefix; default "nwm" -> nwm_subset_streams.gpkg etc.
#     )


# Fetch by reach ID instead of by boundary. boundary=None drops the spatial
# filter and queries the service by attribute alone, which is how reach IDs
# resolve before any AOI exists; a `where` clause is required in that case.
# def test_get_flowlines_by_id():
#     fimbox.NWMFlowlinesDownloader().download(
#         boundary=None,
#         where="ID IN (11239455,11239689,11235965)",
#         out_dir=OUT_DIR,
#         out_name="nwm_reaches_by_id.gpkg",
#         out_layer="flowlines",
#     )

# Catchments go through this helper instead: ID is indexed on the flowline layer
# but not on the catchment layer, where an IN query is slow and fails past a few
# IDs. It fetches them spatially from the reach geometries and filters locally.
# def test_get_catchments_by_id():
#     cat = fimbox.fetch_nwm_catchments_by_id(test_nwm_ids)

# Reach IDs -> AOI boundary (their catchments dissolved into one polygon).
# strict=False keeps whatever resolves; strict=True (default) raises on unknown IDs.
# def test_reach_ids_to_boundary():
#     gdf = fimbox.reaches_to_boundary(test_nwm_ids, strict=False)
#     gdf.to_file(OUT_DIR / "reach_aoi.gpkg", driver="GPKG")

# HUC IDs -> dissolved boundary (one or many).
# def test_hucs_to_boundary():
#     gdf = fimbox.hucs_to_boundary(["08060202", "08060203"])
#     gdf.to_file(OUT_DIR / "huc_aoi.gpkg", driver="GPKG")


# High-resolution flowlines + catchments only (NHDPlus HR via pynhd).
# def test_get_nhd_hr():
#     fimbox.getNHDPlusHRData(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         download_flowlines=True,
#         download_catchments=True,
#         identifier="nwm",  # prefix for saved files
#     )


# NextGen (ngen) hydrofabric flowpaths + divides, read from the community parquet
# mirror. Only the AOI's rows are fetched, so there is no 4.9 GB CONUS download.
# Selectors: boundary=, cat_ids=, feature_ids= (NWM comids), or gages=.
# Streams come back in the canonical schema, with feature_id from the NWM comid.
def test_get_ngen_by_boundary():
    fimbox.getNgenData(
        boundary=test_boundary,
        out_dir=OUT_DIR,
        identifier="ngen",  # --> ngen_subset_streams.gpkg, ngen_catchments_proj_subset.gpkg
    )

# By ngen catchment ID, walking the network upstream from each seed.
# def test_get_ngen_by_cat_ids():
#     fimbox.getNgenData(
#         cat_ids=["cat-1096367"],
#         out_dir=OUT_DIR,
#         include_outlet=True,  # False -> stop at the seed instead of its outlet nexus
#     )

# By NWM feature ID (comid), resolved to ngen reaches via network.hf_id.
# def test_get_ngen_by_feature_ids():
#     fimbox.getNgenData(feature_ids=test_nwm_ids, out_dir=OUT_DIR)


# Bring-your-own flowlines/catchments: map your column names to the canonical
# schema (streams: ID, order_, levpa_id, feature_id[=ID]; catchments: ID).
# def test_normalize_byo_flowlines_catchments():
#     fl = fimbox.normalize_flowlines(
#         "path/to/my_flowlines.gpkg",
#         field_map={"ID": "nhdplusid", "order_": "streamorde", "levpa_id": "levelpathi"},
#     )
#     cat = fimbox.normalize_catchments(
#         "path/to/my_catchments.gpkg", field_map={"ID": "nhdplusid"}
#     )
#     assert {"ID", "order_", "levpa_id", "feature_id"}.issubset(fl.columns)
#     assert "ID" in cat.columns


# Download + process a 3DEP DEM. Reads only the AOI window straight from the
# Planetary Computer COGs over HTTP (no national VRT parse), one snapped
# reprojection, then hole-fill + clip. A resolution with no data for the AOI
# logs + raises DEMResolutionUnavailable (default stays 10 m).
# def test_get_dem():
#     fimbox.DEMProcessor(
#         boundary=test_boundary,
#         output_dir=OUT_DIR,
#         resolution=10,  # 1, 3, 10 (default), 30, 60
#         out_name="dem.tif",  # default is 3dep_dem_<res>m.tif
#         # epsg=None,                     # output CRS; None -> auto UTM zone
#         # layer=None,                    # layer name if boundary has multiple
#         # use_dask=True,                 # dask chunking for reproject/heal
#         # chunksize=None,                # None -> auto from CPU count; or set px
#     )


# "give me 1 m, else just 10 m": fallback_to_10m downgrades to 10 m (and logs)
# when the requested resolution isn't available for the AOI.
# def test_get_dem_fallback():
#     fimbox.DEMProcessor(
#         boundary=test_boundary,
#         output_dir=OUT_DIR,
#         resolution=1,                   # 1 m where lidar exists, else fall back
#         out_name="dem.tif",
#         fallback_to_10m=True,           # default False -> raises if unavailable
#     )

# Bring your own DEM: pass dem_file and it gets the SAME conditioning as a
# downloaded one (reproject -> hole-fill -> clip to boundary).
# def test_process_byo_dem():
#     fimbox.DEMProcessor(
#         boundary=test_boundary,
#         output_dir=OUT_DIR,
#         out_name="dem.tif",
#         resolution=10,
#         dem_file="path/to/my_dem.tif",
#     )

# def test_get_osm_roads():
#     fimbox.DownloadOSMRoads().download(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         out_name="osm_roads.gpkg",
#         out_layer="osm_roads",
#     )

# def test_get_osm_bridges():
#     fimbox.DownloadOSMBridges().download(
#         boundary=test_boundary,
#         out_dir=OUT_DIR,
#         out_name="osm_bridges.gpkg",
#         out_layer="osm_bridges",
#     )


# # Get the HUC8 information
# def test_get_huc8_info():
#     huc8_info = fimbox.getHUC8Info(
#         boundary=test_boundary,
#         calc_overlap=True,
#         save=True,
#         out_dir=OUT_DIR,
#     )
#     logging.getLogger(__name__).info(f"HUC8 info:\n{huc8_info}")


# USGS gauge points — downloads from the ArcGIS Online FeatureServer.
# Uncomment to run live; the smoke test below always runs.

# def test_download_usgs_gages():
#     """Download USGS gauges inside the test boundary into ../out/usgs_gages.gpkg."""
#     gdf = fimbox.DownloadUSGSGages().download(
#         boundary=test_boundary,
#         aoi_id="08060202",
#         out_dir=OUT_DIR,
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
