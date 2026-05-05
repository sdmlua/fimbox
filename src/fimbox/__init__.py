"""
Public API imported from preprocessing subpackages.
Each group is annotated with the source file it originates from.
"""

from __future__ import annotations

from .preprocessing import (
    BranchDerivation,
    BranchDerivationResult,
    derive_area_branches,
    discover_area_inputs,
)

from .preprocessing import BranchZero
from .preprocessing import HydroenforceDEM
from .preprocessing import FlowdirDEM
from .preprocessing import (
    StreamBooleanRasterizer,
    LevelPathBooleanRasterizer,
    HeadwaterRasterizer,
)
from .preprocessing import burn_levee_elevations, mask_levee_dem, rasterize_3d_levee_lines
from .preprocessing import D8SlopeDEM
from .preprocessing import FlowAccDEM
from .preprocessing import ThalwegAdjustment
from .preprocessing import StreamNetReaches
from .preprocessing import split_derived_reaches
from .preprocessing import CreateHAND

__all__ = [
    "BranchDerivation",
    "BranchDerivationResult",
    "derive_area_branches",
    "discover_area_inputs",
    "BranchZero",
    "HydroenforceDEM",
    "FlowdirDEM",
    "D8SlopeDEM",
    "StreamBooleanRasterizer",
    "LevelPathBooleanRasterizer",
    "HeadwaterRasterizer",
    "rasterize_3d_levee_lines",
    "burn_levee_elevations",
    "mask_levee_dem",
    "FlowAccDEM",
    "ThalwegAdjustment",
    "StreamNetReaches",
    "split_derived_reaches",
    "CreateHAND",
]

try:
    from .preprocessing import getAllInputData, preprocess_nld_lines

    __all__ += ["getAllInputData", "preprocess_nld_lines"]
except ImportError:
    pass

try:
    from .preprocessing import HUCChecker, HUCValidationError, HUCCheckResult

    __all__ += ["HUCChecker", "HUCValidationError", "HUCCheckResult"]
except ImportError:
    pass

# preprocessing.download_data (dem_process / utils / nhdplus /
# area_masks / nfhl_data / nld_data / osm_data)
try:
    from .preprocessing import (
        DEMProcessor,
        NHDBoundaryFinder,
        HUC8Finder,
        getHUC8Info,
        getNHDPlusData,
        NWMFlowlinesDownloader,
        NWMCatchmentsDownloader,
        NWMLakesDownloader,
        DownloadDEMDomain,
        DownloadLandSea,
        DownloadFEMANFHL,
        DownloadNLD,
        DownloadOSMRoads,
        DownloadOSMBridges,
    )

    __all__ += [
        "DEMProcessor",
        "NHDBoundaryFinder",
        "HUC8Finder",
        "getHUC8Info",
        "getNHDPlusData",
        "NWMFlowlinesDownloader",
        "NWMCatchmentsDownloader",
        "NWMLakesDownloader",
        "DownloadDEMDomain",
        "DownloadLandSea",
        "DownloadFEMANFHL",
        "DownloadNLD",
        "DownloadOSMRoads",
        "DownloadOSMBridges",
    ]
except ImportError:
    pass

# preprocessing.process_bridgedem (bridge_lidar_raster / bridge_dem_diff)
try:
    from .preprocessing import generateBridgeRaster, BridgeDEMDiff

    __all__ += ["generateBridgeRaster", "BridgeDEMDiff"]
except ImportError:
    pass
