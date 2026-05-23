"""
Author: Supath Dhital
Date Updated: May 2026

Calibration pipeline orchestrator. Each step is gated by an explicit
boolean on CalibrationConfig; defaults match the safer combinations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._common import CalibrationNotImplemented, PathLike, resolve_aoi_dir
from .aggregate import BranchAggregator
from .dem_adjust import (
    BathymetricAdjustment,
    LongitudinalFlowFilter,
    ThalwegNotchesAdjustment,
)
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
    # Reruns reset hydroTables to pre-calibration state before re-applying.
    calibration_rerun: bool = False

    # Step toggles. SRC bankfull + subdivision are the two routines that
    # produce the biggest accuracy lift; the others are smaller refinements
    # and are off by default.
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

    # Inputs needed by the toggled-on routines.
    bathy_file_ehydro: Optional[Path] = None
    bathy_file_aibased: Optional[Path] = None
    ai_toggle: int = 0
    bankfull_flows_file: Optional[Path] = None
    vmann_input_file: Optional[Path] = None
    usgs_rating_curve_csv: Optional[Path] = None
    usgs_acceptable_gages: Optional[Path] = None
    nwm_recur_file: Optional[Path] = None
    ras_rating_curve_csv: Optional[Path] = None
    man_calb_file: Optional[Path] = None

    # Worker count for branch-parallel routines.
    job_branch_limit: int = 1

    # When True, the pipeline warns and skips toggled-on routines that are
    # not yet ported instead of raising.
    skip_unimplemented: bool = False


@dataclass
class Calibrator:
    aoi_dir: PathLike
    cfg: CalibrationConfig

    def run(self) -> None:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        cfg = self.cfg
        aoi_id = aoi_dir.name
        log.info(f"=== Calibration pipeline: {aoi_id} ===")
        log.info(f"calibration_rerun={cfg.calibration_rerun}")

        if cfg.calibration_rerun:
            log.info("--- reset hydroTable + src_full_crosswalked ---")
            HydroTableReset(aoi_dir=aoi_dir).run()

        log.info("--- aggregate usgs + ras2fim elev tables ---")
        BranchAggregator(aoi_dir=aoi_dir, usgs_elev=True, ras_elev=True).run()

        self._maybe(
            cfg.thalweg_notches_adjustment, "thalweg notches adjustment",
            lambda: ThalwegNotchesAdjustment(aoi_dir=aoi_dir).run(),
        )

        self._maybe(
            cfg.longitudinal_filter, "longitudinal discharge adjustment",
            lambda: LongitudinalFlowFilter(aoi_dir=aoi_dir).run(),
        )

        if cfg.bathymetry_adjust:
            if cfg.bathy_file_ehydro is None or cfg.bathy_file_aibased is None:
                raise ValueError(
                    "bathymetry_adjust requires bathy_file_ehydro + bathy_file_aibased"
                )
            self._maybe(
                True, "bathymetry adjustment",
                lambda: BathymetricAdjustment(
                    aoi_dir=aoi_dir,
                    bathy_file_ehydro=cfg.bathy_file_ehydro,
                    bathy_file_aibased=cfg.bathy_file_aibased,
                    ai_toggle=cfg.ai_toggle,
                ).run(),
            )

        if cfg.src_bankfull_toggle:
            if cfg.bankfull_flows_file is None:
                raise ValueError("src_bankfull_toggle requires bankfull_flows_file")
            self._maybe(
                True, "SRC bankfull identification",
                lambda: SrcBankfull(
                    aoi_dir=aoi_dir,
                    bankfull_flows_file=cfg.bankfull_flows_file,
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        if cfg.src_subdiv_toggle and cfg.src_bankfull_toggle:
            if cfg.vmann_input_file is None:
                raise ValueError("src_subdiv_toggle requires vmann_input_file")
            self._maybe(
                True, "SRC channel/overbank subdivision",
                lambda: SrcSubdiv(
                    aoi_dir=aoi_dir,
                    vmann_table=cfg.vmann_input_file,
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        self._maybe(
            cfg.nonmonotonic_src_adjustment, "nonmonotonic SRC adjustment",
            lambda: SrcNonmonotonic(aoi_dir=aoi_dir).run(),
        )

        if cfg.src_adjust_usgs and cfg.src_subdiv_toggle:
            required = (cfg.usgs_rating_curve_csv, cfg.usgs_acceptable_gages, cfg.nwm_recur_file)
            if any(x is None for x in required):
                raise ValueError(
                    "src_adjust_usgs requires usgs_rating_curve_csv, "
                    "usgs_acceptable_gages, nwm_recur_file"
                )
            self._maybe(
                True, "SRC adjust (USGS rating curves)",
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
                True, "SRC adjust (RAS2FIM)",
                lambda: Ras2fimCalibrator(
                    aoi_dir=aoi_dir,
                    ras_rating_curve_csv=cfg.ras_rating_curve_csv,
                    nwm_recur_file=cfg.nwm_recur_file,
                    n_workers=cfg.job_branch_limit,
                ).run(),
            )

        if cfg.src_adjust_spatial and cfg.src_subdiv_toggle:
            self._maybe(
                True, "SRC adjust (spatial observations)",
                lambda: SpatialObsCalibrator(
                    aoi_dir=aoi_dir, n_workers=cfg.job_branch_limit
                ).run(),
            )

        if cfg.manual_calb_toggle:
            if cfg.man_calb_file is None or not Path(cfg.man_calb_file).is_file():
                log.warning(f"manual_calb_toggle set but file missing: {cfg.man_calb_file}")
            else:
                log.info("--- manual calibration ---")
                ManualCalibrator(
                    aoi_dir=aoi_dir, calibration_file=cfg.man_calb_file
                ).run()

        log.info("--- aggregate hydroTable + bridge + road outputs ---")
        BranchAggregator(
            aoi_dir=aoi_dir, htable=True, bridge=True, road=True
        ).run()
        log.info("=== Calibration pipeline complete ===")

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
        raise TypeError("cfg= (CalibrationConfig) is required.")
    Calibrator(aoi_dir=resolve_aoi_dir(aoi_dir, huc_dir), cfg=cfg).run()
