"""
Stage 4 — Synthetic Rating Curve calibration for all HUC8s.

Applies bankfull identification, channel/overbank subdivision, and USGS
rating-curve calibration to the per-branch hydroTables produced by Stage 2.

Prerequisite: run_stage2_hand.py must have completed for a HUC.

Progress is written to TASK_LOG after each HUC so a re-run skips completed ones.

Run:
    .venv\\Scripts\\python.exe scripts/run_stage4_calibration.py
"""
import logging
import traceback
from pathlib import Path
import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH   = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
OUT_DIR      = Path("D:/SI/out")
DATA_DIR     = Path("data")          # calibration tables shipped in the repo
TASK_LOG     = Path("D:/SI/out/stage4_status.txt")
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _load_done() -> set[str]:
    if not TASK_LOG.exists():
        return set()
    done = set()
    for line in TASK_LOG.read_text().splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[2] == "PASS":
            done.add(parts[1])
    return done


def _log_result(huc8: str, status: str, note: str = "") -> None:
    TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TASK_LOG.open("a") as f:
        f.write(f"stage4 {huc8} {status}{(' ' + note) if note else ''}\n")


def run_huc(huc8: str) -> None:
    from fimbox import run_calibration, CalibrationConfig
    from fimbox._dask import _resolve_n_workers

    aoi_root = OUT_DIR / f"HUC{huc8}"
    if not aoi_root.exists():
        raise FileNotFoundError(f"AOI folder not found — run Stage 1 first: {aoi_root}")

    cfg = CalibrationConfig(
        # reset — revert to uncalibrated baseline on re-runs
        calibration_rerun=True,

        # aggregate_pre — assemble usgs/ras2fim elev tables before adjustments
        aggregate_pre=True,

        # thalweg — remove thalweg-notch artifact rows, refill stage ladder
        thalweg_notches_adjustment=True,

        # longitudinal — smooth hydraulic geometry along reach chains
        longitudinal_filter=True,

        # bathymetry — add missing in-channel area below DEM (self-skips if no overlap)
        bathymetry_adjust=True,
        bathy_file_ehydro=DATA_DIR / "final_bathymetry_ehydro_ohrfc.gpkg",

        # bankfull — identify bankfull stage in every branch SRC
        src_bankfull_toggle=True,
        bankfull_flows_file=DATA_DIR / "nwm3_high_water_threshold_cms.parquet",
        include_branch_zero=True,

        # subdiv — channel/overbank subdivision (needs vmann + bankfull on)
        src_subdiv_toggle=True,
        vmann_input_file=DATA_DIR / "mannings_global_optz.parquet",
        default_channel_n=0.06,
        default_overbank_n=0.12,

        # nonmonotonic — force monotonic in-channel rating curves
        nonmonotonic_src_adjustment=True,
        nonmonotonic_stream_order_min=4,

        # usgs — calibrate SRCs against USGS rating curves at NWM recurrence flows
        src_adjust_usgs=True,
        usgs_rating_curve_csv=DATA_DIR / "usgs_rating_curves.parquet",
        usgs_acceptable_gages=DATA_DIR / "acceptable_sites_for_rating_curves.parquet",
        nwm_recur_file=DATA_DIR / "nwm3_17C_recurrence_flows_cfs.parquet",

        # spatial — self-skips when calib_points_file=None (no benchmark points yet)
        src_adjust_spatial=True,
        calib_points_file=None,

        # manual — self-skips when man_calb_file=None
        manual_calb_toggle=True,
        man_calb_file=None,

        # aggregate_post — publish htable + bridge + road to AOI root
        aggregate_post=True,

        # log scan — collect error/warning lines into per-AOI summary files
        scan_logs=True,

        # execution
        job_branch_limit=_resolve_n_workers(),
        skip_unimplemented=True,
    )
    run_calibration(aoi_root, cfg)


def main():
    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    done = _load_done()
    remaining = [h for h in hucs if h not in done]

    log.info(f"Stage 4: {len(hucs)} total | {len(done)} already done | {len(remaining)} to run")

    passed, failed = list(done), []
    for i, huc8 in enumerate(remaining, 1):
        log.info(f"  [{i}/{len(remaining)}] HUC8 = {huc8}")
        try:
            run_huc(huc8)
            _log_result(huc8, "PASS")
            passed.append(huc8)
            log.info(f"  [{huc8}] PASS")
        except Exception:
            err = traceback.format_exc().splitlines()[-1]
            _log_result(huc8, "FAIL", err)
            failed.append(huc8)
            log.error(f"  [{huc8}] FAIL: {err}")

    log.info(f"\nStage 4 done. Passed: {len(passed)}  Failed: {len(failed)}")
    if failed:
        log.warning(f"Failed HUCs: {failed}")


if __name__ == "__main__":
    main()
