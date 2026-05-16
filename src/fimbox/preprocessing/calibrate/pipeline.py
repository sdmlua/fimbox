"""
Author: Supath Dhital
Date Updated: May 2026

Calibration pipeline orchestrator — fimbox port of
``inundation-mapping/src/calibrate_rating_curves.sh``.

Order of operations mirrors the bash script. Each step is gated by an
explicit boolean toggle in ``CalibrationConfig``; defaults match the safer
combinations used in inundation-mapping's ``params_template.env`` (most
heavy SRC-adjustment routines are OFF by default).

Steps (in execution order)
--------------------------
1. reset_hydro_and_src          (only when calibration_rerun=True)
2. aggregate_branches(usgs_elev=True, ras_elev=True)
3. thalweg_notches_adjustment    (toggle)
4. longitudinal_flow_adjustment  (toggle)
5. bathymetric_adjustment        (toggle)
6. identify_src_bankfull         (toggle; required by subdiv)
7. subdiv_chan_obank_src         (toggle; requires bankfull)
8. nonmonotonic_src_adjustment   (toggle)
9. src_adjust_usgs_rating_trace  (toggle; requires subdiv)
10. src_adjust_ras2fim_rating    (toggle; requires subdiv)
11. src_adjust_spatial_obs       (toggle; requires subdiv)
12. src_manual_calibration       (toggle)
13. aggregate_branches(htable=True, bridge=True, road=True)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .aggregate_branches_to_aoi import aggregate_branches
from .reset_htable_src import reset_hydro_and_src
from .src_manual_calibration import manual_calibration

from .thalweg_notches_adjustment import thalweg_notches_adjustment
from .longitudinal_flow_adjustment import longitudinal_flow_adjustment
from .bathymetric_adjustment import bathymetric_adjustment
from .identify_src_bankfull import identify_src_bankfull
from .subdiv_chan_obank_src import subdiv_chan_obank_src
from .nonmonotonic_src_adjustment import nonmonotonic_src_adjustment
from .src_adjust_usgs_rating_trace import src_adjust_usgs_rating_trace
from .src_adjust_ras2fim_rating import src_adjust_ras2fim_rating
from .src_adjust_spatial_obs import src_adjust_spatial_obs
from ._stub import CalibrationNotImplemented

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


@dataclass
class CalibrationConfig:
    """Toggleable calibration pipeline (mirrors bash_variables.env)."""

    # Reruns reset hydroTables to pre-calibration state before re-applying
    calibration_rerun: bool = False

    # Heavy routines — OFF by default until validated end-to-end
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

    # Inputs needed by toggled-on routines
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

    # Worker count for branch-parallel routines
    job_branch_limit: int = 1

    # Stub behaviour: when True, the pipeline logs a warning and skips
    # toggled-on but not-yet-ported routines instead of raising.
    skip_unimplemented: bool = False


def run_calibration(
    aoi_dir: Optional[PathLike] = None,
    cfg: Optional[CalibrationConfig] = None,
    *,
    huc_dir: Optional[PathLike] = None,
) -> None:
    """Run the full calibration chain on ``aoi_dir`` using ``cfg`` toggles.

    ``aoi_dir`` and ``huc_dir`` are equivalent — pass whichever matches your
    workflow."""
    from ._stub import resolve_aoi_dir

    aoi_dir = Path(resolve_aoi_dir(aoi_dir, huc_dir))
    if cfg is None:
        raise TypeError("cfg= (CalibrationConfig) is required.")
    aoi_id = aoi_dir.name
    log.info(f"=== Calibration pipeline: {aoi_id} ===")
    log.info(f"calibration_rerun={cfg.calibration_rerun}")

    if cfg.calibration_rerun:
        log.info("--- reset hydroTable + src_full_crosswalked ---")
        reset_hydro_and_src(aoi_dir)

    log.info("--- aggregate usgs + ras2fim elev tables ---")
    aggregate_branches(aoi_dir, usgs_elev=True, ras_elev=True)

    _maybe(cfg.thalweg_notches_adjustment, cfg.skip_unimplemented,
           "thalweg notches adjustment",
           lambda: thalweg_notches_adjustment(aoi_dir))

    _maybe(cfg.longitudinal_filter, cfg.skip_unimplemented,
           "longitudinal discharge adjustment",
           lambda: longitudinal_flow_adjustment(aoi_dir))

    if cfg.bathymetry_adjust:
        if cfg.bathy_file_ehydro is None or cfg.bathy_file_aibased is None:
            raise ValueError("bathymetry_adjust requires bathy_file_ehydro + bathy_file_aibased")
        _maybe(True, cfg.skip_unimplemented, "bathymetry adjustment",
               lambda: bathymetric_adjustment(
                   aoi_dir,
                   bathy_file_ehydro=cfg.bathy_file_ehydro,
                   bathy_file_aibased=cfg.bathy_file_aibased,
                   ai_toggle=cfg.ai_toggle,
               ))

    if cfg.src_bankfull_toggle:
        if cfg.bankfull_flows_file is None:
            raise ValueError("src_bankfull_toggle requires bankfull_flows_file")
        _maybe(True, cfg.skip_unimplemented, "SRC bankfull identification",
               lambda: identify_src_bankfull(
                   aoi_dir, cfg.bankfull_flows_file, n_workers=cfg.job_branch_limit
               ))

    if cfg.src_subdiv_toggle and cfg.src_bankfull_toggle:
        if cfg.vmann_input_file is None:
            raise ValueError("src_subdiv_toggle requires vmann_input_file")
        _maybe(True, cfg.skip_unimplemented, "SRC channel/overbank subdivision",
               lambda: subdiv_chan_obank_src(
                   aoi_dir, cfg.vmann_input_file, n_workers=cfg.job_branch_limit
               ))

    _maybe(cfg.nonmonotonic_src_adjustment, cfg.skip_unimplemented,
           "nonmonotonic SRC adjustment",
           lambda: nonmonotonic_src_adjustment(aoi_dir))

    if cfg.src_adjust_usgs and cfg.src_subdiv_toggle:
        required = (cfg.usgs_rating_curve_csv, cfg.usgs_acceptable_gages, cfg.nwm_recur_file)
        if any(x is None for x in required):
            raise ValueError("src_adjust_usgs requires usgs_rating_curve_csv, usgs_acceptable_gages, nwm_recur_file")
        _maybe(True, cfg.skip_unimplemented, "SRC adjust (USGS rating curves)",
               lambda: src_adjust_usgs_rating_trace(
                   aoi_dir, cfg.usgs_rating_curve_csv, cfg.usgs_acceptable_gages,
                   cfg.nwm_recur_file, n_workers=cfg.job_branch_limit,
               ))

    if cfg.src_adjust_ras2fim and cfg.src_subdiv_toggle:
        if cfg.ras_rating_curve_csv is None or cfg.nwm_recur_file is None:
            raise ValueError("src_adjust_ras2fim requires ras_rating_curve_csv + nwm_recur_file")
        _maybe(True, cfg.skip_unimplemented, "SRC adjust (RAS2FIM)",
               lambda: src_adjust_ras2fim_rating(
                   aoi_dir, cfg.ras_rating_curve_csv, cfg.nwm_recur_file,
                   n_workers=cfg.job_branch_limit,
               ))

    if cfg.src_adjust_spatial and cfg.src_subdiv_toggle:
        _maybe(True, cfg.skip_unimplemented, "SRC adjust (spatial observations)",
               lambda: src_adjust_spatial_obs(aoi_dir, n_workers=cfg.job_branch_limit))

    if cfg.manual_calb_toggle:
        if cfg.man_calb_file is None or not Path(cfg.man_calb_file).is_file():
            log.warning(f"manual_calb_toggle set but file missing: {cfg.man_calb_file}")
        else:
            log.info("--- manual calibration ---")
            manual_calibration(aoi_dir, cfg.man_calb_file)

    log.info("--- aggregate hydroTable + bridge + road outputs ---")
    aggregate_branches(aoi_dir, htable=True, bridge=True, road=True)
    log.info("=== Calibration pipeline complete ===")


def _maybe(enabled: bool, skip_unimplemented: bool, name: str, fn) -> None:
    if not enabled:
        return
    log.info(f"--- {name} ---")
    try:
        fn()
    except CalibrationNotImplemented as exc:
        if skip_unimplemented:
            log.warning(f"Skipping {name}: {exc}")
        else:
            raise


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import attach_case_log

    parser = argparse.ArgumentParser(
        description="Run the calibration pipeline on an AOI output directory."
    )
    parser.add_argument("-aoi_dir", required=True)
    parser.add_argument("--calibration-rerun", action="store_true")
    parser.add_argument("--manual-calb", default=None, help="Path to manual calibration CSV")
    parser.add_argument("--skip-unimplemented", action="store_true",
                        help="Warn-and-skip calibration steps that are not yet ported.")
    parser.add_argument("--job-branch-limit", type=int, default=1)
    args = parser.parse_args()

    attach_case_log(args.aoi_dir)
    cfg = CalibrationConfig(
        calibration_rerun=args.calibration_rerun,
        manual_calb_toggle=args.manual_calb is not None,
        man_calb_file=Path(args.manual_calb) if args.manual_calb else None,
        skip_unimplemented=args.skip_unimplemented,
        job_branch_limit=args.job_branch_limit,
    )
    run_calibration(args.aoi_dir, cfg)
