"""Generalized area workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from .backend import GeneralizedAreaBackend, merge_area_payload
from .models import (
    AreaArtifacts,
    AreaBranchDescriptor,
    AreaExecutionContext,
    AreaProcessingInputs,
    AreaResult,
    AreaStageRecord,
    BranchArtifacts,
    BranchResult,
    BranchStageRecord,
)


class AreaProcessor:
    """User-facing generalized area processor."""

    def __init__(
        self,
        inputs: AreaProcessingInputs,
        backend: Optional[GeneralizedAreaBackend] = None,
    ) -> None:
        self.inputs = inputs
        self.backend = backend or GeneralizedAreaBackend()
        self._artifacts = AreaArtifacts()
        self._stages: List[AreaStageRecord] = []
        self._required_inputs_used: List[str] = []
        self._optional_inputs_used: List[str] = []
        self._summary: List[str] = []

    def run(self) -> AreaResult:
        started_at = self._utcnow()
        context = self.prepare_context()
        self.validate_inputs()
        self.localize_inputs(context)
        self.derive_levelpaths(context)
        self.associate_levees(context)
        self.generate_branch_polygons(context)
        self.generate_branch_list(context)
        self.prepare_base_branch(context)
        self.process_branches(context)
        self.aggregate_outputs(context)
        self.calibrate_area(context)
        self.finalize_area(context)
        completed_at = self._utcnow()
        return AreaResult(
            area_id=self.inputs.hydro.area_id,
            status="completed",
            started_at=started_at,
            completed_at=completed_at,
            stages=self._stages,
            artifacts=self._artifacts,
            required_inputs_used=self._required_inputs_used,
            optional_inputs_used=self._optional_inputs_used,
            summary=self._summary,
        )

    def prepare_context(self) -> AreaExecutionContext:
        stage = self._start_stage("prepare_context", ["area_id", "config"])
        context = self.backend.prepare_context(self.inputs)
        self._artifacts.execution_context = context
        self._complete_stage(
            stage,
            ["execution_context"],
            ["Prepared normalized execution context for the study area."],
            {"context": context.__dict__},
        )
        return context

    def validate_inputs(self) -> None:
        stage = self._start_stage("validate_inputs", ["hydro.boundary", "hydro.stream_network", "static.dem"])
        missing = []
        if self.inputs.hydro.boundary is None:
            missing.append("boundary")
        if self.inputs.hydro.stream_network is None:
            missing.append("stream_network")
        if self.inputs.static.dem is None:
            missing.append("dem")
        if missing:
            stage.status = "failed"
            stage.notes.append(f"Missing required inputs: {', '.join(missing)}")
            stage.completed_at = self._utcnow()
            raise ValueError(f"Missing required inputs: {', '.join(missing)}")
        self._required_inputs_used.extend(["boundary", "stream_network", "dem"])
        self._complete_stage(stage, ["validated_inputs"], ["Validated the minimum area inputs."])

    def localize_inputs(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("localize_inputs", ["hydro.staged_package", "static"])
        outputs = self.backend.localize_inputs(self.inputs, context)
        self._apply_area_outputs(outputs)
        self._complete_stage(stage, list(outputs.keys()), ["Localized staged area inputs."], merge_area_payload(outputs))

    def derive_levelpaths(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage(
            "derive_levelpaths",
            ["hydro.stream_network", "hydro.buffered_stream_boundary", "hydro.catchments", "hydro.lakes"],
        )
        outputs = self.backend.derive_levelpaths(self.inputs, context, self._artifacts)
        self._apply_area_outputs(outputs)
        self._complete_stage(
            stage,
            list(outputs.keys()),
            ["Derived levelpaths and linked hydrography needed to define processing branches."],
            merge_area_payload(outputs),
        )

    def associate_levees(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("associate_levees", ["hydro.levee_lines", "hydro.levee_protected_areas"])
        self._mark_optional_input("levee_lines", self.inputs.hydro.levee_lines)
        self._mark_optional_input("levee_protected_areas", self.inputs.hydro.levee_protected_areas)
        outputs = self.backend.associate_levees(self.inputs, context, self._artifacts)
        self._apply_area_outputs(outputs)
        self._complete_stage(stage, list(outputs.keys()), ["Associated levees with derived levelpaths."], merge_area_payload(outputs))

    def generate_branch_polygons(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("generate_branch_polygons", ["artifacts.dissolved_levelpaths", "hydro.boundary_buffered"])
        outputs = self.backend.generate_branch_polygons(self.inputs, context, self._artifacts)
        self._apply_area_outputs(outputs)
        self._complete_stage(stage, list(outputs.keys()), ["Generated branch polygons from the derived network."], merge_area_payload(outputs))

    def generate_branch_list(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("generate_branch_list", ["artifacts.dissolved_levelpaths"])
        branch_list = self.backend.generate_branch_list(self.inputs, context, self._artifacts)
        self._artifacts.branch_list = branch_list
        self._complete_stage(stage, ["branch_list"], ["Built the list of branches to process within the area."], {"branch_list": [b.__dict__ for b in branch_list]})

    def prepare_base_branch(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("prepare_base_branch", ["hydro.boundary_buffered", "static.dem", "static.bridge_elevation_diff"])
        outputs = self.backend.prepare_base_branch(self.inputs, context, self._artifacts)
        self._apply_area_outputs(outputs)
        self._complete_stage(stage, list(outputs.keys()), ["Prepared the base branch inputs for the whole area."], merge_area_payload(outputs))

    def process_branches(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("process_branches", ["artifacts.branch_list", "artifacts.branch_polygons"])
        branch_runs = []
        for branch in self._artifacts.branch_list:
            result = self._run_branch(branch, context)
            if branch.kind == "base_branch":
                self._artifacts.base_branch_outputs = result
            else:
                self._artifacts.branch_results.append(result)
            branch_runs.append({"branch_id": branch.branch_id, "status": result.status})
        outputs = self.backend.build_branch_csv(self.inputs, context, self._artifacts)
        self._apply_area_outputs(outputs)
        self._complete_stage(
            stage,
            ["base_branch_outputs", "branch_results", *list(outputs.keys())],
            ["Processed all derived branches and created branch tracking outputs."],
            {"branch_runs": branch_runs, **merge_area_payload(outputs)},
        )

    def aggregate_outputs(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("aggregate_outputs", ["artifacts.base_branch_outputs", "artifacts.branch_results"])
        outputs = self.backend.aggregate_area_outputs(self.inputs, context, self._artifacts)
        self._apply_area_outputs(outputs)
        self._complete_stage(stage, list(outputs.keys()), ["Aggregated branch outputs back to the area level."], merge_area_payload(outputs))

    def calibrate_area(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("calibrate_area", ["calibration", "artifacts.area_aggregates"])
        optional_map = {
            "usgs_gages": self.inputs.calibration.usgs_gages,
            "usgs_rating_curves": self.inputs.calibration.usgs_rating_curves,
            "ras2fim_rating_curves": self.inputs.calibration.ras2fim_rating_curves,
            "calibration_points": self.inputs.calibration.calibration_points,
            "bathymetry": self.inputs.calibration.bathymetry,
            "manual_calibration": self.inputs.calibration.manual_calibration,
        }
        for key, value in optional_map.items():
            self._mark_optional_input(key, value)
        outputs = self.backend.calibrate_area(self.inputs, context, self._artifacts)
        self._apply_area_outputs(outputs)
        self._complete_stage(stage, list(outputs.keys()), ["Applied optional calibration and adjustment routines."], merge_area_payload(outputs))

    def finalize_area(self, context: AreaExecutionContext) -> None:
        stage = self._start_stage("finalize_area", ["artifacts.area_aggregates", "artifacts.area_calibration_outputs"])
        outputs = self.backend.finalize_area(self.inputs, context, self._artifacts)
        self._apply_area_outputs(outputs)
        self._summary.extend(
            [
                "Localized area inputs and derived branches from the supplied hydrography.",
                "Processed base and derived branches into terrain, hydrologic, and hydraulic outputs.",
                "Aggregated branch results and applied optional calibration at the area level.",
            ]
        )
        self._complete_stage(stage, list(outputs.keys()), ["Finalized area summaries and timing outputs."], merge_area_payload(outputs))

    def _run_branch(self, branch: AreaBranchDescriptor, context: AreaExecutionContext) -> BranchResult:
        started_at = self._utcnow()
        outputs = self.backend.process_branch(self.inputs, context, branch)
        artifacts = BranchArtifacts()
        for key, value in outputs.items():
            if hasattr(artifacts, key):
                setattr(artifacts, key, value)
            else:
                artifacts.additional_outputs[key] = value
        stage = BranchStageRecord(
            name="process_branch",
            started_at=started_at,
            completed_at=self._utcnow(),
            status="completed",
            inputs_used=["branch_descriptor", "area_inputs"],
            outputs_created=list(outputs.keys()),
            notes=["Processed one derived branch using the generalized branch sequence."],
            payload=merge_area_payload(outputs),
        )
        return BranchResult(
            branch_name=branch.branch_name,
            area_id=self.inputs.hydro.area_id,
            branch_id=branch.branch_id,
            status="completed",
            started_at=started_at,
            completed_at=self._utcnow(),
            stages=[stage],
            artifacts=artifacts,
            required_inputs_used=["stream_network", "dem"],
            optional_inputs_used=self._optional_inputs_used.copy(),
            summary=["Generated all generalized outputs for one branch."],
        )

    def _apply_area_outputs(self, outputs: dict[str, Any]) -> None:
        for key, value in outputs.items():
            if hasattr(self._artifacts, key):
                setattr(self._artifacts, key, value)
            else:
                self._artifacts.additional_outputs[key] = value

    def _start_stage(self, name: str, inputs_used: Optional[Iterable[str]] = None) -> AreaStageRecord:
        stage = AreaStageRecord(
            name=name,
            started_at=self._utcnow(),
            status="running",
            inputs_used=list(inputs_used or []),
        )
        self._stages.append(stage)
        return stage

    def _complete_stage(
        self,
        stage: AreaStageRecord,
        outputs_created: Optional[Iterable[str]] = None,
        notes: Optional[Iterable[str]] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        stage.outputs_created.extend(list(outputs_created or []))
        stage.notes.extend(list(notes or []))
        if payload:
            stage.payload.update(payload)
        stage.status = "completed"
        stage.completed_at = self._utcnow()

    def _mark_optional_input(self, name: str, value: Any) -> None:
        if value is not None and name not in self._optional_inputs_used:
            self._optional_inputs_used.append(name)

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
