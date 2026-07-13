"""
Author: Supath Dhital
Date Created: July 2026

Candidate-vs-benchmark FIM evaluation via the FIMeval framework.

Thin fimbox-flavoured wrapper around ``fimeval``
(https://github.com/sdmlua/fimeval), SDML's flood-map evaluation framework.
FIMeval builds contingency maps between a benchmark and one or more candidate
FIMs, masks permanent water bodies, and reports agreement metrics (CSI, POD,
FAR, F1, accuracy, ...), optionally down to the building level.

The wrapper handles the plumbing between the two tools:

* candidates default to the flood extents in ``<aoi>/fim-outputs/``
  (produced by :mod:`fimbox.fimgeneration`),
* the benchmark defaults to the newest raster in ``<aoi>/benchmark-data/``
  (downloaded by :func:`fimbox.queryBenchmarkFIM`),
* both are staged into a FIMeval-shaped case directory under
  ``<aoi>/fim-evaluation/<case>/`` (FIMeval requires the benchmark filename
  to contain the word ``benchmark`` — the staging step enforces that), and
* every metrics CSV FIMeval writes is collected into one tidy DataFrame.

Layout
------
::

    <AOI_root>/
      fim-outputs/*.tif                # candidate FIMs (read here)
      benchmark-data/**.tif            # benchmark FIMs from FIMbench (read here)
      fim-evaluation/
        <case>/                        # staged case: candidates + benchmark_*.tif
        outputs/<case>/                # FIMeval results (written here)
          ContingencyMaps/  EvaluationMetrics/  ...

Usage::

    import fimbox

    fimbox.queryBenchmarkFIM("out/my_basin", download=True)   # stage benchmark
    result = fimbox.evaluateFIM("out/my_basin")               # evaluate
    print(result.metrics)                                     # tidy DataFrame
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from .benchmark_query import BENCHMARK_DIR, FIM_OUTPUTS_DIR

PathLike = Union[str, Path]

log = logging.getLogger(__name__)

# fimbox AOI layout piece this module owns.
EVALUATION_DIR = "fim-evaluation"

# Boundary-extraction methods understood by fimeval.EvaluateFIM.
EVALUATION_METHODS = ("smallest_extent", "convex_hull", "AOI")


def _require_fimeval():
    try:
        import fimeval
    except ImportError as exc:  # pragma: no cover - broken environment
        raise ImportError(
            "fimeval (a fimbox dependency) is not importable. Repair the "
            "environment with:  pip install fimeval  (or reinstall fimbox)"
        ) from exc
    return fimeval


@dataclass
class EvaluationResult:
    """Where one evaluation ran and what it produced."""

    case_dir: Path  # staged candidates + benchmark
    output_dir: Path  # FIMeval outputs (maps, plots, CSVs)
    candidates: list[Path]  # staged candidate rasters
    benchmark: Path  # staged benchmark raster
    metrics_files: list[Path] = field(default_factory=list)

    @property
    def metrics(self):
        """All FIMeval metrics CSVs concatenated into one tidy DataFrame.

        Adds a ``source_file`` column naming the CSV each row came from.
        Returns ``None`` when no metrics file was produced.
        """
        import pandas as pd

        frames = []
        for f in self.metrics_files:
            try:
                df = pd.read_csv(f)
            except Exception as exc:  # unreadable/foreign CSV — skip, keep going
                log.warning(f"could not read metrics file {f}: {exc}")
                continue
            df["source_file"] = f.name
            frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else None


@dataclass
class FIMEvaluator:
    """Evaluate candidate FIM(s) against a benchmark FIM with FIMeval.

    One ``run()`` stages the case directory, runs ``fimeval.EvaluateFIM``,
    optionally prints contingency maps / metric plots / the building-footprint
    analysis, and collects every metrics CSV into the returned
    :class:`EvaluationResult`.
    """

    # fimbox AOI root (the folder holding fim-outputs/ and benchmark-data/).
    aoi_dir: PathLike

    # Candidate raster(s). Default: every extent .tif in <aoi>/fim-outputs/
    # (depth rasters are excluded).
    candidate: Optional[Union[PathLike, Sequence[PathLike]]] = None

    # Benchmark raster. Default: newest .tif under <aoi>/benchmark-data/
    # (as downloaded by queryBenchmarkFIM).
    benchmark: Optional[PathLike] = None

    # Case name -> <aoi>/fim-evaluation/<case_name>/. Default: first
    # candidate's stem.
    case_name: Optional[str] = None

    # Boundary-extraction method: "smallest_extent" | "convex_hull" | "AOI".
    method_name: str = "smallest_extent"

    # Results directory. Default: <aoi>/fim-evaluation/outputs/<case_name>/.
    output_dir: Optional[PathLike] = None

    # Optional FIMeval inputs, forwarded only when set.
    pwb_dir: Optional[PathLike] = None  # own permanent-water-bodies vector
    aoi_boundary: Optional[PathLike] = None  # AOI vector (method_name="AOI")
    target_crs: Optional[str] = None  # e.g. "EPSG:5070" (CONUS default)
    target_resolution: Optional[float] = None  # m, when resolutions differ

    # Extra FIMeval stages.
    contingency_map: bool = True  # save contingency map figures
    plot_metrics: bool = True  # save metric bar charts
    building_footprint: bool = False  # building-level agreement analysis
    building_footprint_file: Optional[PathLike] = None  # own footprint vector

    def _resolve_candidates(self, aoi: Path) -> list[Path]:
        if self.candidate is not None:
            paths = (
                [Path(self.candidate)]
                if isinstance(self.candidate, (str, Path))
                else [Path(p) for p in self.candidate]
            )
        else:
            fim_dir = aoi / FIM_OUTPUTS_DIR
            paths = sorted(
                p for p in fim_dir.glob("*.tif") if "depth" not in p.stem.lower()
            )
        if not paths:
            raise FileNotFoundError(
                f"no candidate FIM found under {aoi / FIM_OUTPUTS_DIR} — generate "
                "a FIM first (fimbox.generateFIM) or pass candidate=... explicitly."
            )
        missing = [p for p in paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"candidate raster(s) not found: {missing}")
        return paths

    def _resolve_benchmark(self, aoi: Path) -> Path:
        if self.benchmark is not None:
            bench = Path(self.benchmark)
            if not bench.is_file():
                raise FileNotFoundError(f"benchmark raster not found: {bench}")
            return bench
        bench_dir = aoi / BENCHMARK_DIR
        tifs = sorted(bench_dir.rglob("*.tif")) if bench_dir.is_dir() else []
        if not tifs:
            raise FileNotFoundError(
                f"no benchmark raster found under {bench_dir} — download one "
                "first (fimbox.queryBenchmarkFIM(..., download=True)) or pass "
                "benchmark=... explicitly."
            )
        return max(tifs, key=lambda p: p.stat().st_mtime)

    def _stage_case(
        self, aoi: Path, candidates: list[Path], benchmark: Path
    ) -> tuple[Path, list[Path], Path]:
        """Copy candidates + benchmark into the FIMeval case directory.

        FIMeval discovers the benchmark by the word ``benchmark`` in its
        filename, so the copy is renamed when needed.
        """
        case = self.case_name or candidates[0].stem
        case_dir = aoi / EVALUATION_DIR / case
        case_dir.mkdir(parents=True, exist_ok=True)

        staged_candidates = []
        for src in candidates:
            dst = case_dir / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
            staged_candidates.append(dst)

        bench_name = benchmark.name
        if "benchmark" not in bench_name.lower():
            bench_name = f"benchmark_{bench_name}"
        staged_benchmark = case_dir / bench_name
        if not staged_benchmark.exists():
            shutil.copy2(benchmark, staged_benchmark)

        return case_dir, staged_candidates, staged_benchmark

    def run(self) -> EvaluationResult:
        fimeval = _require_fimeval()

        if self.method_name not in EVALUATION_METHODS:
            raise ValueError(
                f"method_name {self.method_name!r} not in {EVALUATION_METHODS}"
            )
        if self.method_name == "AOI" and self.aoi_boundary is None:
            raise ValueError('method_name="AOI" requires aoi_boundary=...')

        aoi = Path(self.aoi_dir)
        candidates = self._resolve_candidates(aoi)
        benchmark = self._resolve_benchmark(aoi)
        case_dir, staged_candidates, staged_benchmark = self._stage_case(
            aoi, candidates, benchmark
        )

        output_dir = (
            Path(self.output_dir)
            if self.output_dir
            else aoi / EVALUATION_DIR / "outputs" / case_dir.name
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        # Optional args are forwarded only when set, so fimeval's own defaults
        # (CONUS PWB, EPSG:5070, native resolution) stay in charge.
        kwargs: dict[str, Any] = {}
        if self.pwb_dir is not None:
            kwargs["PWB_dir"] = Path(self.pwb_dir)
        if self.aoi_boundary is not None:
            kwargs["shapefile_dir"] = Path(self.aoi_boundary)
        if self.target_crs is not None:
            kwargs["target_crs"] = self.target_crs
        if self.target_resolution is not None:
            kwargs["target_resolution"] = self.target_resolution

        log.info(
            f"EvaluateFIM: {len(staged_candidates)} candidate(s) vs "
            f"{staged_benchmark.name} [{self.method_name}]"
        )
        fimeval.EvaluateFIM(case_dir, self.method_name, output_dir, **kwargs)

        if self.contingency_map:
            fimeval.PrintContingencyMap(case_dir, self.method_name, output_dir)
        if self.plot_metrics:
            fimeval.PlotEvaluationMetrics(case_dir, self.method_name, output_dir)
        if self.building_footprint:
            bf_kwargs: dict[str, Any] = {}
            if self.building_footprint_file is not None:
                bf_kwargs["building_footprint"] = Path(self.building_footprint_file)
            if self.aoi_boundary is not None:
                bf_kwargs["shapefile_dir"] = Path(self.aoi_boundary)
            fimeval.EvaluationWithBuildingFootprint(
                case_dir, self.method_name, output_dir, **bf_kwargs
            )

        metrics_files = sorted(output_dir.rglob("*.csv"))
        log.info(
            f"evaluation done: {len(metrics_files)} metrics file(s) -> {output_dir}"
        )
        return EvaluationResult(
            case_dir=case_dir,
            output_dir=output_dir,
            candidates=staged_candidates,
            benchmark=staged_benchmark,
            metrics_files=metrics_files,
        )


def evaluateFIM(
    aoi_dir: PathLike,
    *,
    candidate: Optional[Union[PathLike, Sequence[PathLike]]] = None,
    benchmark: Optional[PathLike] = None,
    case_name: Optional[str] = None,
    method_name: str = "smallest_extent",
    output_dir: Optional[PathLike] = None,
    pwb_dir: Optional[PathLike] = None,
    aoi_boundary: Optional[PathLike] = None,
    target_crs: Optional[str] = None,
    target_resolution: Optional[float] = None,
    contingency_map: bool = True,
    plot_metrics: bool = True,
    building_footprint: bool = False,
    building_footprint_file: Optional[PathLike] = None,
) -> EvaluationResult:
    """Evaluate candidate FIM(s) against a benchmark FIM with FIMeval.

    Functional wrapper around :class:`FIMEvaluator` — see the class for the
    parameter reference. Typical call, after ``queryBenchmarkFIM(...,
    download=True)`` has staged a benchmark::

        result = evaluateFIM("out/my_basin", method_name="smallest_extent")
        print(result.metrics)          # CSI / POD / FAR / F1 / accuracy ...
    """
    return FIMEvaluator(
        aoi_dir=aoi_dir,
        candidate=candidate,
        benchmark=benchmark,
        case_name=case_name,
        method_name=method_name,
        output_dir=output_dir,
        pwb_dir=pwb_dir,
        aoi_boundary=aoi_boundary,
        target_crs=target_crs,
        target_resolution=target_resolution,
        contingency_map=contingency_map,
        plot_metrics=plot_metrics,
        building_footprint=building_footprint,
        building_footprint_file=building_footprint_file,
    ).run()
