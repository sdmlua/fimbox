"""Generalized backend for area processing."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from .models import (
    AreaArtifacts,
    AreaBranchDescriptor,
    AreaExecutionContext,
    AreaProcessingInputs,
    DataAsset,
)


class GeneralizedAreaBackend:
    """Default backend mirroring the major area and branch phases."""

    def prepare_context(self, inputs: AreaProcessingInputs) -> AreaExecutionContext:
        return AreaExecutionContext(
            area_id=inputs.hydro.area_id,
            projection=inputs.config.projection,
            base_branch_id=inputs.config.base_branch_id,
            requested_adjustments=self._requested_adjustments(inputs),
            metadata={"config": inputs.config.metadata.copy()},
        )

    def localize_inputs(self, inputs: AreaProcessingInputs, context: AreaExecutionContext) -> Dict[str, DataAsset]:
        return {
            "local_area_package": self._asset("local_area_package", "bundle", "Localized staged area package"),
            "local_static_inputs": self._asset("local_static_inputs", "bundle", "Localized area support datasets"),
        }

    def derive_levelpaths(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> Dict[str, DataAsset]:
        return {
            "levelpaths": self._asset("levelpaths", "vector", "Derived levelpaths"),
            "dissolved_levelpaths": self._asset("dissolved_levelpaths", "vector", "Dissolved levelpaths"),
            "extended_levelpaths": self._asset("extended_levelpaths", "vector", "Extended levelpaths"),
            "area_headwaters": self._asset("area_headwaters", "vector", "Headwaters tied to levelpaths"),
            "catchments_levelpaths": self._asset(
                "catchments_levelpaths", "vector", "Catchments associated with levelpaths"
            ),
            "dissolved_headwaters": self._asset(
                "dissolved_headwaters", "vector", "Headwaters associated with dissolved levelpaths"
            ),
        }

    def associate_levees(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> Dict[str, DataAsset]:
        if inputs.hydro.levee_lines is None or inputs.hydro.levee_protected_areas is None:
            return {}
        return {"levee_levelpaths": self._asset("levee_levelpaths", "table", "Levee-to-levelpath associations")}

    def generate_branch_polygons(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> Dict[str, DataAsset]:
        return {"branch_polygons": self._asset("branch_polygons", "vector", "Buffered branch polygons")}

    def generate_branch_list(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> list[AreaBranchDescriptor]:
        return [
            AreaBranchDescriptor(
                branch_id=context.base_branch_id,
                branch_name=f"{inputs.hydro.area_id}_{context.base_branch_id}",
                kind="base_branch",
            )
        ]

    def prepare_base_branch(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> Dict[str, DataAsset]:
        resolution = getattr(inputs.static.dem, "resolution", None)
        return {
            "base_branch_dem": self._asset(
                "base_branch_dem", "raster", "Base-branch DEM", resolution=resolution, units="meters"
            ),
            "base_branch_bridge_raster": self._asset(
                "base_branch_bridge_raster",
                "raster",
                "Base-branch bridge elevation-difference raster",
                resolution=resolution,
                units="meters",
            ),
        }

    def build_branch_csv(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> Dict[str, DataAsset]:
        return {"branch_csv": self._asset("branch_csv", "table", "Branch tracking CSV")}

    def aggregate_area_outputs(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> Dict[str, DataAsset]:
        return {"area_aggregates": self._asset("area_aggregates", "bundle", "Aggregated area outputs")}

    def calibrate_area(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> Dict[str, DataAsset]:
        return {
            "area_calibration_outputs": self._asset(
                "area_calibration_outputs", "bundle", "Area-level calibration outputs"
            )
        }

    def finalize_area(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, artifacts: AreaArtifacts
    ) -> Dict[str, DataAsset]:
        return {
            "processing_time_summary": self._asset(
                "processing_time_summary", "table", "Area processing summary"
            )
        }

    def process_branch(
        self, inputs: AreaProcessingInputs, context: AreaExecutionContext, branch: AreaBranchDescriptor
    ) -> Dict[str, DataAsset]:
        resolution = getattr(inputs.static.dem, "resolution", None)
        return {
            "subset_streams": self._asset("subset_streams", "vector", "Branch-filtered streams"),
            "subset_levelpaths": self._asset("subset_levelpaths", "vector", "Branch-filtered levelpaths"),
            "subset_levelpaths_extended": self._asset(
                "subset_levelpaths_extended", "vector", "Extended branch levelpaths"
            ),
            "subset_catchments": self._asset("subset_catchments", "vector", "Branch-filtered catchments"),
            "subset_headwaters": self._asset("subset_headwaters", "vector", "Branch headwaters"),
            "clipped_dem": self._asset("clipped_dem", "raster", "Branch DEM", resolution=resolution, units="meters"),
            "clipped_bridge_raster": self._asset(
                "clipped_bridge_raster",
                "raster",
                "Branch bridge elevation-difference raster",
                resolution=resolution,
                units="meters",
            ),
            "reach_boolean_raster": self._asset(
                "reach_boolean_raster", "raster", "Branch stream boolean raster", resolution=resolution
            ),
            "conditioned_dem": self._asset(
                "conditioned_dem", "raster", "Conditioned branch DEM", resolution=resolution, units="meters"
            ),
            "flow_direction": self._asset("flow_direction", "raster", "D8 flow direction", resolution=resolution),
            "flow_accumulation": self._asset(
                "flow_accumulation", "raster", "Flow accumulation", resolution=resolution
            ),
            "headwaters_raster": self._asset("headwaters_raster", "raster", "Headwaters raster", resolution=resolution),
            "stream_pixels": self._asset("stream_pixels", "raster", "Derived stream pixels", resolution=resolution),
            "thalweg_dem": self._asset(
                "thalweg_dem", "raster", "Thalweg-conditioned DEM", resolution=resolution, units="meters"
            ),
            "slope_raster": self._asset("slope_raster", "raster", "Slope raster", resolution=resolution),
            "reach_vectors": self._asset("reach_vectors", "vector", "Branch reach vectors"),
            "reach_catchments": self._asset("reach_catchments", "vector", "Branch reach catchments"),
            "pixel_catchments": self._asset("pixel_catchments", "raster", "Pixel catchments", resolution=resolution),
            "rem": self._asset("rem", "raster", "Relative elevation model", resolution=resolution, units="meters"),
            "hand": self._asset("hand", "raster", "HAND raster", resolution=resolution, units="meters"),
            "stage_table": self._asset("stage_table", "table", "Stage table"),
            "catchment_list": self._asset("catchment_list", "table", "Catchment list"),
            "src_base_table": self._asset("src_base_table", "table", "Base SRC table"),
            "crosswalk_table": self._asset("crosswalk_table", "table", "Crosswalk table"),
            "hydrotable": self._asset("hydrotable", "table", "HydroTable"),
            "src_table": self._asset("src_table", "table", "SRC table"),
            "usgs_crosswalk": self._asset("usgs_crosswalk", "table", "USGS crosswalk outputs"),
            "bridge_outputs": self._asset("bridge_outputs", "table", "Bridge outputs"),
            "road_outputs": self._asset("road_outputs", "table", "Road outputs"),
            "calibration_outputs": self._asset("calibration_outputs", "bundle", "Calibration outputs"),
            "assembled_outputs": self._asset("assembled_outputs", "bundle", "Branch outputs bundle"),
        }

    def _requested_adjustments(self, inputs: AreaProcessingInputs) -> list[str]:
        cfg = inputs.config
        toggles = {
            "mask_leveed_areas": cfg.mask_leveed_areas,
            "adjust_floodplains": cfg.adjust_floodplains,
            "heal_bridges": cfg.heal_bridges,
            "process_roads": cfg.process_roads,
            "run_bankfull_estimation": cfg.run_bankfull_estimation,
            "run_src_subdivision": cfg.run_src_subdivision,
            "run_usgs_adjustment": cfg.run_usgs_adjustment,
            "run_ras2fim_adjustment": cfg.run_ras2fim_adjustment,
            "run_spatial_adjustment": cfg.run_spatial_adjustment,
            "run_bathymetry_adjustment": cfg.run_bathymetry_adjustment,
            "run_manual_calibration": cfg.run_manual_calibration,
        }
        return [name for name, enabled in toggles.items() if enabled]

    @staticmethod
    def _asset(
        name: str,
        kind: str,
        description: str,
        resolution: Any = None,
        units: str | None = None,
    ) -> DataAsset:
        return DataAsset(
            name=name,
            kind=kind,
            resolution=resolution,
            units=units,
            metadata={"description": description},
        )


def merge_area_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, DataAsset):
            normalized[key] = {
                "name": value.name,
                "kind": value.kind,
                "resolution": value.resolution,
                "units": value.units,
                "metadata": value.metadata,
            }
        elif isinstance(value, AreaBranchDescriptor):
            normalized[key] = value.__dict__
        elif isinstance(value, list) and value and isinstance(value[0], AreaBranchDescriptor):
            normalized[key] = [item.__dict__ for item in value]
        else:
            normalized[key] = value
    return normalized
