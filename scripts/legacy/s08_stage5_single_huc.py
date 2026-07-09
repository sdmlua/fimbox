"""
Stage 5 — FIM Generation for a SINGLE HUC8.
Use this to test one HUC before running run_stage5_fim.py on all 24.

Prerequisite: Stages 2 and 3 must have completed for this HUC
(branches and discharge CSVs must exist).

Set HUC8 below and run:
    .venv\\Scripts\\python.exe scripts/run_single_huc_stage5.py
"""
import logging
import traceback
from pathlib import Path

# ── SET THIS ──────────────────────────────────────────────────────
HUC8 = "03020102"
# ─────────────────────────────────────────────────────────────────

OUT_DIR  = Path("D:/SI/out")
TASK_LOG = Path("D:/SI/out/stage5_status.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _log_result(huc8: str, status: str, note: str = "") -> None:
    TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TASK_LOG.open("a") as f:
        f.write(f"stage5 {huc8} {status}{(' ' + note) if note else ''}\n")


def main():
    from fimbox import generateFIM
    from fimbox._dask import _resolve_n_workers

    aoi_root = OUT_DIR / f"HUC{HUC8}"
    discharge_dir = aoi_root / "discharge-inputs"

    if not aoi_root.exists():
        log.error(f"AOI folder not found — run Stage 1 first: {aoi_root}")
        return
    if not discharge_dir.exists() or not list(discharge_dir.glob("*.csv")):
        log.error(f"No discharge CSVs found — run Stage 3 first: {discharge_dir}")
        return

    log.info(f"Stage 5 — HUC8: {HUC8}")
    try:
        results = generateFIM(
            aoi_root, n_workers=_resolve_n_workers(), depth=True
        ).from_discharge_inputs()
        _log_result(HUC8, "PASS")
        log.info(f"PASS — {len(results)} FIM raster(s) → {aoi_root / 'fim-outputs'}")
    except Exception:
        err = traceback.format_exc()
        _log_result(HUC8, "FAIL", err.splitlines()[-1])
        log.error(f"FAIL:\n{err}")


if __name__ == "__main__":
    main()
