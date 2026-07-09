"""
Stage 3b — force monotone rating curves in every branch hydroTable.

Why this exists
---------------
The stage-2 SRC computation assembles A(h)/R(h) cell-by-cell from the HAND
raster, and raster artifacts (bridge decks, pit-fill seams) produce rating
curves where discharge DECREASES with stage on ~7% of reaches.  A non-monotone
curve corrupts the flow→stage lookup in FIM generation (the inundator sorts by
discharge before interpolating).  This is a property of the deterministic
stage-2 code, not a corrupted run — re-running stage 2 reproduces it — so the
fix is a pipeline step, exactly as OWP treats it (their SrcNonmonotonic class).

Why not fimbox's SrcNonmonotonic
--------------------------------
The ported OWP class requires the `bankfull_proxy` column written by
SrcBankfull (part of the OWP stage-4 calibration chain) and silently no-ops
without it.  It also replays geometry columns.  This script is the minimal,
dependency-free equivalent: per (HydroID[, feature_id]) cumulative maximum on
discharge_cms, hydroTable-only (the calibration's single source of truth).

The fix is idempotent and preserves already-monotone curves exactly.

Pipeline position: after stage 2 (s03) and BEFORE calibration (s05a–d), so the
fixed state is what calibration backs up as baseline.

Run:
    .venv\\Scripts\\python.exe scripts/s03b_fix_nonmonotonic_srcs.py
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH   = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
OUT_DIR      = Path("E:/SI/out")
TASK_LOG     = Path("E:/SI/out/s03b_status.txt")
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def fix_hydrotable(ht_path: Path) -> tuple[int, int]:
    """
    Enforce non-decreasing discharge_cms with stage per rating curve.
    Returns (curves_fixed, rows_changed); writes only when something changed.
    """
    ht = pd.read_csv(ht_path, low_memory=False)
    if "discharge_cms" not in ht.columns or "stage" not in ht.columns:
        return 0, 0

    key = ["HydroID", "feature_id"] if "feature_id" in ht.columns else ["HydroID"]
    ht = ht.drop_duplicates(subset=key + ["stage"], keep="first")
    ht = ht.sort_values(key + ["stage"]).reset_index(drop=True)

    before = ht["discharge_cms"].to_numpy(float)
    # pandas cummax leaves NaN rows (e.g. lakes) as NaN and continues the
    # running max across them — exactly the behavior we want.
    ht["discharge_cms"] = ht.groupby(key)["discharge_cms"].cummax()
    after = ht["discharge_cms"].to_numpy(float)

    valid = ~(np.isnan(before) & np.isnan(after))
    changed = valid & ~np.isclose(np.nan_to_num(before, nan=-1),
                                  np.nan_to_num(after,  nan=-1))
    rows_changed = int(changed.sum())
    if rows_changed == 0:
        return 0, 0

    curves_fixed = int(ht.loc[changed, "HydroID"].nunique())
    tmp = ht_path.with_suffix(".tmp.csv")
    ht.to_csv(tmp, index=False)
    os.replace(tmp, ht_path)
    return curves_fixed, rows_changed


def run_huc(huc8: str) -> tuple[int, int, int]:
    branches = OUT_DIR / f"HUC{huc8}" / "watershed-data" / "branches"
    if not branches.exists():
        raise FileNotFoundError(f"branches dir not found: {branches}")
    n_branches = curves = rows = 0
    for branch_dir in sorted(d for d in branches.iterdir() if d.is_dir()):
        bid = branch_dir.name
        ht_path = branch_dir / f"hydroTable_{bid}.csv"
        if not ht_path.exists():
            continue
        c, r = fix_hydrotable(ht_path)
        n_branches += 1
        curves += c
        rows += r
    return n_branches, curves, rows


def main():
    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]

    done = set()
    if TASK_LOG.exists():
        done = {p[1] for line in TASK_LOG.read_text().splitlines()
                if len(p := line.strip().split()) >= 3 and p[2] == "PASS"}
    remaining = [h for h in hucs if h not in done]
    log.info("s03b monotonicity fix: %d total | %d done | %d to run",
             len(hucs), len(done), len(remaining))

    t0 = time.time()
    tot_curves = tot_rows = 0
    for i, huc8 in enumerate(remaining, 1):
        t = time.time()
        try:
            nb, c, r = run_huc(huc8)
        except Exception as exc:
            with TASK_LOG.open("a") as f:
                f.write(f"s03b {huc8} FAIL {exc}\n")
            log.error("  [%d/%d] %s FAIL: %s", i, len(remaining), huc8, exc)
            continue
        tot_curves += c
        tot_rows += r
        with TASK_LOG.open("a") as f:
            f.write(f"s03b {huc8} PASS curves={c} rows={r}\n")
        log.info("  [%d/%d] %s PASS | %d branches | %d curves fixed | %d rows | %.0fs",
                 i, len(remaining), huc8, nb, c, r, time.time() - t)

    log.info("s03b complete in %.0fs | %d curves fixed | %d rows changed",
             time.time() - t0, tot_curves, tot_rows)


if __name__ == "__main__":
    main()
