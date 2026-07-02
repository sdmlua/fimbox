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
import traceback
from pathlib import Path
import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH   = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
OUT_DIR      = Path("D:/SI/out")

# NWM retrospective event — edit to your flood event of interest
# Use "date" for a single instant, or set START + END for a continuous range.
EVENT_DATE   = "2020-10-10 21:00:00"   # single event
# START      = "2016-10-05"            # uncomment for range
# END        = "2016-10-20"

TASK_LOG     = Path("D:/SI/out/stage3_status.txt")
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


def main():
    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    done = _load_done()
    remaining = [h for h in hucs if h not in done]

    log.info(f"Stage 3: {len(hucs)} total | {len(done)} already done | {len(remaining)} to run")

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

    log.info(f"\nStage 3 done. Passed: {len(passed)}  Failed: {len(failed)}")
    if failed:
        log.warning(f"Failed HUCs: {failed}")


if __name__ == "__main__":
    main()
