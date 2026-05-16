"""
Author: Supath Dhital
Date Updated: May 2026

Multi-branch AOI orchestrator — fimbox port of the tail of
``inundation-mapping/src/run_huc.sh`` (the parallel branch loop plus
``calibrate_rating_curves.sh``).

Pipeline
--------
1. Read the AOI's branch list (output of BranchDerivation, expected at
   ``<aoi_dir>/branch_list.csv`` with rows ``huc,levpa_id``).
2. For every non-zero branch in parallel (ProcessPoolExecutor):
       a. BranchZero raster prep for the branch (DEM clip + AGREE)
       b. adjust_floodplains (optional, when FEMA NFHL gpkg is provided)
       c. CreateHAND (22-step HAND pipeline)
       d. run_branch_crosswalk (USGS gage --> HydroID --> DEM elevations)
3. Per-branch error handling matching process_branch.sh:
       - exit code 61 (no valid flowlines) --> branch dir wiped, run continues
       - exit code 64 (no crosswalks)      --> branch dir wiped, run continues
       - other unhandled exception          --> branch logged as failed,
                                               run continues
4. After every branch finishes, call ``calibrate.run_calibration`` on the
   AOI with the user-provided ``CalibrationConfig`` (defaults skip the
   not-yet-ported routines).

Logging
-------
Each worker process re-attaches the same case logger as the parent (so all
log lines land in ``<aoi_dir>/preprocess.log``). Parent and worker share
the same ``HH:MM:SS [LEVEL] message`` format used everywhere else in fimbox.

Inputs
------
aoi_dir              <aoi_dir>/                 (output of getAllInputData + BranchDerivation)
branch_list          <aoi_dir>/branch_list.csv  (default location, override via param)
fema_nfhl_gpkg       optional, for adjust_floodplains
usgs_gages_gpkg      optional, for gage crosswalk
calibration_config   CalibrationConfig          (defaults to all toggles OFF)

Outputs
-------
For every successful branch B:
    <aoi_dir>/branches/<B>/  ... full BranchZero + HAND outputs
    <aoi_dir>/branches/<B>/usgs_elev_table.csv
    <aoi_dir>/branches/<B>/ras_elev_table.csv (if RAS2FIM inputs given)

After calibration:
    <aoi_dir>/hydrotable.csv / .feather / .parquet
    <aoi_dir>/usgs_elev_table.csv
    <aoi_dir>/ras_elev_table.csv
    <aoi_dir>/osm_bridge_centroids.gpkg
    <aoi_dir>/osm_roads_fimpact.csv

A summary line is appended to ``<aoi_dir>/processing_time_<aoi>.txt``.
"""

from __future__ import annotations

import csv
import logging
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

from ...logging_utils import attach_case_log
from ..calibrate import CalibrationConfig, run_calibration
from .adjust_floodplains import adjust_floodplains
from .calculate_branchzero import BranchZero
from .create_hand import CreateHAND
from .gage_crosswalk import assign_gages_to_branches, run_branch_crosswalk

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


# Exit-code conventions kept identical to inundation-mapping's process_branch.sh
EXIT_OK = 0
EXIT_NO_FLOWLINES = 61
EXIT_NO_CROSSWALK = 64
EXIT_TOO_MANY_HYDROIDS = 65


@dataclass
class BranchResult:
    branch_id: str
    status: str  # "ok" | "no_flowlines" | "no_crosswalk" | "too_many_hydroids" | "failed"
    elapsed_s: float
    error: Optional[str] = None


class AOIProcessingConfig:
    """Inputs the orchestrator needs to run every branch.

    The AOI identifier can be passed as either ``huc_id`` (when the AOI is a
    USGS HUC8) or ``aoi_id`` (for any other drainage area). Both are accepted
    and stored under ``self.aoi_id``; ``self.huc_id`` is exposed as an alias
    that returns the same value. This lets you write either::

        AOIProcessingConfig(aoi_dir=..., huc_id="08060202", ...)
        AOIProcessingConfig(aoi_dir=..., aoi_id="MyBasin", ...)

    Field aliases applied the same way:
        ``huc_dir`` is an alias for ``aoi_dir``
        ``huc_code`` is an alias for ``aoi_code``
    """

    def __init__(
        self,
        *,
        # ID can be huc_id OR aoi_id (positional aliases)
        aoi_dir: Optional[Path] = None,
        huc_dir: Optional[Path] = None,
        aoi_id: Optional[str] = None,
        huc_id: Optional[str] = None,
        branch_list_path: Optional[Path] = None,
        # required per-branch inputs
        dem_path: Optional[Path] = None,
        streams_gpkg: Optional[Path] = None,
        boundary_gpkg: Optional[Path] = None,
        catchments_gpkg: Optional[Path] = None,
        levelpaths_gpkg: Optional[Path] = None,
        # optional per-branch inputs
        bridge_elev_diff_path: Optional[Path] = None,
        levee_gpkg_path: Optional[Path] = None,
        levee_raster_path: Optional[Path] = None,
        headwaters_gpkg: Optional[Path] = None,
        levelpaths_extended_gpkg: Optional[Path] = None,
        fema_nfhl_gpkg: Optional[Path] = None,
        branch_polygons_gpkg: Optional[Path] = None,
        # USGS crosswalk inputs
        usgs_gages_gpkg: Optional[Path] = None,
        ahps_gpkg: Optional[Path] = None,
        ras_locs_gpkg: Optional[Path] = None,
        # CRS / numeric
        target_crs: Union[str, int] = 5070,
        branch_zero_id: str = "0",
        # AGREE + floodplain adjustment defaults match inundation-mapping
        agree_buffer_m: float = 15.0,
        agree_smooth_drop: float = 10.0,
        agree_sharp_drop: float = 1000.0,
        floodplain_distance_threshold: float = 7.0,
        floodplain_slope_exponent: float = 1.0,
        floodplain_z_factor: float = 0.5,
        fema_floodplain_layer: str = "combined",
        # AOI code used by CreateHAND for hydroTable annotation
        aoi_code: Optional[str] = None,
        huc_code: Optional[str] = None,
        # parallelism
        n_workers: int = 1,
        timeout_seconds: Optional[int] = None,
    ):
        self.aoi_dir = _pick_one("aoi_dir", aoi_dir, "huc_dir", huc_dir, required=True)
        self.aoi_id = _pick_one("aoi_id", aoi_id, "huc_id", huc_id, required=True)
        self.branch_list_path = branch_list_path
        self.dem_path = dem_path
        self.streams_gpkg = streams_gpkg
        self.boundary_gpkg = boundary_gpkg
        self.catchments_gpkg = catchments_gpkg
        self.levelpaths_gpkg = levelpaths_gpkg
        self.bridge_elev_diff_path = bridge_elev_diff_path
        self.levee_gpkg_path = levee_gpkg_path
        self.levee_raster_path = levee_raster_path
        self.headwaters_gpkg = headwaters_gpkg
        self.levelpaths_extended_gpkg = levelpaths_extended_gpkg
        self.fema_nfhl_gpkg = fema_nfhl_gpkg
        self.branch_polygons_gpkg = branch_polygons_gpkg
        self.usgs_gages_gpkg = usgs_gages_gpkg
        self.ahps_gpkg = ahps_gpkg
        self.ras_locs_gpkg = ras_locs_gpkg
        self.target_crs = target_crs
        self.branch_zero_id = branch_zero_id
        self.agree_buffer_m = agree_buffer_m
        self.agree_smooth_drop = agree_smooth_drop
        self.agree_sharp_drop = agree_sharp_drop
        self.floodplain_distance_threshold = floodplain_distance_threshold
        self.floodplain_slope_exponent = floodplain_slope_exponent
        self.floodplain_z_factor = floodplain_z_factor
        self.fema_floodplain_layer = fema_floodplain_layer
        # aoi_code defaults to the AOI id (CreateHAND stores it on hydroTable rows)
        self.aoi_code = _pick_one("aoi_code", aoi_code, "huc_code", huc_code, required=False) or ""
        self.n_workers = n_workers
        self.timeout_seconds = timeout_seconds

    # Read-only aliases so old callers using `cfg.huc_dir` / `cfg.huc_id` keep working
    @property
    def huc_dir(self) -> Path:
        return self.aoi_dir

    @property
    def huc_id(self) -> str:
        return self.aoi_id

    @property
    def huc_code(self) -> str:
        return self.aoi_code


def _pick_one(name_a: str, val_a, name_b: str, val_b, *, required: bool):
    """Accept either of two equivalent kwargs but not both at once."""
    if val_a is not None and val_b is not None and val_a != val_b:
        raise TypeError(
            f"Pass either {name_a}= or {name_b}=, not both with different values "
            f"({val_a!r} vs {val_b!r})."
        )
    chosen = val_a if val_a is not None else val_b
    if required and chosen is None:
        raise TypeError(f"Either {name_a}= or {name_b}= must be provided.")
    return chosen


# Backwards-compatible alias: HucProcessingConfig is the same class.
HucProcessingConfig = AOIProcessingConfig


def process_branches(
    cfg: AOIProcessingConfig,
    *,
    calibration: Optional[CalibrationConfig] = None,
) -> list[BranchResult]:
    """Run every non-zero branch in parallel, then run calibration.

    Returns the per-branch results list (caller can inspect statuses)."""
    cfg = _resolve_paths(cfg)
    attach_case_log(cfg.aoi_dir)

    start = time.time()
    log.info(f"=== process_branches: {cfg.aoi_id} ===")
    log.info(f"Branch list: {cfg.branch_list_path}")
    log.info(f"Workers: {cfg.n_workers}")

    # Stage 0: AOI-level USGS gage assignment (once for the entire AOI)
    if cfg.usgs_gages_gpkg and cfg.levelpaths_gpkg:
        log.info("--- USGS gage assignment (AOI level) ---")
        try:
            assign_gages_to_branches(
                usgs_gages_gpkg=cfg.usgs_gages_gpkg,
                nwm_streams_levelpaths_gpkg=cfg.levelpaths_gpkg,
                aoi_id=cfg.aoi_id,
                out_dir=cfg.aoi_dir,
                target_crs=cfg.target_crs,
                branch_zero_id=cfg.branch_zero_id,
                ras_locs_gpkg=cfg.ras_locs_gpkg,
                ahps_gpkg=cfg.ahps_gpkg,
            )
        except Exception as exc:
            log.error(f"USGS gage assignment failed: {exc}", exc_info=True)

    branch_ids = _read_branch_list(cfg.branch_list_path, cfg.branch_zero_id)
    log.info(f"Branches to process (excluding branch zero): {len(branch_ids)}")
    if not branch_ids:
        log.warning("No non-zero branches found — only branch zero will exist.")

    results: list[BranchResult] = []
    if branch_ids:
        with ProcessPoolExecutor(max_workers=cfg.n_workers) as pool:
            futures = {
                pool.submit(_process_single_branch, cfg, bid): bid
                for bid in branch_ids
            }
            for fut in as_completed(futures, timeout=cfg.timeout_seconds):
                bid = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    log.error(f"Branch {bid} crashed in worker: {exc}", exc_info=True)
                    results.append(
                        BranchResult(branch_id=bid, status="failed", elapsed_s=0.0, error=str(exc))
                    )

    branches_elapsed = time.time() - start
    _log_branch_summary(results, branches_elapsed)

    # Calibration
    calibration = calibration or CalibrationConfig(skip_unimplemented=True)
    try:
        run_calibration(cfg.aoi_dir, calibration)
    except Exception as exc:
        log.error(f"Calibration pipeline failed: {exc}", exc_info=True)

    total_elapsed = time.time() - start
    _write_processing_time_csv(cfg.aoi_dir, cfg.aoi_id, total_elapsed, branches_elapsed, len(results))
    log.info(f"=== AOI processing complete: {cfg.aoi_id} ({total_elapsed:.1f}s) ===")
    return results


def _resolve_paths(cfg: AOIProcessingConfig) -> AOIProcessingConfig:
    """Fill in missing default paths from the case directory."""
    d = cfg.aoi_dir
    cfg.dem_path = cfg.dem_path or d / "dem.tif"
    cfg.streams_gpkg = cfg.streams_gpkg or d / "nwm_subset_streams.gpkg"
    cfg.boundary_gpkg = cfg.boundary_gpkg or d / "wbd8_clp.gpkg"
    cfg.catchments_gpkg = cfg.catchments_gpkg or d / "nwm_catchments_proj_subset.gpkg"
    cfg.levelpaths_gpkg = cfg.levelpaths_gpkg or d / "nwm_subset_streams_levelPaths.gpkg"
    cfg.branch_polygons_gpkg = cfg.branch_polygons_gpkg or d / "branch_polygons.gpkg"
    if cfg.headwaters_gpkg is None:
        hw = d / "nwm_headwater_points_subset.gpkg"
        cfg.headwaters_gpkg = hw if hw.exists() else None
    if cfg.levee_gpkg_path is None:
        lg = d / "3d_nld_subset_levees_burned.gpkg"
        cfg.levee_gpkg_path = lg if lg.exists() else None
    return cfg


def _read_branch_list(path: Path, branch_zero_id: str) -> list[str]:
    """Read branch_list.csv (rows: ``aoi_id,levpa_id``) excluding branch zero."""
    if not path.is_file():
        return []
    branches: list[str] = []
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            # accept either 1-col (just branch id) or 2-col (aoi_id,branch_id)
            bid = row[-1].strip()
            if bid and bid != branch_zero_id:
                branches.append(bid)
    return branches


def _process_single_branch(cfg: AOIProcessingConfig, branch_id: str) -> BranchResult:
    """Per-branch worker. Runs in its own process; re-attaches the case log
    so its output reaches the shared preprocess.log."""
    attach_case_log(cfg.aoi_dir)
    branch_log = logging.getLogger(__name__)

    started = time.time()
    branch_log.info(f"--- Branch {branch_id} start ---")
    branch_dir = cfg.aoi_dir / "branches" / branch_id
    branch_dir.mkdir(parents=True, exist_ok=True)

    try:
        # BranchZero raster prep — equivalent of run_by_branch.sh's clip+AGREE+filldem
        BranchZero(
            dem_path=cfg.dem_path,
            streams_gpkg=cfg.streams_gpkg,
            boundary_gpkg=cfg.boundary_gpkg,
            out_dir=cfg.aoi_dir,
            bridge_elev_diff_path=cfg.bridge_elev_diff_path,
            levee_gpkg_path=cfg.levee_gpkg_path,
            levee_raster_path=cfg.levee_raster_path,
            headwaters_gpkg=cfg.headwaters_gpkg,
            levelpaths_extended_gpkg=cfg.levelpaths_extended_gpkg,
            target_crs=(
                f"EPSG:{cfg.target_crs}" if str(cfg.target_crs).isdigit() else cfg.target_crs
            ),
            agree_buffer_m=cfg.agree_buffer_m,
            agree_smooth_drop=cfg.agree_smooth_drop,
            agree_sharp_drop=cfg.agree_sharp_drop,
            branch_zero_id=branch_id,
        ).run()

        # adjust_floodplains is optional (requires NFHL gpkg + branch polygons)
        if cfg.fema_nfhl_gpkg and cfg.branch_polygons_gpkg.exists():
            dem_burned = branch_dir / f"dem_burned_{branch_id}.tif"
            flows_bool = branch_dir / f"flows_grid_boolean_{branch_id}.tif"
            if dem_burned.exists() and flows_bool.exists():
                try:
                    adjust_floodplains(
                        input_file=flows_bool,
                        dem_file=dem_burned,
                        nwm_catchments=cfg.catchments_gpkg,
                        nwm_streams=cfg.streams_gpkg,
                        nwm_levelpaths=cfg.levelpaths_gpkg,
                        distance_file=branch_dir / f"flows_grid_boolean_euclidean_distance_{branch_id}.tif",
                        output_file=branch_dir / f"dem_burned_adjusted_{branch_id}.tif",
                        distance_threshold=cfg.floodplain_distance_threshold,
                        slope_exponent=cfg.floodplain_slope_exponent,
                        z_factor=cfg.floodplain_z_factor,
                        branch_polygons=cfg.branch_polygons_gpkg,
                        branch_id=branch_id,
                        fema_flood_zones_file=cfg.fema_nfhl_gpkg,
                        fema_flood_zones_layer=cfg.fema_floodplain_layer,
                    )
                except Exception as exc:
                    branch_log.warning(
                        f"adjust_floodplains skipped for branch {branch_id}: {exc}"
                    )

        # CreateHAND: 22-step HAND pipeline
        CreateHAND(
            aoi_dir=cfg.aoi_dir,
            branch_dir=branch_dir,
            branch_id=branch_id,
            aoi_code=cfg.aoi_code or cfg.aoi_id,
            levee_protected_areas_gpkg=None,  # set by user if needed
            levee_levelpaths_csv=None,
            lakes_gpkg=cfg.aoi_dir / "nwm_lakes_proj_subset.gpkg" if (
                cfg.aoi_dir / "nwm_lakes_proj_subset.gpkg"
            ).exists() else None,
            boundary_gpkg=cfg.boundary_gpkg,
            dem_path=cfg.dem_path,
        ).run()

        # USGS gage crosswalk (optional)
        aoi_gages = cfg.aoi_dir / "usgs_subset_gages.gpkg"
        if aoi_gages.is_file():
            catchments_xwalk = (
                branch_dir
                / f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{branch_id}.gpkg"
            )
            flows = branch_dir / f"demDerived_reaches_split_filtered_{branch_id}.gpkg"
            dem_b = branch_dir / f"dem_meters_{branch_id}.tif"
            dem_adj = branch_dir / f"dem_thalwegCond_{branch_id}.tif"
            if all(p.exists() for p in (catchments_xwalk, flows, dem_b, dem_adj)):
                try:
                    run_branch_crosswalk(
                        aoi_gages_gpkg=aoi_gages,
                        branch_catchments_gpkg=catchments_xwalk,
                        branch_flows_gpkg=flows,
                        dem_path=dem_b,
                        dem_thalweg_path=dem_adj,
                        branch_id=branch_id,
                        out_dir=branch_dir,
                        target_crs=cfg.target_crs,
                    )
                except Exception as exc:
                    branch_log.warning(
                        f"USGS crosswalk skipped for branch {branch_id}: {exc}"
                    )

        elapsed = time.time() - started
        branch_log.info(f"--- Branch {branch_id} ok ({elapsed:.1f}s) ---")
        return BranchResult(branch_id=branch_id, status="ok", elapsed_s=elapsed)

    except Exception as exc:
        elapsed = time.time() - started
        msg = f"{exc}\n{traceback.format_exc()}"
        # Classify known fatal error markers (match process_branch.sh codes)
        status = _classify_branch_error(msg)
        branch_log.error(f"Branch {branch_id} {status} ({elapsed:.1f}s): {exc}")
        if status in ("no_flowlines", "no_crosswalk", "too_many_hydroids"):
            _wipe_branch_dir(branch_dir)
        return BranchResult(
            branch_id=branch_id, status=status, elapsed_s=elapsed, error=msg
        )


def _classify_branch_error(msg: str) -> str:
    """Translate fimbox exception text into the inundation-mapping exit codes."""
    text = msg.lower()
    if "noflowlines" in text or "no valid flowlines" in text or "no flowlines" in text:
        return "no_flowlines"
    if "nocrosswalk" in text or "no crosswalks" in text:
        return "no_crosswalk"
    if "too many hydroids" in text or "hydroid with more than 8 digits" in text:
        return "too_many_hydroids"
    return "failed"


def _wipe_branch_dir(branch_dir: Path) -> None:
    """Match process_branch.sh's behaviour: branches that hit 61/64/65 get
    their output directory removed so they don't pollute later aggregation."""
    if branch_dir.is_dir():
        import shutil

        shutil.rmtree(branch_dir, ignore_errors=True)


def _log_branch_summary(results: Sequence[BranchResult], elapsed_s: float) -> None:
    n_ok = sum(1 for r in results if r.status == "ok")
    n_fail = sum(1 for r in results if r.status == "failed")
    n_no_flow = sum(1 for r in results if r.status == "no_flowlines")
    n_no_xw = sum(1 for r in results if r.status == "no_crosswalk")
    log.info(
        f"--- Branch summary: {n_ok} ok | {n_no_flow} no-flowlines | "
        f"{n_no_xw} no-crosswalk | {n_fail} failed | total {elapsed_s:.1f}s ---"
    )


def _write_processing_time_csv(
    aoi_dir: Path, aoi_id: str, total_s: float, branches_s: float, n_branches: int
) -> None:
    out = aoi_dir / f"processing_time_{aoi_id}.txt"
    line = f"{aoi_id},{total_s:.1f},{branches_s:.1f},{n_branches}\n"
    with open(out, "a") as fh:
        fh.write(line)


# CLI
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Run BranchZero + CreateHAND + USGS crosswalk for every branch in a "
            "AOI, then run the calibration pipeline."
        )
    )
    parser.add_argument("--aoi-dir", required=True)
    parser.add_argument("--aoi-id", required=True)
    parser.add_argument("--branch-list", default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--fema-nfhl", default=None)
    parser.add_argument("--usgs-gages", default=None)
    parser.add_argument("--ahps", default=None)
    parser.add_argument("--ras-locs", default=None)
    parser.add_argument("--target-crs", default="5070")
    parser.add_argument("--manual-calb", default=None)
    parser.add_argument("--skip-unimplemented", action="store_true")
    args = parser.parse_args()

    aoi_dir = Path(args.aoi_dir)
    cfg = AOIProcessingConfig(
        aoi_dir=aoi_dir,
        aoi_id=args.aoi_id,
        branch_list_path=Path(args.branch_list) if args.branch_list else (aoi_dir / "branch_list.csv"),
        n_workers=args.workers,
        fema_nfhl_gpkg=Path(args.fema_nfhl) if args.fema_nfhl else None,
        usgs_gages_gpkg=Path(args.usgs_gages) if args.usgs_gages else None,
        ahps_gpkg=Path(args.ahps) if args.ahps else None,
        ras_locs_gpkg=Path(args.ras_locs) if args.ras_locs else None,
        target_crs=args.target_crs,
    )
    calib = CalibrationConfig(
        manual_calb_toggle=args.manual_calb is not None,
        man_calb_file=Path(args.manual_calb) if args.manual_calb else None,
        skip_unimplemented=args.skip_unimplemented,
        job_branch_limit=args.workers,
    )
    process_branches(cfg, calibration=calib)
