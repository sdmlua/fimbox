"""
Stage 2 — HAND Processing for all HUC8s in the study area.
Runs BranchDerivation then calculate_allbranches (DEM conditioning,
HAND rasters, synthetic rating curves) for each HUC.

Prerequisite: run_stage1_download.py must have completed for a HUC
before this script processes it.

Progress is written to TASK_LOG after each HUC so a re-run skips completed ones.

Run:
    .venv\\Scripts\\python.exe scripts/run_stage2_hand.py
"""
import logging
import time
import traceback
from datetime import datetime
from pathlib import Path
import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH   = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
OUT_DIR      = Path("E:/SI/out")
IDENTIFIER   = "nwmmr"
TASK_LOG     = Path("E:/SI/out/stage2_status.txt")
CONFIG_DIR   = Path("config")   # deny lists live here
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
        f.write(f"stage2 {huc8} {status}{(' ' + note) if note else ''}\n")


def run_huc(huc8: str) -> None:
    from fimbox import AOIProcessingConfig, BranchDerivation, calculate_allbranches
    from fimbox._dask import _resolve_n_workers

    aoi_dir = OUT_DIR / f"HUC{huc8}" / "watershed-data"
    if not aoi_dir.exists():
        raise FileNotFoundError(f"watershed-data not found — run Stage 1 first: {aoi_dir}")

    def _opt(p: Path):
        return p if p.exists() else None

    # Step Z0: level paths + branch polygons
    BranchDerivation(
        out_dir=aoi_dir,
        branch_id_attribute="levpa_id",
        reach_id_attribute="ID",
        branch_buffer_distance_meters=7000.0,
    ).run()

    # Steps Z1 + B: BranchZero (whole AOI) then all non-zero branches in parallel
    cfg = AOIProcessingConfig(
        aoi_dir=aoi_dir,
        branch_list_path=aoi_dir / "branch_ids.lst",
        dem_path=aoi_dir / "dem.tif",
        streams_gpkg=aoi_dir / f"{IDENTIFIER}_subset_streams.gpkg",
        boundary_gpkg=aoi_dir / "wbd_buffered.gpkg",
        bridge_elev_diff_path=_opt(aoi_dir / "bridge_elev_diff.tif"),
        levee_gpkg_path=_opt(aoi_dir / "3d_nld_subset_levees_burned.gpkg"),
        headwaters_gpkg=_opt(aoi_dir / f"{IDENTIFIER}_headwater_points_subset.gpkg"),
        levelpaths_extended_gpkg=_opt(
            aoi_dir / f"{IDENTIFIER}_subset_streams_levelPaths_extended.gpkg"
        ),
        # AGREE DEM conditioning
        agree_buffer_m=15.0,
        agree_smooth_drop=10.0,
        agree_sharp_drop=1000.0,
        # HAND geometry
        cost_distance_tolerance=50.0,
        lateral_elevation_threshold=10,
        max_split_distance_m=1500.0,
        slope_min=0.0001,
        lakes_buffer_dist_m=100.0,
        # SRC
        mannings_n=0.06,
        stage_min_m=0.0,
        stage_interval_m=0.3048,
        stage_max_m=25.2984,
        min_catchment_area=0.25,
        min_stream_length=0.5,
        crosswalk_max_distance_m=100.0,
        src_slope_source="iris_sword",
        iris_slope_csv=None,
        hfab_slope_column=None,
        # execution
        n_workers=_resolve_n_workers(),
        keep_failed_branches=True,
        delete_deny_list=True,
    )

    calculate_allbranches(
        cfg,
        run_branch_zero=True,
        delete_deny_list=True,
        deny_unit_list=CONFIG_DIR / "deny_unit.lst",
        branch_ids_csv=aoi_dir / "branch_ids.csv",
    )


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def main():
    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    done = _load_done()
    remaining = [h for h in hucs if h not in done]

    log.info(f"Stage 2: {len(hucs)} total | {len(done)} already done | {len(remaining)} to run")
    log.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    batch_start  = time.time()
    huc_times: list[tuple[str, float, str]] = []  # (huc8, elapsed_s, status)
    passed, failed = list(done), []

    for i, huc8 in enumerate(remaining, 1):
        huc_start = time.time()
        log.info(f"  [{i}/{len(remaining)}] HUC8 = {huc8}  |  "
                 f"batch elapsed: {_fmt(huc_start - batch_start)}")
        try:
            run_huc(huc8)
            elapsed = time.time() - huc_start
            _log_result(huc8, "PASS")
            passed.append(huc8)
            huc_times.append((huc8, elapsed, "PASS"))

            # running average and ETA over completed HUCs
            completed   = [t for _, t, s in huc_times if s == "PASS"]
            avg_s       = sum(completed) / len(completed)
            remaining_n = len(remaining) - i
            eta_s       = avg_s * remaining_n
            log.info(f"  [{huc8}] PASS  |  this HUC: {_fmt(elapsed)}  |  "
                     f"avg: {_fmt(avg_s)}  |  remaining: {remaining_n}  |  "
                     f"ETA: {_fmt(eta_s)}"
                     + (f"  (~{datetime.fromtimestamp(time.time() + eta_s).strftime('%H:%M')})"
                        if remaining_n > 0 else "  (last HUC)"))
        except Exception:
            elapsed = time.time() - huc_start
            err = traceback.format_exc().splitlines()[-1]
            _log_result(huc8, "FAIL", err)
            failed.append(huc8)
            huc_times.append((huc8, elapsed, "FAIL"))
            log.error(f"  [{huc8}] FAIL after {_fmt(elapsed)}  |  {err}")

    total = time.time() - batch_start
    log.info(f"\n{'─'*60}")
    log.info(f"Stage 2 complete  |  total time: {_fmt(total)}")
    log.info(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Passed: {len(passed)}  |  Failed: {len(failed)}")
    if huc_times:
        log.info(f"\nPer-HUC timing summary:")
        for huc8, elapsed, status in huc_times:
            log.info(f"  {huc8}  {status:4s}  {_fmt(elapsed)}")
    if failed:
        log.warning(f"\nFailed HUCs: {failed}")
        log.warning("Re-run this script to retry (FAIL lines are automatically retried).")


if __name__ == "__main__":
    main()
