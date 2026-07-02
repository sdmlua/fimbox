"""Preprocessing subpackage exports."""

from __future__ import annotations

# calculate_branch
# level path derivation, dissolved outputs, branch list
# full branch-zero raster preprocessing pipeline
# D8 flow direction pointer
# AGREE DEM conditioning
# stream / level-path / headwater boolean grids
# 3D NLD levee rasterization, DEM burning, and levee-area masking
# HAND components
from .calculate_branch import (
    AllBranchesResult,
    AOIProcessingConfig,
    BranchDerivation,
    BranchDerivationResult,
    BranchResult,
    BranchZero,
    CannotConvertHydroIDsToInt16,
    CreateHAND,
    D8SlopeDEM,
    FilterCatchments,
    FlowAccDEM,
    FlowdirDEM,
    GageBranchAssignment,
    GageCatchments,
    HeadwaterRasterizer,
    HucProcessingConfig,
    HydroenforceDEM,
    LevelPathBooleanRasterizer,
    MakeREM,
    NoCrosswalkError,
    NoFlowlinesError,
    OutletBackpoolMitigate,
    StreamBooleanRasterizer,
    StreamNetReaches,
    ThalwegAdjustment,
    add_crosswalk,
    adjust_floodplains,
    assign_gages_to_branches,
    build_src_base,
    burn_levee_elevations,
    calculate_allbranches,
    convert_branch_to_int16,
    derive_area_branches,
    discover_area_inputs,
    evaluate_crosswalk,
    heal_bridges_osm,
    make_stages_and_catchlist,
    mask_levee_dem,
    mask_slopes_to_catchments,
    process_branches,
    process_roads_fimpact,
    rasterize_3d_levee_lines,
    rem_zeroed_masked,
    remove_deny_list_files,
    run_branch_crosswalk,
    split_derived_reaches,
    stream_pixel_points,
)

# calibrate_ratingcurve subpackage: SRC calibration pipeline + entry points
from .calibrate_ratingcurve import (
    CalibrationConfig,
    CalibrationNotImplemented,
    aggregate_branches,
    manual_calibration,
    reset_hydro_and_src,
    run_calibration,
    scan_logs,
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
    "scan_logs",
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
        DEMResolutionUnavailable,
        DownloadDEMDomain,
        DownloadFEMANFHL,
        DownloadLandSea,
        DownloadNLD,
        DownloadOSMBridges,
        DownloadOSMRoads,
        DownloadUSGSGages,
        HUC8Finder,
        NHDBoundaryFinder,
        NWMCatchmentsDownloader,
        NWMFlowlinesDownloader,
        NWMLakesDownloader,
        getHUC8Info,
        getNHDPlusData,
        getNHDPlusHRData,
        normalize_catchments,
        normalize_flowlines,
    )

    __all__ += [
        "DEMProcessor",
        "DEMResolutionUnavailable",
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
    from .huc_test import HUCChecker, HUCCheckResult, HUCValidationError

    __all__ += ["HUCChecker", "HUCValidationError", "HUCCheckResult"]
except ImportError:
    pass

# process_bridgedem
# LiDAR bridge point cloud --> raster, generate DEM difference raster
try:
    from .process_bridgedem import BridgeDEMDiff, generateBridgeRaster

    __all__ += ["generateBridgeRaster", "BridgeDEMDiff"]
except ImportError:
    pass
