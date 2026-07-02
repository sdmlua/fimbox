"""
Stage 3 — Feature ID extraction + NWM retrospective streamflow for all HUC8s.

Step 3a: extract_feature_ids  → out/<HUC>/feature_id.csv
Step 3b: getNWMretrospective  → out/<HUC>/discharge-inputs/<date>.csv

Prerequisite: run_stage2_hand.py must have completed for a HUC.

Progress is written to TASK_LOG after each HUC so a re-run skips completed ones.

Run:
    .venv\\Scripts\\python.exe scripts/run_stage3_streamflow.py
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

# NWM retrospective event — edit to your flood event of interest
# Use "date" for a single instant, or set START + END for a continuous range.
EVENT_DATE   = "2020-10-10 21:00:00"   # single event
# START      = "2016-10-05"            # uncomment for range
# END        = "2016-10-20"

TASK_LOG     = Path("E:/SI/out/stage3_status.txt")
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
        f.write(f"stage3 {huc8} {status}{(' ' + note) if note else ''}\n")


def run_huc(huc8: str) -> None:
    from fimbox import extract_feature_ids, getNWMretrospective

    aoi_root = OUT_DIR / f"HUC{huc8}"
    if not aoi_root.exists():
        raise FileNotFoundError(f"AOI folder not found — run Stage 1 first: {aoi_root}")

    # Step 3a: collect NWM feature IDs from all branch hydroTables
    extract_feature_ids(aoi_root)

    # Step 3b: pull NWM v3 retrospective streamflow for the event
    getNWMretrospective(aoi_root, date=EVENT_DATE)
    # For a date range, replace the line above with:
    # getNWMretrospective(aoi_root, start=START, end=END)


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

    log.info(f"Stage 3: {len(hucs)} total | {len(done)} already done | {len(remaining)} to run")
    log.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    batch_start = time.time()
    huc_times: list[tuple[str, float, str]] = []
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
    log.info(f"Stage 3 complete  |  total time: {_fmt(total)}")
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
