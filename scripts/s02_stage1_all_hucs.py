"""
Stage 1 — Data Download for all HUC8s in the study area.
Downloads NWM flowlines, catchments, levees, OSM roads, FEMA data.
Uses pre-downloaded 10m DEM tiles from DEM_TILES_DIR.

Progress is written to TASK_LOG after each HUC so a re-run skips completed ones.

Run:
    .venv\\Scripts\\python.exe scripts/run_stage1_download.py
"""
import logging
import traceback
from pathlib import Path
import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH    = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL  = "HUC_CODE"
DEM_TILES_DIR = Path("D:/SI/out/study_area/watershed-data/dem_tiles")
OUT_DIR       = Path("D:/SI/out")
IDENTIFIER    = "nwmmr"
BUFFER_M      = 5000
TASK_LOG      = Path("D:/SI/out/stage1_status.txt")   # records pass/fail per HUC
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _load_done() -> set[str]:
    """Return set of HUC8s already marked PASS in the task log."""
    if not TASK_LOG.exists():
        return set()
    done = set()
    for line in TASK_LOG.read_text().splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[2] == "PASS":
            done.add(parts[1])
    return done


def _log_result(huc8: str, status: str, note: str = "") -> None:
    TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TASK_LOG.open("a") as f:
        f.write(f"stage1 {huc8} {status}{(' ' + note) if note else ''}\n")


def run_huc(huc8: str) -> None:
    import fimbox

    dem_path = DEM_TILES_DIR / f"dem_{huc8}.tif"
    if not dem_path.exists():
        raise FileNotFoundError(f"DEM tile not found: {dem_path}")

    pp = fimbox.getAllInputData(
        huc8=huc8,
        out_dir=str(OUT_DIR),
        buffer_m=BUFFER_M,
        headwater_buffer_cells=8,
        get_flowlines=True,
        get_catchments=True,
        resolution="medium",
        identifier=IDENTIFIER,
        dem=dem_path,
    )
    pp.run()


def main():
    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    done = _load_done()
    remaining = [h for h in hucs if h not in done]

    log.info(f"Stage 1: {len(hucs)} total | {len(done)} already done | {len(remaining)} to run")

    passed, failed = list(done), []
    for i, huc8 in enumerate(remaining, 1):
        log.info(f"  [{i}/{len(remaining)}] HUC8 = {huc8}")
        try:
            run_huc(huc8)
            _log_result(huc8, "PASS")
            passed.append(huc8)
            log.info(f"  [{huc8}] PASS → {OUT_DIR / f'HUC{huc8}'}")
        except Exception:
            err = traceback.format_exc().splitlines()[-1]
            _log_result(huc8, "FAIL", err)
            failed.append(huc8)
            log.error(f"  [{huc8}] FAIL: {err}")

    log.info(f"\nStage 1 done. Passed: {len(passed)}  Failed: {len(failed)}")
    if failed:
        log.warning(f"Failed HUCs: {failed}")
        log.warning(f"Re-run this script to retry failures (edit TASK_LOG to remove their FAIL lines first).")


if __name__ == "__main__":
    main()
