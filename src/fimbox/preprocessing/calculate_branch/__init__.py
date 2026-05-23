# level path derivation, dissolved outputs, branch list, levee association
from .branch_derivation import (
    BranchDerivation,
    BranchDerivationResult,
    derive_area_branches,
    discover_area_inputs,
)

# full branch-zero raster preprocessing pipeline
from .calculate_branchzero import BranchZero

# HAND production pipeline (Phase 3)
from .create_hand import CreateHAND

# D8 flow accumulation --> stream pixels
from .flowacc_dem import FlowAccDEM

# D8 flow direction pointer + D8 slope raster
from .flowdir_dem import D8SlopeDEM, FlowdirDEM

# AGREE DEM hydrological conditioning (Hellweger 1997)
from .hydroenforce_dem import HydroenforceDEM

# 3D NLD levee line rasterization, DEM burning, and levee-area masking
from .levee_rasterize import (
    burn_levee_elevations,
    mask_levee_dem,
    rasterize_3d_levee_lines,
)

# stream / level-path / headwater boolean grids
from .reach_rasterize import (
    HeadwaterRasterizer,
    LevelPathBooleanRasterizer,
    StreamBooleanRasterizer,
)

# REM / HAND computation
from .make_rem import MakeREM

# catchment filtering + flow attribute join
from .filter_catchments import FilterCatchments, NoFlowlinesError

# gage watershed delineation and outlet backpool mitigation
from .gage_catchments import GageCatchments, OutletBackpoolMitigate, stream_pixel_points

# split DEM-derived reaches and build network topology
from .split_reaches import split_derived_reaches

# REM zero/mask + slope mask
from .mask_to_catchments import mask_slopes_to_catchments, rem_zeroed_masked

# stage ladder + per-HydroID catchment list
from .stages_catchlist import make_stages_and_catchlist

# synthetic rating curve base table
from .build_src import build_src_base

# crosswalk to NWM feature_ids & final hydroTable / SRC
from .add_crosswalk import NoCrosswalkError, add_crosswalk

# USGS / AHPS / RAS2FIM gage assignment + per-branch DEM crosswalk
from .gage_crosswalk import (
    GageBranchAssignment,
    assign_gages_to_branches,
    run_branch_crosswalk,
)

# FEMA NFHL floodplain adjustment for branch-level burned DEMs
from .adjust_floodplains import adjust_floodplains

# Branch-zero crosswalk-accuracy diagnostic (intersections + network checks)
from .evaluate_crosswalk import evaluate_crosswalk

# Int16 downcast of gw_catchments + REM rasters (storage optimisation)
from .convert_to_int16 import (
    CannotConvertHydroIDsToInt16,
    convert_branch_to_int16,
)

# Branch-directory deny-list cleanup
from .outputs_cleanup import remove_deny_list_files

# Full per-AOI branch loop + AOI-level cleanup wrapper
from .calculate_allbranches import (
    AllBranchesResult,
    calculate_allbranches,
)

# Multi-branch AOI orchestrator (parallel BranchZero + CreateHAND + calibration)
# HucProcessingConfig is exported as a backwards-compatible alias for callers
# that still pass huc_dir / huc_id — both names point at the same class.
from .process_branches import (
    BranchResult,
    AOIProcessingConfig,
    HucProcessingConfig,
    process_branches,
)

# OSM bridge healing of HAND
from .heal_bridges_osm import heal_bridges_osm

# OSM road minimum-HAND FIMpact
from .process_roads_fimpact import process_roads_fimpact

# stream network delineation
from .streamnet_reaches import StreamNetReaches

# lateral thalweg adjustment + flow conditioning
from .thalweg_adjustment import ThalwegAdjustment

__all__ = [
    # branch derivation
    "BranchDerivation",
    "BranchDerivationResult",
    "derive_area_branches",
    "discover_area_inputs",
    # branch zero
    "BranchZero",
    # HAND pipeline
    "CreateHAND",
    # flow accumulation
    "FlowAccDEM",
    # flow direction + slopes
    "FlowdirDEM",
    "D8SlopeDEM",
    # AGREE DEM
    "HydroenforceDEM",
    # rasterization
    "StreamBooleanRasterizer",
    "LevelPathBooleanRasterizer",
    "HeadwaterRasterizer",
    # levees
    "rasterize_3d_levee_lines",
    "burn_levee_elevations",
    "mask_levee_dem",
    # HAND steps
    "ThalwegAdjustment",
    "StreamNetReaches",
    "split_derived_reaches",
    # gage watershed + backpool
    "GageCatchments",
    "OutletBackpoolMitigate",
    "stream_pixel_points",
    # REM / HAND
    "MakeREM",
    # catchment filtering
    "FilterCatchments",
    "NoFlowlinesError",
    # REM / slope masking to catchments
    "rem_zeroed_masked",
    "mask_slopes_to_catchments",
    # stage + catchlist builders
    "make_stages_and_catchlist",
    # SRC base + crosswalk
    "build_src_base",
    "add_crosswalk",
    "NoCrosswalkError",
    # USGS / AHPS / RAS2FIM gage crosswalk
    "GageBranchAssignment",
    "assign_gages_to_branches",
    "run_branch_crosswalk",
    # FEMA floodplain adjustment
    "adjust_floodplains",
    # diagnostics + storage optimisation
    "evaluate_crosswalk",
    "convert_branch_to_int16",
    "CannotConvertHydroIDsToInt16",
    "remove_deny_list_files",
    "AllBranchesResult",
    "calculate_allbranches",
    # multi-branch orchestrator
    "BranchResult",
    "AOIProcessingConfig",
    "HucProcessingConfig",
    "process_branches",
    # bridges + roads
    "heal_bridges_osm",
    "process_roads_fimpact",
]
