"""
Stage 4 — Calibration for a SINGLE HUC8.
Use this to test one HUC before running run_stage4_calibration.py on all 24.

Prerequisite: Stage 2 must have completed for this HUC.

Set HUC8 below and run:
    .venv\\Scripts\\python.exe scripts/run_single_huc_stage4.py
"""
import logging
import traceback
from pathlib import Path

# ── SET THIS ──────────────────────────────────────────────────────
HUC8 = "03020102"
# ─────────────────────────────────────────────────────────────────

OUT_DIR  = Path("D:/SI/out")
DATA_DIR = Path("data")
TASK_LOG = Path("D:/SI/out/stage4_status.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _log_result(huc8: str, status: str, note: str = "") -> None:
    TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TASK_LOG.open("a") as f:
        f.write(f"stage4 {huc8} {status}{(' ' + note) if note else ''}\n")


def main():
    from fimbox import run_calibration, CalibrationConfig
    from fimbox._dask import _resolve_n_workers

    aoi_root = OUT_DIR / f"HUC{HUC8}"
    if not aoi_root.exists():
        log.error(f"AOI folder not found — run Stage 1 first: {aoi_root}")
        return

    log.info(f"Stage 4 — HUC8: {HUC8}")
    try:
        cfg = CalibrationConfig(
            calibration_rerun=True,
            aggregate_pre=True,
            thalweg_notches_adjustment=True,
            longitudinal_filter=True,
            bathymetry_adjust=True,
            bathy_file_ehydro=DATA_DIR / "final_bathymetry_ehydro_ohrfc.gpkg",
            src_bankfull_toggle=True,
            bankfull_flows_file=DATA_DIR / "nwm3_high_water_threshold_cms.parquet",
            include_branch_zero=True,
            src_subdiv_toggle=True,
            vmann_input_file=DATA_DIR / "mannings_global_optz.parquet",
            default_channel_n=0.06,
            default_overbank_n=0.12,
            nonmonotonic_src_adjustment=True,
            nonmonotonic_stream_order_min=4,
            src_adjust_usgs=True,
            usgs_rating_curve_csv=DATA_DIR / "usgs_rating_curves.parquet",
            usgs_acceptable_gages=DATA_DIR / "acceptable_sites_for_rating_curves.parquet",
            nwm_recur_file=DATA_DIR / "nwm3_17C_recurrence_flows_cfs.parquet",
            src_adjust_spatial=True,
            calib_points_file=None,
            manual_calb_toggle=True,
            man_calb_file=None,
            aggregate_post=True,
            scan_logs=True,
            job_branch_limit=_resolve_n_workers(),
            skip_unimplemented=True,
        )
        run_calibration(aoi_root, cfg)
        _log_result(HUC8, "PASS")
        log.info("PASS")
    except Exception:
        err = traceback.format_exc()
        _log_result(HUC8, "FAIL", err.splitlines()[-1])
        log.error(f"FAIL:\n{err}")


if __name__ == "__main__":
    main()
