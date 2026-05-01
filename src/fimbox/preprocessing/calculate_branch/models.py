"""Shared models for generalized area processing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class DataAsset:
    """Generic container describing one user-supplied or generated dataset."""

    name: str
    data: Any = None
    kind: str = "generic"
    resolution: Optional[float] = None
    units: Optional[str] = None
    crs: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AreaStagedHydroData:
    """Core staged hydrography for the user-defined area."""

    area_id: str
    boundary: Any
    boundary_clipped: Optional[Any] = None
    boundary_buffered: Optional[Any] = None
    buffered_stream_boundary: Optional[Any] = None
    stream_network: Optional[Any] = None
    catchments: Optional[Any] = None
    lakes: Optional[Any] = None
    headwater_points: Optional[Any] = None
    levee_lines: Optional[Any] = None
    levee_protected_areas: Optional[Any] = None
    levee_burned_lines: Optional[Any] = None
    landsea_mask: Optional[Any] = None
    roads: Optional[Any] = None
    bridges: Optional[Any] = None
    staged_package: Optional[Any] = None


@dataclass(slots=True)
class AreaStaticInputs:
    """Static terrain and support datasets for the area."""

    dem: Any
    dem_domain: Optional[Any] = None
    bridge_elevation_diff: Optional[Any] = None
    usgs_gages: Optional[Any] = None
    nws_lids: Optional[Any] = None
    ras2fim_points: Optional[Any] = None
    ras2fim_curves: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AreaOptionalSpatialData:
    """Optional spatial modifiers used if present."""

    fema_flood_zones: Optional[Any] = None
    levee_lines: Optional[Any] = None
    levee_protected_areas: Optional[Any] = None
    levee_burned_lines: Optional[Any] = None
    bridges: Optional[Any] = None
    roads: Optional[Any] = None
    landsea_mask: Optional[Any] = None


@dataclass(slots=True)
class AreaCalibrationData:
    """Optional observational and calibration datasets."""

    usgs_gages: Optional[Any] = None
    nws_lids: Optional[Any] = None
    usgs_rating_curves: Optional[Any] = None
    acceptable_usgs_sites: Optional[Any] = None
    nwm_recurrence_flows: Optional[Any] = None
    bankfull_flows: Optional[Any] = None
    manning_table: Optional[Any] = None
    ras2fim_rating_curves: Optional[Any] = None
    calibration_points: Optional[Any] = None
    bathymetry: Optional[Any] = None
    ai_bathymetry_support: Optional[Any] = None
    manual_calibration: Optional[Any] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AreaProcessingConfig:
    """Execution config for one area run."""

    area_id: str
    projection: Optional[str] = None
    base_branch_id: str = "0"
    evaluate_crosswalk: bool = False
    max_branch_workers: int = 1
    mask_leveed_areas: bool = True
    adjust_floodplains: bool = True
    heal_bridges: bool = True
    process_roads: bool = True
    run_bankfull_estimation: bool = True
    run_src_subdivision: bool = True
    run_usgs_adjustment: bool = True
    run_ras2fim_adjustment: bool = True
    run_spatial_adjustment: bool = True
    run_bathymetry_adjustment: bool = True
    run_manual_calibration: bool = True
    stage_min_meters: float = 0.0
    stage_interval_meters: float = 0.3048
    stage_max_meters: float = 25.0
    max_split_distance_meters: float = 1000.0
    floodplain_distance_threshold: float = 1000.0
    floodplain_z_factor: float = 1.0
    floodplain_slope_exponent: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AreaProcessingInputs:
    """All user-supplied inputs for one area."""

    hydro: AreaStagedHydroData
    static: AreaStaticInputs
    spatial: AreaOptionalSpatialData = field(default_factory=AreaOptionalSpatialData)
    calibration: AreaCalibrationData = field(default_factory=AreaCalibrationData)
    config: AreaProcessingConfig = field(default_factory=lambda: AreaProcessingConfig(area_id=""))


@dataclass(slots=True)
class AreaExecutionContext:
    """Normalized area run metadata."""

    area_id: str
    projection: Optional[str]
    base_branch_id: str
    requested_adjustments: List[str]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AreaBranchDescriptor:
    """Descriptor for one derived branch within an area."""

    branch_id: str
    branch_name: str
    kind: str = "standard"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BranchStageRecord:
    """Trace record for one branch workflow stage."""

    name: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str = "pending"
    inputs_used: List[str] = field(default_factory=list)
    outputs_created: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BranchArtifacts:
    """Structured outputs from one derived branch."""

    subset_streams: Optional[Any] = None
    subset_levelpaths: Optional[Any] = None
    subset_levelpaths_extended: Optional[Any] = None
    subset_catchments: Optional[Any] = None
    subset_headwaters: Optional[Any] = None
    clipped_dem: Optional[Any] = None
    clipped_bridge_raster: Optional[Any] = None
    reach_boolean_raster: Optional[Any] = None
    conditioned_dem: Optional[Any] = None
    floodplain_adjusted_dem: Optional[Any] = None
    flow_direction: Optional[Any] = None
    flow_accumulation: Optional[Any] = None
    headwaters_raster: Optional[Any] = None
    stream_pixels: Optional[Any] = None
    thalweg_dem: Optional[Any] = None
    slope_raster: Optional[Any] = None
    reach_catchments: Optional[Any] = None
    reach_vectors: Optional[Any] = None
    pixel_catchments: Optional[Any] = None
    rem: Optional[Any] = None
    hand: Optional[Any] = None
    stage_table: Optional[Any] = None
    catchment_list: Optional[Any] = None
    crosswalk_table: Optional[Any] = None
    hydrotable: Optional[Any] = None
    src_base_table: Optional[Any] = None
    src_table: Optional[Any] = None
    usgs_crosswalk: Optional[Any] = None
    bridge_outputs: Optional[Any] = None
    road_outputs: Optional[Any] = None
    calibration_outputs: Optional[Any] = None
    assembled_outputs: Optional[Any] = None
    additional_outputs: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BranchResult:
    """Final result from one branch run."""

    branch_name: str
    area_id: str
    branch_id: str
    status: str
    started_at: datetime
    completed_at: datetime
    stages: List[BranchStageRecord]
    artifacts: BranchArtifacts
    required_inputs_used: List[str]
    optional_inputs_used: List[str]
    summary: List[str]


@dataclass(slots=True)
class AreaStageRecord:
    """Trace record for one area workflow stage."""

    name: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str = "pending"
    inputs_used: List[str] = field(default_factory=list)
    outputs_created: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AreaArtifacts:
    """Structured area outputs plus branch collections."""

    execution_context: Optional[AreaExecutionContext] = None
    local_area_package: Optional[DataAsset] = None
    local_static_inputs: Optional[DataAsset] = None
    levelpaths: Optional[DataAsset] = None
    dissolved_levelpaths: Optional[DataAsset] = None
    extended_levelpaths: Optional[DataAsset] = None
    area_headwaters: Optional[DataAsset] = None
    catchments_levelpaths: Optional[DataAsset] = None
    dissolved_headwaters: Optional[DataAsset] = None
    branch_polygons: Optional[DataAsset] = None
    branch_list: List[AreaBranchDescriptor] = field(default_factory=list)
    base_branch_outputs: Optional[BranchResult] = None
    branch_results: List[BranchResult] = field(default_factory=list)
    branch_csv: Optional[DataAsset] = None
    area_calibration_outputs: Optional[DataAsset] = None
    area_aggregates: Optional[DataAsset] = None
    processing_time_summary: Optional[DataAsset] = None
    additional_outputs: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AreaResult:
    """Final result from one area run."""

    area_id: str
    status: str
    started_at: datetime
    completed_at: datetime
    stages: List[AreaStageRecord]
    artifacts: AreaArtifacts
    required_inputs_used: List[str]
    optional_inputs_used: List[str]
    summary: List[str]
