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
from .preprocessing import (
    burn_levee_elevations,
    mask_levee_dem,
    rasterize_3d_levee_lines,
)
from .preprocessing import D8SlopeDEM
from .preprocessing import FlowAccDEM
from .preprocessing import ThalwegAdjustment
from .preprocessing import StreamNetReaches
from .preprocessing import split_derived_reaches
from .preprocessing import GageCatchments, OutletBackpoolMitigate, stream_pixel_points
from .preprocessing import MakeREM
from .preprocessing import FilterCatchments, NoFlowlinesError
from .preprocessing import CreateHAND
from .preprocessing import mask_slopes_to_catchments, rem_zeroed_masked
from .preprocessing import make_stages_and_catchlist
from .preprocessing import build_src_base
from .preprocessing import NoCrosswalkError, add_crosswalk
from .preprocessing import heal_bridges_osm
from .preprocessing import process_roads_fimpact
from .preprocessing import (
    GageBranchAssignment,
    assign_gages_to_branches,
    run_branch_crosswalk,
)
from .preprocessing import adjust_floodplains
from .preprocessing import (
    CannotConvertHydroIDsToInt16,
    convert_branch_to_int16,
    evaluate_crosswalk,
    remove_deny_list_files,
)
from .preprocessing import (
    AllBranchesResult,
    calculate_allbranches,
)
from .preprocessing import (
    BranchResult,
    AOIProcessingConfig,
    HucProcessingConfig,
    process_branches,
)
from .preprocessing import (
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
]

# FIM generation: forecast -> per-branch inundation -> AOI mosaic
from .fimgeneration import (
    FimGenerator,
    FimGenerationResult,
    Inundator,
    InundationResult,
    BranchMosaic,
    MosaicResult,
    NoForecastMatch,
    extract_feature_ids,
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

# preprocessing.process_bridgedem (bridge_lidar_raster / bridge_dem_diff)
try:
    from .preprocessing import generateBridgeRaster, BridgeDEMDiff

    __all__ += ["generateBridgeRaster", "BridgeDEMDiff"]
except ImportError:
    pass
