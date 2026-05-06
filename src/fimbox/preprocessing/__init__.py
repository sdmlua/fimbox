"""Preprocessing subpackage exports."""

from __future__ import annotations

# calculate_branch
# level path derivation, dissolved outputs, branch list
from .calculate_branch import (
    BranchDerivation,
    BranchDerivationResult,
    derive_area_branches,
    discover_area_inputs,
)

# full branch-zero raster preprocessing pipeline
from .calculate_branch import BranchZero

# D8 flow direction pointer
from .calculate_branch import FlowdirDEM

# AGREE DEM conditioning
from .calculate_branch import HydroenforceDEM

# stream / level-path / headwater boolean grids
from .calculate_branch import (
    HeadwaterRasterizer,
    LevelPathBooleanRasterizer,
    StreamBooleanRasterizer,
)

# 3D NLD levee rasterization, DEM burning, and levee-area masking
from .calculate_branch import burn_levee_elevations, mask_levee_dem, rasterize_3d_levee_lines

# HAND Phase-3 components
from .calculate_branch import CreateHAND
from .calculate_branch import D8SlopeDEM
from .calculate_branch import FlowAccDEM
from .calculate_branch import GageCatchments, OutletBackpoolMitigate, stream_pixel_points
from .calculate_branch import StreamNetReaches
from .calculate_branch import ThalwegAdjustment
from .calculate_branch import split_derived_reaches

__all__ = [
    "BranchDerivation",
    "BranchDerivationResult",
    "derive_area_branches",
    "discover_area_inputs",
    "BranchZero",
    "FlowdirDEM",
    "D8SlopeDEM",
    "HydroenforceDEM",
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
    "GageCatchments",
    "OutletBackpoolMitigate",
    "stream_pixel_points",
    "CreateHAND",
]

# preprocess_area
# area input staging, NLD line elevation preprocessing
try:
    from .preprocess_area import getAllInputData, preprocess_nld_lines

    __all__ += ["getAllInputData", "preprocess_nld_lines"]
except ImportError:
    pass

try:
    from .download_data import (
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

# huc_test 
# HUC ID format validation and lookup utilities
try:
    from .huc_test import HUCChecker, HUCValidationError, HUCCheckResult

    __all__ += ["HUCChecker", "HUCValidationError", "HUCCheckResult"]
except ImportError:
    pass

# process_bridgedem
# LiDAR bridge point cloud → raster
# bridge elevation difference (DEM_diff) generator
try:
    from .process_bridgedem import generateBridgeRaster, BridgeDEMDiff

    __all__ += ["generateBridgeRaster", "BridgeDEMDiff"]
except ImportError:
    pass
