# Example Usage:
import fimbox
NHDboundary = "/Users/Supath/Downloads/SDML/FIMBOX/data_s3/BoundaryUnit.shp" #will go to the AWS S3 later
test_boundary = "/Users/Supath/Downloads/SDML/FIMBOX/Sample_Data/SampleBoundary.shp"

# # Testing the entire NHDPlus data extraction process along with National Flood Hazard Layer data extraction
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

#Testing the download of NLD dataset
def test_download_nld():
    fimbox.NLDDownloader(
        boundary=test_boundary,
        # layer_name=None, # Specifically for the geopackage boundary files
        # output_dir= None      #Directory to save output data, else creates 'nld_data' folder in current working directory
    )
    print("NLD data download and processing completed.")