"""
Author: Supath Dhital
Date Updated: June 2026

Synthetic rating curve (SRC) calibration pipeline.

The whole configuration lives in a single ``CalibrationConfig`` dataclass:
the toggles that decide which steps run and the input file paths those steps
consume. Every step is gated by an explicit boolean, and the field defaults
form the "default pipeline" — the two always-on aggregations that bracket the
run, plus a reset when ``calibration_rerun`` is set. Every optional adjustment
stays off until you flip its toggle and supply its input file.

Usage
-----
    from fimbox import CalibrationConfig, run_calibration

    # default pipeline: aggregate elev tables -> aggregate htable/bridge/road
    run_calibration(aoi_dir, CalibrationConfig())

    # turn on the two big-lift SRC routines
    run_calibration(aoi_dir, CalibrationConfig(
        src_bankfull_toggle=True, bankfull_flows_file="bankfull.csv",
        src_subdiv_toggle=True,   vmann_input_file="mannings.csv",
        nonmonotonic_src_adjustment=True,
        job_branch_limit=4,
    ))

Each ``Calibrator`` step can also be run on its own (see the individual
classes in this subpackage), which is what the step-by-step tests exercise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ...logging_utils import attach_case_log
from ._common import CalibrationNotImplemented, PathLike, aoi_id_of, resolve_aoi_dir
from .aggregate import BranchAggregator
from .dem_adjust import (
    BathymetricAdjustment,
    LongitudinalFlowFilter,
    ThalwegNotchesAdjustment,
)
from .logscan import LogScanner
from .reset import HydroTableReset
from .src_adjust import SrcBankfull, SrcNonmonotonic, SrcSubdiv
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

    The defaults below ARE the default pipeline: a rerun resets first, then
    the two bracketing aggregations always run, and every optional
    adjustment stays off until you toggle it on and provide its input file.
    """

    # --- mode ----
    # Reruns reset hydroTables to the pre-calibration baseline before re-applying any adjustments.
    calibration_rerun: bool = False

    # --- step toggles ----
    # SRC bankfull + subdivision give the biggest accuracy lift; everything else is a smaller refinement
    thalweg_notches_adjustment: bool = False
    longitudinal_filter: bool = False
    bathymetry_adjust: bool = False
    src_bankfull_toggle: bool = False
    src_subdiv_toggle: bool = False
    nonmonotonic_src_adjustment: bool = False
    src_adjust_usgs: bool = False
    src_adjust_ras2fim: bool = False
    src_adjust_spatial: bool = False
    manual_calb_toggle: bool = False

    # --- input files consumed by the toggled-on routines ----
    bathy_file_ehydro: Optional[PathLike] = None
    bathy_file_aibased: Optional[PathLike] = None
    ai_toggle: int = 0
    ai_strm_order: int = 4  # min stream order for AI-based bathymetry
    bankfull_flows_file: Optional[PathLike] = None
    vmann_input_file: Optional[PathLike] = None
    usgs_rating_curve_csv: Optional[PathLike] = None
    usgs_acceptable_gages: Optional[PathLike] = None
    nwm_recur_file: Optional[PathLike] = None
    ras_rating_curve_csv: Optional[PathLike] = None
    man_calb_file: Optional[PathLike] = None

    # --- tunables ------
    # Default Manning's n used by the subdivision step when a feature_id is absent from vmann_input_file.
    default_channel_n: float = 0.06
    default_overbank_n: float = 0.12
    nonmonotonic_stream_order_min: int = 1
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
    # Worker count for the branch-parallel routines.
    job_branch_limit: int = 1

    # When True, toggled-on routines that aren't ported yet warn and skip instead of raising CalibrationNotImplemented.
    skip_unimplemented: bool = False


@dataclass
class Calibrator:
    aoi_dir: PathLike
    cfg: CalibrationConfig

    def run(self) -> None:
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
            if cfg.bathy_file_ehydro is None or cfg.bathy_file_aibased is None:
                raise ValueError(
                    "bathymetry_adjust requires bathy_file_ehydro + bathy_file_aibased"
                )
            self._maybe(
                True,
                "bathymetry adjustment",
                lambda: BathymetricAdjustment(
                    aoi_dir=aoi_dir,
                    bathy_file_ehydro=cfg.bathy_file_ehydro,
                    bathy_file_aibased=cfg.bathy_file_aibased,
                    ai_toggle=cfg.ai_toggle,
                    ai_strm_order=cfg.ai_strm_order,
                ).run(),
            )

        if cfg.src_bankfull_toggle:
            if cfg.bankfull_flows_file is None:
                raise ValueError("src_bankfull_toggle requires bankfull_flows_file")
            self._maybe(
                True,
                "SRC bankfull identification",
                lambda: SrcBankfull(
                    aoi_dir=aoi_dir,
                    bankfull_flows_file=cfg.bankfull_flows_file,
                    n_workers=cfg.job_branch_limit,
                    include_branch_zero=cfg.include_branch_zero,
                ).run(),
            )

        if cfg.src_subdiv_toggle and cfg.src_bankfull_toggle:
            if cfg.vmann_input_file is None:
                raise ValueError("src_subdiv_toggle requires vmann_input_file")
            self._maybe(
                True,
                "SRC channel/overbank subdivision",
                lambda: SrcSubdiv(
                    aoi_dir=aoi_dir,
                    vmann_table=cfg.vmann_input_file,
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
            ).run(),
        )

        if cfg.src_adjust_usgs and cfg.src_subdiv_toggle:
            required = (
                cfg.usgs_rating_curve_csv,
                cfg.usgs_acceptable_gages,
                cfg.nwm_recur_file,
            )
            if any(x is None for x in required):
                raise ValueError(
                    "src_adjust_usgs requires usgs_rating_curve_csv, "
                    "usgs_acceptable_gages, nwm_recur_file"
                )
            self._maybe(
                True,
                "SRC adjust (USGS rating curves)",
                lambda: UsgsRatingCalibrator(
                    aoi_dir=aoi_dir,
                    usgs_rating_curve_csv=cfg.usgs_rating_curve_csv,
                    usgs_acceptable_gages=cfg.usgs_acceptable_gages,
                    nwm_recur_file=cfg.nwm_recur_file,
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        if cfg.src_adjust_ras2fim and cfg.src_subdiv_toggle:
            if cfg.ras_rating_curve_csv is None or cfg.nwm_recur_file is None:
                raise ValueError(
                    "src_adjust_ras2fim requires ras_rating_curve_csv + nwm_recur_file"
                )
            self._maybe(
                True,
                "SRC adjust (RAS2FIM)",
                lambda: Ras2fimCalibrator(
                    aoi_dir=aoi_dir,
                    ras_rating_curve_csv=cfg.ras_rating_curve_csv,
                    nwm_recur_file=cfg.nwm_recur_file,
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        if cfg.src_adjust_spatial and cfg.src_subdiv_toggle:
            self._maybe(
                True,
                "SRC adjust (spatial observations)",
                lambda: SpatialObsCalibrator(
                    aoi_dir=aoi_dir, n_workers=cfg.job_branch_limit
                ).run(),
            )

        if cfg.manual_calb_toggle:
            if cfg.man_calb_file is None or not Path(cfg.man_calb_file).is_file():
                log.warning(
                    f"manual_calb_toggle set but file missing: {cfg.man_calb_file}"
                )
            else:
                log.info("--- manual calibration ---")
                ManualCalibrator(
                    aoi_dir=aoi_dir, calibration_file=cfg.man_calb_file
                ).run()

        if cfg.aggregate_post:
            log.info("--- aggregate hydroTable + bridge + road outputs ---")
            BranchAggregator(
                aoi_dir=aoi_dir, htable=True, bridge=True, road=True
            ).run()

        if cfg.scan_logs:
            log.info("--- scan logs for errors / warnings ---")
            LogScanner(
                aoi_dir=aoi_dir, calibration_rerun=cfg.calibration_rerun
            ).run()

        log.info(f"=== {verb} complete: {aoi_id} ===")

    def _maybe(self, enabled: bool, name: str, fn) -> None:
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


def run_calibration(
    aoi_dir: Optional[PathLike] = None,
    cfg: Optional[CalibrationConfig] = None,
    *,
    huc_dir: Optional[PathLike] = None,
) -> None:
    if cfg is None:
        cfg = CalibrationConfig()
    Calibrator(aoi_dir=resolve_aoi_dir(aoi_dir, huc_dir), cfg=cfg).run()
