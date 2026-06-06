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
from .calculate_branch import (
    burn_levee_elevations,
    mask_levee_dem,
    rasterize_3d_levee_lines,
)

# HAND components
from .calculate_branch import CreateHAND
from .calculate_branch import D8SlopeDEM
from .calculate_branch import FlowAccDEM
from .calculate_branch import (
    GageCatchments,
    OutletBackpoolMitigate,
    stream_pixel_points,
)
from .calculate_branch import MakeREM
from .calculate_branch import FilterCatchments, NoFlowlinesError
from .calculate_branch import StreamNetReaches
from .calculate_branch import ThalwegAdjustment
from .calculate_branch import split_derived_reaches
from .calculate_branch import mask_slopes_to_catchments, rem_zeroed_masked
from .calculate_branch import make_stages_and_catchlist
from .calculate_branch import build_src_base
from .calculate_branch import NoCrosswalkError, add_crosswalk
from .calculate_branch import heal_bridges_osm
from .calculate_branch import process_roads_fimpact
from .calculate_branch import (
    GageBranchAssignment,
    assign_gages_to_branches,
    run_branch_crosswalk,
)
from .calculate_branch import adjust_floodplains
from .calculate_branch import (
    CannotConvertHydroIDsToInt16,
    convert_branch_to_int16,
    evaluate_crosswalk,
    remove_deny_list_files,
)
from .calculate_branch import (
    AllBranchesResult,
    calculate_allbranches,
)
from .calculate_branch import (
    BranchResult,
    AOIProcessingConfig,
    HucProcessingConfig,
    process_branches,
)

# calibrate_ratingcurve subpackage: SRC calibration pipeline + entry points
from .calibrate_ratingcurve import (
    CalibrationConfig,
    CalibrationNotImplemented,
    aggregate_branches,
    manual_calibration,
    reset_hydro_and_src,
    run_calibration,
)

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
    "MakeREM",
    "FilterCatchments",
    "NoFlowlinesError",
    "CreateHAND",
    "rem_zeroed_masked",
    "mask_slopes_to_catchments",
    "make_stages_and_catchlist",
    "build_src_base",
    "add_crosswalk",
    "NoCrosswalkError",
    "heal_bridges_osm",
    "process_roads_fimpact",
    "GageBranchAssignment",
    "assign_gages_to_branches",
    "run_branch_crosswalk",
    "adjust_floodplains",
    "evaluate_crosswalk",
    "convert_branch_to_int16",
    "CannotConvertHydroIDsToInt16",
    "remove_deny_list_files",
    "AllBranchesResult",
    "calculate_allbranches",
    "BranchResult",
    "AOIProcessingConfig",
    "HucProcessingConfig",
    "process_branches",
    "CalibrationConfig",
    "CalibrationNotImplemented",
    "aggregate_branches",
    "manual_calibration",
    "reset_hydro_and_src",
    "run_calibration",
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
        getNHDPlusHRData,
        normalize_flowlines,
        normalize_catchments,
        NWMFlowlinesDownloader,
        NWMCatchmentsDownloader,
        NWMLakesDownloader,
        DownloadDEMDomain,
        DownloadLandSea,
        DownloadFEMANFHL,
        DownloadNLD,
        DownloadOSMRoads,
        DownloadOSMBridges,
        DownloadUSGSGages,
    )

    __all__ += [
        "DEMProcessor",
        "NHDBoundaryFinder",
        "HUC8Finder",
        "getHUC8Info",
        "getNHDPlusData",
        "getNHDPlusHRData",
        "normalize_flowlines",
        "normalize_catchments",
        "NWMFlowlinesDownloader",
        "NWMCatchmentsDownloader",
        "NWMLakesDownloader",
        "DownloadDEMDomain",
        "DownloadLandSea",
        "DownloadFEMANFHL",
        "DownloadNLD",
        "DownloadOSMRoads",
        "DownloadOSMBridges",
        "DownloadUSGSGages",
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
# LiDAR bridge point cloud --> raster, generate DEM difference raster
try:
    from .process_bridgedem import generateBridgeRaster, BridgeDEMDiff

    __all__ += ["generateBridgeRaster", "BridgeDEMDiff"]
except ImportError:
    pass
