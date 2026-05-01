"""User-facing generalized area processing API.

This package intentionally avoids HUC-centric names. Internally it delegates to
the HUC/branch workflow because inundation-mapping is organized that way, but
the public interface is area-oriented:

- one area in
- branches derived if needed
- all branch processing performed
- one area result out
"""

from .area_processer import (
    AreaArtifacts,
    AreaBranchDescriptor,
    AreaCalibrationData,
    AreaExecutionContext,
    AreaOptionalSpatialData,
    AreaProcessingConfig,
    AreaProcessingInputs,
    AreaProcessor,
    AreaResult,
    AreaStageRecord,
    BranchArtifacts,
    BranchResult,
    BranchStageRecord,
    DataAsset,
    AreaStaticInputs,
    AreaStagedHydroData,
    GeneralizedAreaBackend,
    InundationMappingAreaRunConfig,
    InundationMappingAreaRunResult,
    InundationMappingAreaRunner,
)

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
    "BranchArtifacts",
    "BranchResult",
    "BranchStageRecord",
    "DataAsset",
    "AreaStaticInputs",
    "AreaStagedHydroData",
    "GeneralizedAreaBackend",
    "InundationMappingAreaRunConfig",
    "InundationMappingAreaRunResult",
    "InundationMappingAreaRunner",
]
