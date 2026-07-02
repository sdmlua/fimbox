# 3DEP DEM (Planetary Computer STAC) + custom DEM processing module
# Get static area masks
from .area_masks import DownloadDEMDomain, DownloadLandSea
from .dem_process import (
    DEMProcessor,
    DEMResolutionUnavailable,
)

# FEMA National Flood Hazard Layer (NFHL) data processing module
from .nfhl_data import DownloadFEMANFHL

# Get NHDPlus Dataset
from .nhdplus import (
    NWMCatchmentsDownloader,
    NWMFlowlinesDownloader,
    NWMLakesDownloader,
    getNHDPlusData,
    getNHDPlusHRData,
    normalize_catchments,
    normalize_flowlines,
)

# Get the NLD Dataset
from .nld_data import DownloadNLD

# Get the OSM Roads
from .osm_data import DownloadOSMBridges, DownloadOSMRoads

# Download USGS gauge points (CONUS) from ArcGIS Online FeatureServer
from .usgs_gages import DownloadUSGSGages

# Utils
from .utils import HUC8Finder, NHDBoundaryFinder, getHUC8Info

__all__ = [
    "DEMProcessor",
    "DEMResolutionUnavailable",
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
