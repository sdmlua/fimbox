"""
Stage 1 — Data Download for a SINGLE HUC8.
Use this to test one HUC before running run_stage1_download.py on all 24.

Set HUC8 below and run:
    .venv\\Scripts\\python.exe scripts/run_single_huc_stage1.py
"""
import logging
import traceback
from pathlib import Path

# ── SET THIS ──────────────────────────────────────────────────────
HUC8 = "03020102"
# ─────────────────────────────────────────────────────────────────

DEM_TILES_DIR = Path("D:/SI/out/study_area/watershed-data/dem_tiles")
OUT_DIR       = Path("D:/SI/out")
IDENTIFIER    = "nwmmr"
BUFFER_M      = 5000
TASK_LOG      = Path("D:/SI/out/stage1_status.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _log_result(huc8: str, status: str, note: str = "") -> None:
    TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TASK_LOG.open("a") as f:
        f.write(f"stage1 {huc8} {status}{(' ' + note) if note else ''}\n")


def main():
    import fimbox

    dem_path = DEM_TILES_DIR / f"dem_{HUC8}.tif"
    if not dem_path.exists():
        log.error(f"DEM tile not found: {dem_path}")
        return

    log.info(f"Stage 1 — HUC8: {HUC8}")
    try:
        pp = fimbox.getAllInputData(
            huc8=HUC8,
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
        _log_result(HUC8, "PASS")
        log.info(f"PASS → {OUT_DIR / f'HUC{HUC8}'}")
    except Exception:
        err = traceback.format_exc()
        _log_result(HUC8, "FAIL", err.splitlines()[-1])
        log.error(f"FAIL:\n{err}")


if __name__ == "__main__":
    main()
