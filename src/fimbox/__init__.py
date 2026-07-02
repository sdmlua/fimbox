"""
Public API imported from preprocessing subpackages.
Each group is annotated with the source file it originates from.
"""

from __future__ import annotations

from .preprocessing import (
    AllBranchesResult,
    AOIProcessingConfig,
    BranchDerivation,
    BranchDerivationResult,
    BranchResult,
    BranchZero,
    CalibrationConfig,
    CalibrationNotImplemented,
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
    aggregate_branches,
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
    manual_calibration,
    mask_levee_dem,
    mask_slopes_to_catchments,
    process_branches,
    process_roads_fimpact,
    rasterize_3d_levee_lines,
    rem_zeroed_masked,
    remove_deny_list_files,
    reset_hydro_and_src,
    run_branch_crosswalk,
    run_calibration,
    scan_logs,
    split_derived_reaches,
    stream_pixel_points,
)

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

# FIM generation: forecast -> per-branch inundation -> AOI mosaic
from .fimgeneration import (
    BranchMosaic,
    FimGenerationResult,
    FimGenerator,
    InundationResult,
    Inundator,
    MosaicResult,
    NoForecastMatch,
    extract_feature_ids,
    generateFIM,
)

__all__ += [
    "FimGenerator",
    "FimGenerationResult",
    "Inundator",
    "InundationResult",
    "BranchMosaic",
    "MosaicResult",
    "NoForecastMatch",
    "extract_feature_ids",
    "generateFIM",
]

# Streamflow retrieval / plotting / statistics. Heavy optional deps (teehr,
# s3fs, matplotlib, ...) are imported lazily inside the functions, so this
# import is light; guard it anyway so a partial install never breaks `import
# fimbox`.
try:
    from .streamflow import (
        GeoglowsData,
        NWMForecast,
        NWMRetrospective,
        StreamflowMetrics,
        StreamflowPipeline,
        USGSData,
        calculate_statistics,
        compute_metrics,
        get_usgs_fid_pairs,
        getNWMforecast,
        getNWMretrospective,
        plot_comparison,
        plot_nwm,
        plot_usgs,
    )

    __all__ += [
        "NWMRetrospective",
        "NWMForecast",
        "USGSData",
        "GeoglowsData",
        "StreamflowPipeline",
        "getNWMretrospective",
        "getNWMforecast",
        "get_usgs_fid_pairs",
        "plot_nwm",
        "plot_usgs",
        "plot_comparison",
        "calculate_statistics",
        "compute_metrics",
        "StreamflowMetrics",
    ]
except ImportError:
    pass

try:
    from .preprocessing import getAllInputData, preprocess_nld_lines

    __all__ += ["getAllInputData", "preprocess_nld_lines"]
except ImportError:
    pass

try:
    from .preprocessing import HUCChecker, HUCCheckResult, HUCValidationError

    __all__ += ["HUCChecker", "HUCValidationError", "HUCCheckResult"]
except ImportError:
    pass

# preprocessing.download_data (dem_process / utils / nhdplus /
# area_masks / nfhl_data / nld_data / osm_data)
try:
    from .preprocessing import (
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

# preprocessing.process_bridgedem (bridge_lidar_raster / bridge_dem_diff)
try:
    from .preprocessing import BridgeDEMDiff, generateBridgeRaster

    __all__ += ["generateBridgeRaster", "BridgeDEMDiff"]
except ImportError:
    pass
