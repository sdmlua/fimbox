"""
Author: Supath Dhital
Date Updated: May 2026

Multi-branch processing.

Pipeline
--------
1. Read the AOI's branch list (output of BranchDerivation, at
   ``<aoi_dir>/branch_list.csv``).
2. AOI-level USGS gage assignment (when ``usgs_gages_gpkg`` is provided).
3. Branch-zero post-CreateHAND tasks: USGS crosswalk and deny-list cleanup.
4. For every non-zero branch in parallel (ProcessPoolExecutor):
       a. BranchZero raster prep for the branch (DEM clip + AGREE)
       b. adjust_floodplains (optional, when FEMA NFHL gpkg is provided)
       c. CreateHAND (22-step HAND pipeline)
       d. run_branch_crosswalk (USGS gage --> HydroID --> DEM elevations)
       e. per-branch deny-list cleanup (default-on; uses
          ``fimbox/config/deny_branches.lst``)
       f. per-branch log file: ``<aoi_dir>/logs/branch/<aoi_id>_branch_<bid>.log``
5. Per-branch error handling matching process_branch.sh:
       - exit code 61 (no valid flowlines) --> branch dir wiped, run continues
       - exit code 64 (no crosswalks)      --> branch dir wiped, run continues
       - other unhandled exception          --> branch logged as failed,
                                               run continues

Logging
-------
Each worker process re-attaches the case logger so its records land in
``<aoi_dir>/preprocess.log`` AND in a per-branch
``<aoi_dir>/logs/branch/<aoi_id>_branch_<bid>.log`` (attached for the
duration of that worker only). The shared format is the standard fimbox
``HH:MM:SS [LEVEL] message``.

Inputs
------
aoi_dir              <aoi_dir>/                 (output of getAllInputData + BranchDerivation)
branch_list          <aoi_dir>/branch_list.csv  (default location, override via param)
fema_nfhl_gpkg       optional, for adjust_floodplains
usgs_gages_gpkg      optional, for gage crosswalk
delete_deny_list     True (default) deletes branch-zero, per-branch, and AOI
                     intermediates from fimbox/config/deny_*.lst; False keeps everything.

Outputs
-------
For every successful branch B:
    <aoi_dir>/branches/<B>/  ... full BranchZero + HAND outputs (most
                                 intermediates removed when delete_deny_list=True)
    <aoi_dir>/branches/<B>/usgs_elev_table.csv  (when gages were provided)
    <aoi_dir>/logs/branch/<aoi_id>_branch_<B>.log
"""

from __future__ import annotations

import csv
import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

from ...logging_utils import attach_case_log
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
    status: str  # "ok" | "no_flowlines" | "no_crosswalk" | "too_many_hydroids" | "failed" | "killed"
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

        # AGREE and floodplain adjustment defaults match inundation-mapping
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
        evaluate_crosswalk: bool = False,
        convert_to_int16: bool = False,
        # gage crosswalk schema 
        gage_aoi_filter_column: str = "HUC8",

        # branch-zero post-CreateHAND steps 
        run_branch_zero_usgs_crosswalk: bool = False,
        delete_deny_list: bool = True,
        deny_branch_zero_list: Optional[Path] = None,
        deny_branches_list: Optional[Path] = None,

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
        self.evaluate_crosswalk = evaluate_crosswalk
        self.convert_to_int16 = convert_to_int16
        self.gage_aoi_filter_column = gage_aoi_filter_column
        self.run_branch_zero_usgs_crosswalk = run_branch_zero_usgs_crosswalk
        self.delete_deny_list = bool(delete_deny_list)
        self.deny_branch_zero_list = (
            Path(deny_branch_zero_list) if deny_branch_zero_list else None
        )
        self.deny_branches_list = (
            Path(deny_branches_list) if deny_branches_list else None
        )
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


def process_branches(cfg: AOIProcessingConfig) -> list[BranchResult]:
    """Run every non-zero branch in parallel.

    Pure branch calculation: BranchZero + adjust_floodplains + CreateHAND +
    USGS crosswalk + per-branch + branch-zero cleanup. **Calibration is NOT
    invoked here** — run it explicitly via ``fimbox.run_calibration`` once
    the branch loop and any deny-list cleanups are complete.

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
                aoi_filter_column=cfg.gage_aoi_filter_column,
            )
        except Exception as exc:
            log.error(f"USGS gage assignment failed: {exc}", exc_info=True)

    # If outputs already exist, we run the branch-zero post-steps here.
    _run_branch_zero_post_steps(cfg)

    branch_ids = _read_branch_list(cfg.branch_list_path, cfg.branch_zero_id)
    log.info(f"Branches to process (excluding branch zero): {len(branch_ids)}")
    if not branch_ids:
        log.warning("No non-zero branches found — only branch zero will exist.")

    results: list[BranchResult] = []
    if branch_ids:
        # Dispatch every non-zero branch to the shared Dask LocalCluster.
        # Worker count comes from FIMBOX_DASK_WORKERS or os.cpu_count();
        # cfg.n_workers > 1 still acts as an explicit override so old
        # callers stay reproducible. Each branch runs in its own worker
        # process (threads_per_worker=1) to keep WBT/TauDEM subprocess
        # handles and per-branch log files isolated.
        from ..._dask import get_client
        from distributed import as_completed as dask_as_completed

        client = get_client(n_workers=cfg.n_workers if cfg.n_workers > 1 else None)
        log.info(
            "Dispatching %d branches to Dask (%d workers, dashboard %s)",
            len(branch_ids),
            len(client.scheduler_info()["workers"]),
            client.dashboard_link,
        )
        # retries=0 is critical: when a branch OOMs and Dask kills the worker,
        # the default behaviour retries on 3 more workers and OOMs each in
        # turn (the KilledWorker storm seen in real-world runs). Setting it
        # to 0 makes the failure surface immediately so we record one
        # 'failed' result and move on to siblings.
        futures = client.map(
            _process_single_branch,
            [cfg] * len(branch_ids),
            branch_ids,
            pure=False,
            retries=0,
        )
        future_to_bid = {fut.key: bid for fut, bid in zip(futures, branch_ids)}
        for fut in dask_as_completed(futures):
            bid = future_to_bid.get(fut.key, "?")
            try:
                results.append(fut.result(timeout=cfg.timeout_seconds))
            except Exception as exc:
                # KilledWorker / OOM / unhandled exception — record as failed
                # so the summary reflects it, but never let one branch's
                # crash abort the whole AOI.
                exc_text = str(exc)
                status = (
                    "killed"
                    if "KilledWorker" in type(exc).__name__
                    or "killed" in exc_text.lower()
                    else "failed"
                )
                log.error(
                    f"Branch {bid} crashed in worker (status={status}): {exc}",
                    exc_info=True,
                )
                results.append(
                    BranchResult(branch_id=bid, status=status, elapsed_s=0.0, error=exc_text)
                )

    branches_elapsed = time.time() - start
    _log_branch_summary(results, branches_elapsed)

    total_elapsed = time.time() - start
    log.info(f"=== AOI processing complete: {cfg.aoi_id} ({total_elapsed:.1f}s) ===")
    return results


def _resolve_paths(cfg: AOIProcessingConfig) -> AOIProcessingConfig:
    """Fill in missing default paths from the case directory."""
    d = cfg.aoi_dir
    if cfg.branch_list_path is None:
        # BranchDerivation writes branch_ids.lst (one branch id per line).
        # Inundation-mapping's branch_ids.csv is only consumed by its
        # cross-AOI post-processing aggregator (fim_post_processing.sh),
        # not by the branch loop itself, so fimbox doesn't emit it.
        # branch_list.csv is kept as a legacy fallback for older AOIs.
        for candidate in ("branch_ids.lst", "branch_list.csv"):
            p = d / candidate
            if p.is_file():
                cfg.branch_list_path = p
                break
        else:
            cfg.branch_list_path = d / "branch_ids.lst"
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


def _run_branch_zero_post_steps(cfg: AOIProcessingConfig) -> None:
    """Run the branch-zero USGS crosswalk and deny-list cleanup.

    USGS crosswalk is opt-in (``cfg.run_branch_zero_usgs_crosswalk``).
    Deny-list cleanup runs by default (``cfg.delete_deny_list=True``) and uses
    ``cfg.deny_branch_zero_list`` when set, otherwise falls back to
    ``fimbox/config/deny_branch_zero.lst``. Pass ``delete_deny_list=False`` on
    the config to keep every intermediate.

    Every step is a no-op when its required inputs are missing or when branch
    zero's directory hasn't been populated yet (which is fine — call this
    after running branch-zero CreateHAND).
    """
    branch_dir = cfg.aoi_dir / "branches" / cfg.branch_zero_id
    if not branch_dir.is_dir():
        return

    # USGS crosswalk for branch zero. The branch-zero gpkg is the assignment
    # output with `levpa_id` overwritten to "0", so every gage in the AOI is
    # eligible for branch-zero rating-curve calibration.
    if cfg.run_branch_zero_usgs_crosswalk:
        bzero_gages = cfg.aoi_dir / f"usgs_subset_gages_{cfg.branch_zero_id}.gpkg"
        catchments_xwalk = (
            branch_dir
            / f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_"
            f"{cfg.branch_zero_id}.gpkg"
        )
        flows = (
            branch_dir / f"demDerived_reaches_split_filtered_{cfg.branch_zero_id}.gpkg"
        )
        dem_b = branch_dir / f"dem_meters_{cfg.branch_zero_id}.tif"
        dem_adj = branch_dir / f"dem_thalwegCond_{cfg.branch_zero_id}.tif"
        if not dem_b.exists():
            dem_b = branch_dir / f"dem_{cfg.branch_zero_id}.tif"

        if all(p.exists() for p in (bzero_gages, catchments_xwalk, flows, dem_b, dem_adj)):
            log.info("--- USGS crosswalk (branch zero) ---")
            try:
                run_branch_crosswalk(
                    aoi_gages_gpkg=bzero_gages,
                    branch_catchments_gpkg=catchments_xwalk,
                    branch_flows_gpkg=flows,
                    dem_path=dem_b,
                    dem_thalweg_path=dem_adj,
                    branch_id=cfg.branch_zero_id,
                    out_dir=branch_dir,
                    target_crs=cfg.target_crs,
                )
            except Exception as exc:
                log.warning(f"Branch-zero USGS crosswalk skipped: {exc}")
        else:
            log.info(
                "Branch-zero USGS crosswalk: required inputs missing — skipping"
            )

    # Deny-list cleanup for the branch-zero directory.
    if not cfg.delete_deny_list:
        log.info("Branch-zero cleanup disabled (delete_deny_list=False)")
        return

    deny = cfg.deny_branch_zero_list
    if deny is None:
        from .calculate_allbranches import _bundled_deny_list

        deny = _bundled_deny_list("deny_branch_zero.lst")
        if deny is None:
            log.warning(
                "delete_deny_list=True but no deny_branch_zero_list provided "
                "and fimbox/config/deny_branch_zero.lst not found — "
                "skipping branch-zero cleanup."
            )
            return

    from .outputs_cleanup import remove_deny_list_files

    log.info(f"--- branch-zero outputs cleanup ({Path(deny).name}) ---")
    try:
        remove_deny_list_files(
            src_dir=branch_dir,
            deny_list=deny,
            branch_id=cfg.branch_zero_id,
            verbose=True,
        )
    except Exception as exc:
        log.warning(f"Branch-zero cleanup skipped: {exc}")


def _process_single_branch(cfg: AOIProcessingConfig, branch_id: str) -> BranchResult:
    """Per-branch worker. Runs in its own process; re-attaches the case log
    so its output reaches the shared preprocess.log AND a per-branch log file
    under ``<aoi_dir>/logs/branch/<aoi_id>_branch_<branch_id>.log``."""
    attach_case_log(cfg.aoi_dir)
    branch_log = logging.getLogger(__name__)

    # Per-branch log file (matches inundation-mapping's branchLogFileName).
    # Attached to the fimbox root logger so every module's records this worker
    # emits are captured. Detached again in the finally-block at the end.
    branch_log_path = (
        cfg.aoi_dir / "logs" / "branch" / f"{cfg.aoi_id}_branch_{branch_id}.log"
    )
    branch_log_path.parent.mkdir(parents=True, exist_ok=True)
    branch_log_handler = logging.FileHandler(branch_log_path, mode="a")
    branch_log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    logging.getLogger("fimbox").addHandler(branch_log_handler)

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

        # CreateHAND: All steps
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

        # Crosswalk accuracy diagnostic (branch-zero only, mirrors
        # inundation-mapping's `evaluateCrosswalk=1` flag scoped to bzero).
        if cfg.evaluate_crosswalk and branch_id == cfg.branch_zero_id:
            from .evaluate_crosswalk import evaluate_crosswalk as _eval_xw

            xw_flows = (
                branch_dir
                / f"demDerived_reaches_split_filtered_addedAttributes_crosswalked_{branch_id}.gpkg"
            )
            hw = cfg.headwaters_gpkg or (
                cfg.aoi_dir / "nwm_headwater_points_subset.gpkg"
            )
            if xw_flows.exists() and hw and Path(hw).exists():
                try:
                    _eval_xw(
                        flows_gpkg=xw_flows,
                        nwm_flows_gpkg=cfg.streams_gpkg,
                        nwm_headwaters_gpkg=hw,
                        out_csv=cfg.aoi_dir / f"crosswalk_evaluation_{branch_id}.csv",
                        aoi_id=cfg.aoi_id,
                        branch_id=branch_id,
                    )
                except Exception as exc:
                    branch_log.warning(
                        f"evaluate_crosswalk skipped for branch {branch_id}: {exc}"
                    )
            else:
                branch_log.info(
                    f"evaluate_crosswalk skipped for branch {branch_id}: "
                    f"required inputs missing"
                )

        # Int16 storage downcast. Skipped for Alaska (AOI id starting with
        # "19" — HUC2=19 HydroIDs exceed Int16 range).
        if cfg.convert_to_int16:
            if str(cfg.aoi_id).startswith("19"):
                branch_log.info(
                    f"convert_to_int16 skipped (Alaska AOI {cfg.aoi_id})"
                )
            else:
                from .convert_to_int16 import (
                    CannotConvertHydroIDsToInt16,
                    convert_branch_to_int16,
                )

                try:
                    convert_branch_to_int16(branch_dir)
                except CannotConvertHydroIDsToInt16 as exc:
                    branch_log.error(
                        f"convert_to_int16 cannot run for branch {branch_id}: {exc}"
                    )
                except Exception as exc:
                    branch_log.warning(
                        f"convert_to_int16 skipped for branch {branch_id}: {exc}"
                    )

        # Per-branch deny-list cleanup. Skipped for branch zero (its cleanup
        # is handled by _run_branch_zero_post_steps with deny_branch_zero.lst).
        # Default-on; opt out via cfg.delete_deny_list=False.
        if cfg.delete_deny_list and branch_id != cfg.branch_zero_id:
            deny = cfg.deny_branches_list
            if deny is None:
                from .calculate_allbranches import _bundled_deny_list

                deny = _bundled_deny_list("deny_branches.lst")
            if deny is not None and Path(deny).is_file():
                from .outputs_cleanup import remove_deny_list_files

                try:
                    remove_deny_list_files(
                        src_dir=branch_dir,
                        deny_list=deny,
                        branch_id=branch_id,
                        verbose=True,
                    )
                except Exception as exc:
                    branch_log.warning(
                        f"Branch {branch_id} cleanup skipped: {exc}"
                    )
            else:
                branch_log.info(
                    f"Branch {branch_id}: no deny_branches.lst available, "
                    "skipping per-branch cleanup"
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

    finally:
        # Always detach + close the per-branch file handler so the workers
        # don't leak file descriptors across the ProcessPool.
        try:
            logging.getLogger("fimbox").removeHandler(branch_log_handler)
            branch_log_handler.close()
        except Exception:
            pass


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
    n_killed = sum(1 for r in results if r.status == "killed")
    n_no_flow = sum(1 for r in results if r.status == "no_flowlines")
    n_no_xw = sum(1 for r in results if r.status == "no_crosswalk")
    log.info(
        f"--- Branch summary: {n_ok} ok | {n_no_flow} no-flowlines | "
        f"{n_no_xw} no-crosswalk | {n_fail} failed | {n_killed} killed (OOM) "
        f"| total {elapsed_s:.1f}s ---"
    )
    if n_killed:
        killed_ids = [r.branch_id for r in results if r.status == "killed"]
        log.warning(
            "Killed branches (rerun individually with FIMBOX_DASK_WORKERS=1 "
            "for max RAM per worker): %s",
            ", ".join(killed_ids),
        )


# CLI
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Run BranchZero + CreateHAND + USGS crosswalk for every branch "
            "in an AOI. Pure branch calculation — run "
            "``python -m fimbox.preprocessing.calibrate_ratingcurve.pipeline`` separately "
            "for calibration after this finishes."
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
    parser.add_argument(
        "--evaluate-crosswalk", action="store_true",
        help="Write crosswalk_evaluation_<branch>.csv for branch zero only.",
    )
    parser.add_argument(
        "--convert-to-int16", action="store_true",
        help="Downcast gw_catchments + REM rasters to Int16 (skipped for AOI id starting with '19').",
    )
    parser.add_argument(
        "--gage-aoi-filter-column", default="HUC8",
        help=(
            "Column in the USGS gages gpkg that identifies AOI membership "
            "(default HUC8; use 'aoi_id' when the gpkg came from DownloadUSGSGages)."
        ),
    )
    parser.add_argument(
        "--run-branch-zero-usgs-crosswalk", action="store_true",
        help="Run the USGS crosswalk for branch zero after its CreateHAND finishes.",
    )
    parser.add_argument(
        "--no-delete-deny-list", action="store_true",
        help=(
            "Skip branch-zero deny-list cleanup. Default behaviour deletes "
            "intermediates using fimbox/config/deny_branch_zero.lst (or the "
            "file specified by --deny-branch-zero-list)."
        ),
    )
    parser.add_argument(
        "--deny-branch-zero-list", default=None,
        help=(
            "Path to a branch-zero deny-list. When omitted and cleanup is on, "
            "fimbox/config/deny_branch_zero.lst is used. Lines starting with "
            "'#' are comments; '{}' is substituted with the branch id."
        ),
    )
    args = parser.parse_args()

    aoi_dir = Path(args.aoi_dir)
    cfg = AOIProcessingConfig(
        aoi_dir=aoi_dir,
        aoi_id=args.aoi_id,
        branch_list_path=Path(args.branch_list) if args.branch_list else (aoi_dir / "branch_ids.lst"),
        n_workers=args.workers,
        fema_nfhl_gpkg=Path(args.fema_nfhl) if args.fema_nfhl else None,
        usgs_gages_gpkg=Path(args.usgs_gages) if args.usgs_gages else None,
        ahps_gpkg=Path(args.ahps) if args.ahps else None,
        ras_locs_gpkg=Path(args.ras_locs) if args.ras_locs else None,
        target_crs=args.target_crs,
        evaluate_crosswalk=args.evaluate_crosswalk,
        convert_to_int16=args.convert_to_int16,
        gage_aoi_filter_column=args.gage_aoi_filter_column,
        run_branch_zero_usgs_crosswalk=args.run_branch_zero_usgs_crosswalk,
        delete_deny_list=not args.no_delete_deny_list,
        deny_branch_zero_list=(
            Path(args.deny_branch_zero_list) if args.deny_branch_zero_list else None
        ),
    )
    process_branches(cfg)
