# 3DEP DEM and custom DEM processing module
from .dem_process import DEMProcessor

# Utils
from .utils import NHDBoundaryFinder, HUC8Finder, getHUC8Info

# Get NHDPlus Dataset
from .nhdplus import (
    getNHDPlusData,
    getNHDPlusHRData,
    normalize_flowlines,
    normalize_catchments,
    NWMFlowlinesDownloader,
    NWMCatchmentsDownloader,
    NWMLakesDownloader,
)

# Get static area masks
from .area_masks import DownloadDEMDomain, DownloadLandSea

# FEMA National Flood Hazard Layer (NFHL) data processing module
from .nfhl_data import DownloadFEMANFHL

# Get the NLD Dataset
from .nld_data import DownloadNLD

# Get the OSM Roads
from .osm_data import DownloadOSMRoads, DownloadOSMBridges

# Download USGS gauge points (CONUS) from ArcGIS Online FeatureServer
from .usgs_gages import DownloadUSGSGages

__all__ = [
    "DEMProcessor",
    "NHDBoundaryFinder",
    "HUC8Finder",
    "getHUC8Info",
    "DownloadFEMANFHL",
    "getNHDPlusData",
    "getNHDPlusHRData",
    "normalize_flowlines",
    "normalize_catchments",
    "DownloadNLD",
    "NWMFlowlinesDownloader",
    "NWMCatchmentsDownloader",
    "NWMLakesDownloader",
    "DownloadDEMDomain",
    "DownloadLandSea",
    "DownloadOSMRoads",
    "DownloadOSMBridges",
    "DownloadUSGSGages",
]
