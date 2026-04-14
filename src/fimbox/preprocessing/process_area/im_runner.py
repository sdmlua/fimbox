"""Actual area execution wrapper around inundation-mapping HUC processing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess, run
from typing import Dict, List, Optional


@dataclass(slots=True)
class InundationMappingAreaRunConfig:
    """Runtime config for executing one staged area through inundation-mapping."""

    project_dir: str
    run_name: str
    area_id: str
    outputs_dir: str
    work_dir: str
    src_dir: Optional[str] = None
    tools_dir: Optional[str] = None
    extra_env: Dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class InundationMappingAreaRunResult:
    """Collected outputs from an executed area run."""

    command: List[str]
    exit_code: int
    output_area_dir: Path
    branch_dirs: List[Path]
    stdout: str
    stderr: str
    artifacts: Dict[str, Path]


class InundationMappingAreaRunner:
    """Run the original inundation-mapping HUC script as the area executor."""

    def __init__(self, config: InundationMappingAreaRunConfig) -> None:
        self.config = config
        self.project_dir = Path(config.project_dir).resolve()
        self.src_dir = Path(config.src_dir).resolve() if config.src_dir else self.project_dir / "src"
        self.tools_dir = Path(config.tools_dir).resolve() if config.tools_dir else self.project_dir / "tools"

    def run(self) -> InundationMappingAreaRunResult:
        script = self.project_dir / "fim_process_huc.sh"
        if not script.exists():
            raise FileNotFoundError(f"Could not find area script: {script}")

        env = dict(self.config.extra_env)
        env.update(
            {
                "projectDir": str(self.project_dir),
                "srcDir": str(self.src_dir),
                "toolsDir": str(self.tools_dir),
                "outputsDir": self.config.outputs_dir,
                "workDir": self.config.work_dir,
            }
        )
        command = [str(script), self.config.run_name, self.config.area_id]
        proc: CompletedProcess[str] = run(
            command,
            cwd=str(self.project_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        output_area_dir = Path(self.config.outputs_dir) / self.config.run_name / self.config.area_id
        branch_root = output_area_dir / "branches"
        branch_dirs = sorted([path for path in branch_root.iterdir() if path.is_dir()]) if branch_root.exists() else []
        artifacts = self._collect_area_artifacts(output_area_dir)
        return InundationMappingAreaRunResult(
            command=command,
            exit_code=proc.returncode,
            output_area_dir=output_area_dir,
            branch_dirs=branch_dirs,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
        )

    def _collect_area_artifacts(self, output_area_dir: Path) -> Dict[str, Path]:
        candidates = {
            "hydrotable": output_area_dir / "hydroTable.csv",
            "src_table": output_area_dir / "src_full_crosswalked.csv",
            "usgs_elev_table": output_area_dir / "usgs_elev_table.csv",
            "branch_csv": output_area_dir / "branch_ids.csv",
            "warnings_log": output_area_dir / "logs" / f"huc_{self.config.area_id}_warnings.log",
            "errors_log": output_area_dir / "logs" / f"huc_{self.config.area_id}_errors.log",
        }
        return {name: path for name, path in candidates.items() if path.exists()}
