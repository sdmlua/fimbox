# preprocessing HUC checker
from .preprocessing.huc_test.hucs import HUCChecker, HUCValidationError, HUCCheckResult

# DEM processing
from .preprocessing.download_data.dem_process import DEMProcessor

# Dataset downloading for a given boundary
# Get NHDPlus Dataset
from .preprocessing.download_data.nhdplus import (
    getNHDPlusData,
)  # uses EPA AWS S3 bucket, takes more time
from .preprocessing.download_data.nhdplus import (
    NWMFlowlinesDownloader,
    NWMCatchmentsDownloader,
)  # download using ArcGIS Online, faster download

# FEMA NFHL data processing module
from .preprocessing.download_data.nfhl_data import DownloadFEMANFHL

# get NLD Dataset
from .preprocessing.download_data.nld_data import DownloadNLD

# Download the OSM dataset
from .preprocessing.download_data.osm_data import DownloadOSMRoads, DownloadOSMBridges

__all__ = [
    "HUCChecker",
    "HUCValidationError",
    "HUCCheckResult",
    "DEMProcessor",
    "DownloadFEMANFHL",
    "getNHDPlusData",
    "DownloadNLD",
    "NWMFlowlinesDownloader",
    "NWMCatchmentsDownloader",
    "DownloadOSMRoads",
    "DownloadOSMBridges",
]
