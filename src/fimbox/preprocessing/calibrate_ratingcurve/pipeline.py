"""
Author: Supath Dhital
Date Updated: June 2026

Synthetic rating curve (SRC) calibration pipeline.

The whole configuration lives in a single ``CalibrationConfig`` dataclass, and
every step is a socket with two independent halves:

  * a toggle, on by default — pass ``False`` to unplug that step;
  * an input path, ``None`` by default — meaning "fetch the fimbox dataset for
    this AOI". Pass your own file to substitute it.

So ``CalibrationConfig()`` runs the full pipeline on published data with nothing
to stage by hand, and each step is independently swappable or removable without
disturbing the rest.

Usage
-----
    from fimbox import CalibrationConfig, run_calibration

    run_calibration(aoi_dir)                       #everything, published data

    run_calibration(aoi_dir, CalibrationConfig(
        vmann_input_file="my_roughness.parquet",    #my table, this step only
        src_adjust_spatial=False,                   #unplug one step
    ))
    # job_branch_limit is left unset above: the branch-parallel steps size
    # themselves to the machine. Set it only to cap the pool (or 1 to go serial).

Each ``Calibrator`` step can also be run on its own (see the individual
classes in this subpackage), which is what the step-by-step tests exercise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..._aoi_lock import aoi_write_lock
from ...logging_utils import attach_case_log, log_errors
from ._common import CalibrationNotImplemented, PathLike, aoi_id_of, resolve_aoi_dir
from .aggregate import BranchAggregator
from .dem_adjust import (
    BathymetricAdjustment,
    LongitudinalFlowFilter,
    ThalwegNotchesAdjustment,
)
from .logscan import LogScanner
from .reset import HydroTableReset
from .src_adjust import SlopeAdjustment, SrcBankfull, SrcNonmonotonic, SrcSubdiv
from .src_calibrate import (
    ManualCalibrator,
    Ras2fimCalibrator,
    SpatialObsCalibrator,
    UsgsRatingCalibrator,
)

log = logging.getLogger(__name__)


@dataclass
class CalibrationConfig:
    """Every calibration knob in one dataclass.

    Toggles default on and input paths default to ``None``, which resolves to
    the published fimbox dataset for the AOI being calibrated. Set a toggle
    ``False`` to unplug a step; set its path to run the step on your own data.
    """

    # --- mode ----
    # Reruns reset hydroTables to the pre-calibration baseline before re-applying any adjustments.
    calibration_rerun: bool = False

    # --- step toggles ----
    # All on: this is the full pipeline. Pass False to unplug an individual step.
    slope_adjustment: bool = True
    thalweg_notches_adjustment: bool = True
    longitudinal_filter: bool = True
    bathymetry_adjust: bool = True
    src_bankfull_toggle: bool = True
    src_subdiv_toggle: bool = True
    nonmonotonic_src_adjustment: bool = True
    src_adjust_usgs: bool = True
    src_adjust_ras2fim: bool = True
    src_adjust_spatial: bool = True
    manual_calb_toggle: bool = True

    # --- input files: None -> fetch the published dataset, or pass your own ----
    # Slope replacing the DEM rise/run slope the crosswalk built each SRC on.
    # "table" (default) reads slope_table, which resolves to the published
    # reach-slope dataset; "hfab" reads the SLOPE_HFAB column already carried
    # through the SRC. Reaches the chosen source does not cover keep the DEM slope.
    slope_source: str = "table"
    slope_table: Optional[PathLike] = None
    bathy_file_ehydro: Optional[PathLike] = None
    bathy_file_aibased: Optional[PathLike] = None
    ai_toggle: int = 1  # 1 = also apply predicted (AI) channel geometry
    ai_strm_order: int = 4  # min stream order for AI-based bathymetry
    bankfull_flows_file: Optional[PathLike] = None
    vmann_input_file: Optional[PathLike] = None
    usgs_rating_curve_csv: Optional[PathLike] = None
    usgs_acceptable_gages: Optional[PathLike] = None
    nwm_recur_file: Optional[PathLike] = None
    ras_rating_curve_csv: Optional[PathLike] = None
    calib_points_file: Optional[PathLike] = (
        None  # flood-edge obs points for src_adjust_spatial
    )
    man_calb_file: Optional[PathLike] = None

    # Skip the S3 lookups entirely and run only on paths given explicitly. Steps
    # whose input is then missing are logged and skipped.
    offline: bool = False

    # --- tunables ------
    # Default Manning's n used by the subdivision step when a feature_id is absent from vmann_input_file.
    default_channel_n: float = 0.06
    default_overbank_n: float = 0.12
    nonmonotonic_stream_order_min: int = 4
    include_branch_zero: bool = True

    # --- aggregation control ----
    # The two always-on aggregations. Pre-calibration assembles the elev tables every adjustment consumes; post-calibration publishes the final
    # AOI hydroTable + bridge / road layers. Turn off only for debugging.
    aggregate_pre: bool = True
    aggregate_post: bool = True

    # --- log scan ---
    # After calibration, scan logs/ for error / warning lines into per-AOI summary files. Off by default (no-ops cleanly when there is no logs/).
    scan_logs: bool = False

    # --- execution ---
    # Worker count for the branch-parallel routines. Leave it alone (or pass
    # None / 0) to use everything the machine can feed; a number larger than
    # that is clamped rather than obeyed. Pass 1 for a serial, debuggable run.
    job_branch_limit: Optional[int] = None

    # When True, toggled-on routines that aren't ported yet warn and skip instead of raising CalibrationNotImplemented.
    skip_unimplemented: bool = False


@dataclass
class Calibrator:
    aoi_dir: PathLike
    cfg: CalibrationConfig

    def run(self) -> None:
        # One writer per AOI: every step below rewrites the branch tables in
        # place, so a second run over the same AOI would interleave with this
        # one and splice the CSVs.
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        attach_case_log(aoi_dir)
        with log_errors(f"Calibration {aoi_id_of(aoi_dir)}"):
            with aoi_write_lock(aoi_dir, "calibration"):
                self._run()

    def _run(self) -> None:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        cfg = self.cfg
        aoi_id = aoi_id_of(aoi_dir)
        # Route every calibration log line into the AOI's shared
        # processing.log (and stdout) in the standard format, same as the
        # preprocessing / branch / FIM stages do.
        attach_case_log(aoi_dir)
        verb = "Rerunning calibration" if cfg.calibration_rerun else "Calibration"
        log.info(f"=== {verb}: {aoi_id} ===")

        if cfg.calibration_rerun:
            log.info("--- reset hydroTable + src_full_crosswalked ---")
            HydroTableReset(aoi_dir=aoi_dir).run()

        if cfg.aggregate_pre:
            log.info("--- aggregate usgs + ras2fim elev tables ---")
            BranchAggregator(aoi_dir=aoi_dir, usgs_elev=True, ras_elev=True).run()

        if cfg.slope_adjustment:
            # "hfab" needs no external table — the column rides through the SRC.
            slope_table = (
                self._dataset("reach_slope", cfg.slope_table)
                if cfg.slope_source == "table"
                else None
            )
            if cfg.slope_source == "table" and slope_table is None:
                log.info("Skipping slope adjustment: no reach-slope table available")
            else:
                self._maybe(
                    True,
                    f"slope adjustment ({cfg.slope_source})",
                    lambda: SlopeAdjustment(
                        aoi_dir=aoi_dir,
                        slope_source=cfg.slope_source,
                        slope_table=slope_table,
                        n_workers=cfg.job_branch_limit,
                        include_branch_zero=cfg.include_branch_zero,
                    ).run(),
                )

        self._maybe(
            cfg.thalweg_notches_adjustment,
            "thalweg notches adjustment",
            lambda: ThalwegNotchesAdjustment(
                aoi_dir=aoi_dir, n_workers=cfg.job_branch_limit
            ).run(),
        )

        self._maybe(
            cfg.longitudinal_filter,
            "longitudinal discharge adjustment",
            lambda: LongitudinalFlowFilter(
                aoi_dir=aoi_dir, n_workers=cfg.job_branch_limit
            ).run(),
        )

        if cfg.bathymetry_adjust:
            # Surveyed channels are the base source; predicted geometry only when ai_toggle=1.
            ehydro = self._dataset("channel_bathymetry", cfg.bathy_file_ehydro)
            aibased = (
                self._dataset("channel_geometry_predicted", cfg.bathy_file_aibased)
                if cfg.ai_toggle
                else None
            )
            self._needs(
                "bathymetry adjustment",
                ehydro or aibased,
                lambda: BathymetricAdjustment(
                    aoi_dir=aoi_dir,
                    bathy_file_ehydro=ehydro,
                    bathy_file_aibased=aibased,
                    ai_toggle=cfg.ai_toggle if aibased else 0,
                    ai_strm_order=cfg.ai_strm_order,
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        bankfull_flows = None
        if cfg.src_bankfull_toggle:
            bankfull_flows = self._dataset("bankfull_flows", cfg.bankfull_flows_file)
            self._needs(
                "SRC bankfull identification",
                bankfull_flows,
                lambda: SrcBankfull(
                    aoi_dir=aoi_dir,
                    bankfull_flows_file=bankfull_flows,
                    n_workers=cfg.job_branch_limit,
                    include_branch_zero=cfg.include_branch_zero,
                ).run(),
            )

        # Subdivision consumes the Stage_bankfull column SrcBankfull adds, so it
        # needs both toggles. The observation-driven calibrators below gate on
        # this same flag rather than src_subdiv_toggle alone: they read the
        # per-stage channel_n / overbank_n only subdivision writes, so the
        # toggle being set is not enough — it has to have actually run.
        subdiv_ran = cfg.src_subdiv_toggle and bankfull_flows is not None
        if cfg.src_subdiv_toggle and not cfg.src_bankfull_toggle:
            log.warning(
                "src_subdiv_toggle is set but src_bankfull_toggle is not — "
                "subdivision needs Stage_bankfull, so it will be skipped along "
                "with the SRC adjustment routines that depend on it"
            )

        if subdiv_ran:
            vmann = self._dataset("channel_roughness", cfg.vmann_input_file)
            subdiv_ran = vmann is not None
            self._needs(
                "SRC channel/overbank subdivision",
                vmann,
                lambda: SrcSubdiv(
                    aoi_dir=aoi_dir,
                    vmann_table=vmann,
                    n_workers=cfg.job_branch_limit,
                    include_branch_zero=cfg.include_branch_zero,
                    default_channel_n=cfg.default_channel_n,
                    default_overbank_n=cfg.default_overbank_n,
                ).run(),
            )

        self._maybe(
            cfg.nonmonotonic_src_adjustment,
            "nonmonotonic SRC adjustment",
            lambda: SrcNonmonotonic(
                aoi_dir=aoi_dir,
                stream_order_min=cfg.nonmonotonic_stream_order_min,
                n_workers=cfg.job_branch_limit,
            ).run(),
        )

        # Recurrence flows are shared by the two rating-curve calibrators.
        recur = (
            self._dataset("recurrence_flows", cfg.nwm_recur_file)
            if subdiv_ran and (cfg.src_adjust_usgs or cfg.src_adjust_ras2fim)
            else None
        )

        if cfg.src_adjust_usgs and subdiv_ran:
            # Rating curves + recurrence flows required; the gage-quality filter is optional.
            rating = self._dataset("gage_rating_curves", cfg.usgs_rating_curve_csv)
            self._needs(
                "SRC adjust (gage rating curves)",
                rating and recur,
                lambda: UsgsRatingCalibrator(
                    aoi_dir=aoi_dir,
                    usgs_rating_curve_csv=rating,
                    nwm_recur_file=recur,
                    usgs_acceptable_gages=self._dataset(
                        "gage_quality_filter", cfg.usgs_acceptable_gages
                    ),
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        if cfg.src_adjust_ras2fim and subdiv_ran:
            xsec = self._dataset("xsec_rating_curves", cfg.ras_rating_curve_csv)
            self._needs(
                "SRC adjust (cross-section rating curves)",
                xsec and recur,
                lambda: Ras2fimCalibrator(
                    aoi_dir=aoi_dir,
                    ras_rating_curve_csv=xsec,
                    nwm_recur_file=recur,
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        if cfg.src_adjust_spatial and subdiv_ran:
            points = self._dataset("flood_edge_points", cfg.calib_points_file)
            self._needs(
                "SRC adjust (flood-edge observations)",
                points,
                lambda: SpatialObsCalibrator(
                    aoi_dir=aoi_dir,
                    calib_points_file=points,
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        if cfg.manual_calb_toggle:
            coefs = self._dataset("manual_coefficients", cfg.man_calb_file)
            self._needs(
                "manual calibration",
                coefs,
                lambda: ManualCalibrator(aoi_dir=aoi_dir, calibration_file=coefs).run(),
            )

        if cfg.aggregate_post:
            log.info("--- aggregate hydroTable + bridge + road outputs ---")
            BranchAggregator(aoi_dir=aoi_dir, htable=True, bridge=True, road=True).run()

        if cfg.scan_logs:
            log.info("--- scan logs for errors / warnings ---")
            LogScanner(aoi_dir=aoi_dir, calibration_rerun=cfg.calibration_rerun).run()

        log.info(f"=== {verb} complete: {aoi_id} ===")

    def _dataset(
        self, key: Optional[str], override: Optional[PathLike]
    ) -> Optional[Path]:
        """One step's input: the caller's file, else the published dataset.

        None means nothing resolved, which the caller turns into a skip — a
        missing dataset should cost that one step, not the run."""
        if override is not None:
            p = Path(override)
            if p.exists():
                return p
            log.warning(f"{key}: no such file {p}")
            if self.cfg.offline:
                return None
        if key is None or self.cfg.offline:
            return None
        from ...datasets import fetch

        return fetch(key)

    def _needs(self, name: str, dataset: Optional[object], fn) -> None:
        # Run the step only if its input resolved.
        if not dataset:
            log.info(f"Skipping {name}: no input dataset available")
            return
        self._maybe(True, name, fn)

    def _maybe(self, enabled: bool, name: str, fn) -> None:
        # A step that blows up is logged and skipped: one bad input or a quirk of
        # somebody's catchments shouldn't cost them the rest of the pipeline.
        if not enabled:
            return
        log.info(f"--- {name} ---")
        try:
            fn()
        except CalibrationNotImplemented as exc:
            if self.cfg.skip_unimplemented:
                log.warning(f"Skipping {name}: {exc}")
            else:
                raise
        except Exception:
            log.exception(f"{name} failed — skipping to the next step")


def run_calibration(
    aoi_dir: Optional[PathLike] = None,
    cfg: Optional[CalibrationConfig] = None,
    *,
    huc_dir: Optional[PathLike] = None,
) -> None:
    if cfg is None:
        cfg = CalibrationConfig()
    Calibrator(aoi_dir=resolve_aoi_dir(aoi_dir, huc_dir), cfg=cfg).run()
