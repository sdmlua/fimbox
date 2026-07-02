"""
Stage 5 — FIM Generation for all HUC8s in the study area.

Generates inundation extent + depth rasters for every discharge CSV
found in out/<HUC>/discharge-inputs/.

Prerequisite: run_stage3_streamflow.py must have completed for a HUC
(discharge CSVs must exist before this script runs).

Progress is written to TASK_LOG after each HUC so a re-run skips completed ones.

Run:
    .venv\\Scripts\\python.exe scripts/run_stage5_fim.py
"""
import logging
import traceback
from pathlib import Path
import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH   = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
OUT_DIR      = Path("D:/SI/out")
TASK_LOG     = Path("D:/SI/out/stage5_status.txt")
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
        f.write(f"stage5 {huc8} {status}{(' ' + note) if note else ''}\n")


def run_huc(huc8: str) -> None:
    from fimbox import generateFIM
    from fimbox._dask import _resolve_n_workers

    aoi_root = OUT_DIR / f"HUC{huc8}"
    discharge_dir = aoi_root / "discharge-inputs"

    if not aoi_root.exists():
        raise FileNotFoundError(f"AOI folder not found — run Stage 1 first: {aoi_root}")
    if not discharge_dir.exists() or not list(discharge_dir.glob("*.csv")):
        raise FileNotFoundError(
            f"No discharge CSVs found — run Stage 3 (streamflow) first: {discharge_dir}"
        )

    results = generateFIM(
        aoi_root, n_workers=_resolve_n_workers(), depth=True
    ).from_discharge_inputs()

    log.info(f"  [{huc8}] {len(results)} FIM raster(s) written to {aoi_root / 'fim-outputs'}")


def main():
    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    done = _load_done()
    remaining = [h for h in hucs if h not in done]

    log.info(f"Stage 5: {len(hucs)} total | {len(done)} already done | {len(remaining)} to run")

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

    log.info(f"\nStage 5 done. Passed: {len(passed)}  Failed: {len(failed)}")
    if failed:
        log.warning(f"Failed HUCs: {failed}")


if __name__ == "__main__":
    main()
