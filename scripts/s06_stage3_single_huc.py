"""
Stage 3 — Feature IDs + Streamflow for a SINGLE HUC8.
Use this to test one HUC before running run_stage3_streamflow.py on all 24.

Prerequisite: Stage 2 must have completed for this HUC.

Set HUC8 and EVENT_DATE below and run:
    .venv\\Scripts\\python.exe scripts/run_single_huc_stage3.py
"""
import logging
import traceback
from pathlib import Path

# ── SET THESE ─────────────────────────────────────────────────────
HUC8       = "03020102"
EVENT_DATE = "2020-10-10 21:00:00"   # edit to your flood event
# For a date range instead of a single event, uncomment and use:
# START = "2016-10-05"
# END   = "2016-10-20"
# ─────────────────────────────────────────────────────────────────

OUT_DIR  = Path("D:/SI/out")
TASK_LOG = Path("D:/SI/out/stage3_status.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _log_result(huc8: str, status: str, note: str = "") -> None:
    TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TASK_LOG.open("a") as f:
        f.write(f"stage3 {huc8} {status}{(' ' + note) if note else ''}\n")


def main():
    from fimbox import extract_feature_ids, getNWMretrospective

    aoi_root = OUT_DIR / f"HUC{HUC8}"
    if not aoi_root.exists():
        log.error(f"AOI folder not found — run Stage 1 first: {aoi_root}")
        return

    log.info(f"Stage 3 — HUC8: {HUC8}")
    try:
        log.info("  Step 3a: extracting feature IDs …")
        extract_feature_ids(aoi_root)

        log.info(f"  Step 3b: fetching NWM retrospective for {EVENT_DATE} …")
        getNWMretrospective(aoi_root, date=EVENT_DATE)
        # For a date range replace the line above with:
        # getNWMretrospective(aoi_root, start=START, end=END)

        _log_result(HUC8, "PASS")
        log.info("PASS")
    except Exception:
        err = traceback.format_exc()
        _log_result(HUC8, "FAIL", err.splitlines()[-1])
        log.error(f"FAIL:\n{err}")


if __name__ == "__main__":
    main()
