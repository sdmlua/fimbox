"""Compatibility facade for area processing."""

from .backend import GeneralizedAreaBackend
from .im_runner import (
    InundationMappingAreaRunConfig,
    InundationMappingAreaRunResult,
    InundationMappingAreaRunner,
)
from .models import (
    AreaArtifacts,
    AreaBranchDescriptor,
    AreaCalibrationData,
    AreaExecutionContext,
    AreaOptionalSpatialData,
    AreaProcessingConfig,
    AreaProcessingInputs,
    AreaResult,
    AreaStageRecord,
    AreaStaticInputs,
    AreaStagedHydroData,
    BranchArtifacts,
    BranchResult,
    BranchStageRecord,
    DataAsset,
)
from .workflow import AreaProcessor

__all__ = [
    "AreaArtifacts",
    "AreaBranchDescriptor",
    "AreaCalibrationData",
    "AreaExecutionContext",
    "AreaOptionalSpatialData",
    "AreaProcessingConfig",
    "AreaProcessingInputs",
    "AreaProcessor",
    "AreaResult",
    "AreaStageRecord",
    "AreaStaticInputs",
    "AreaStagedHydroData",
    "BranchArtifacts",
    "BranchResult",
    "BranchStageRecord",
    "DataAsset",
    "GeneralizedAreaBackend",
    "InundationMappingAreaRunConfig",
    "InundationMappingAreaRunResult",
    "InundationMappingAreaRunner",
]
