# Example Usage:
import fimbox

NHDboundary = "/Users/Supath/Downloads/SDML/FIMBOX/data_s3/BoundaryUnit.shp"  # will go to the AWS S3 later
test_boundary = "/Users/Supath/Downloads/SDML/FIMBOX/Sample_Data/test_smallB.shp"

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

# #Testing the NFHL data extraction process
# def test_get_nfhl():
#     fimbox.DownloadFEMANFHL(
#         boundary=test_boundary,
#         # output_path = None,
#         # log_path = None
#     )

# #Testing the download of NLD dataset
# def test_download_nld():
#     fimbox.DownloadNLD(
#         boundary=test_boundary,
#         # layer_name=None, # Specifically for the geopackage boundary files
#         # output_dir= None      #Directory to save output data, else creates 'nld_data' folder in current working directory
#     )
#     print("NLD data download and processing completed.")


# USING ARCGIS ONLINE, Setting up the data download process
# For NWM Flowlines
def test_get_nhdflowline():
    dl = fimbox.NWMFlowlinesDownloader()
    gdf = dl.download(
        boundary="/Users/Supath/Downloads/SDML/FIMBOX/Sample_Data/test_smallB.shp",  # Path to the boundary file (can be a shapefile or a geopackage, or bbox)
        out_dir="../out",  # Output directory
        out_name="nwm_flowlines.gpkg",  # Name of the output file, if None, default name will be used
        out_layer="flowlines",  # Name of the layer in the output gpkg to save the flowlines, if None, default name will be used
        boundary_layer=None,  # Name of the layer in the boundary gpkg to use as boundary, if None, first layer will be used
    )
    print("NHDPlus data download and processing completed.")


# For NWM Catchments
def test_get_nhdcatchment():
    dl = fimbox.NWMCatchmentsDownloader(debug=True)
    gdf = dl.download(
        boundary="/Users/Supath/Downloads/SDML/FIMBOX/Sample_Data/test_smallB.shp",  # Path to the boundary file (can be a shapefile or a geopackage, or bbox)
        out_dir="../out",  # Output directory
        out_name="nwm_catchments.gpkg",  # Name of the output file, if None, default name will be used
        out_layer="catchments",  # Name of the layer in the output gpkg to save the flowlines, if None, default name will be used
        boundary_layer=None,  # Name of the layer in the boundary gpkg to use as boundary, if None, first layer will be used
    )
    print("NHDPlus data download and processing completed.")


# #DOWNLOAD THE OSM DATASETS; ROADS AND BRIDGES
# #ROADS
# def test_get_osmdata():
#     roads = fimbox.DownloadOSMRoads()
#     roads.download(
#         boundary = test_boundary,
#         out_dir= "../out",      #Output directory
#         out_name= "osm_roads.gpkg",     #Name of the output file, if None, default name will be used
#         out_layer = "osm_roads",        #Name of the layer in the output gpkg to save the roads data, if None, default name will be used
#         boundary_layer= None,       #Name of the layer in the boundary gpkg to use as boundary, if None, first layer will be used
#         boundary_crs = None,        #CRS of the boundary file, if None, CRS will be inferred from the boundary file
#     )

# #DOWNLOAD THE OSM DATASETS; Bridge
# def test_get_osmdata():
#     roads = fimbox.DownloadOSMBridges()
#     roads.download(
#         boundary = test_boundary,
#         out_dir= "../out",      #Output directory
#         out_name= "osm_bridges.gpkg",     #Name of the output file, if None, default name will be used
#         out_layer = "osm_bridge",        #Name of the layer in the output gpkg to save the roads data, if None, default name will be used
#         boundary_layer= None,       #Name of the layer in the boundary gpkg to use as boundary, if None, first layer will be used
#         boundary_crs = None,        #CRS of the boundary file, if None, CRS will be inferred from the boundary file
#     )
