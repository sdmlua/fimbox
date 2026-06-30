"""
Revert SRCs for branches that were not directly calibrated.

"Directly calibrated" = a non-trunk branch (levpa_id != "0") that has exactly
one USGS gauge with available rating-curve data.  These are the only branches
where n was back-calculated from real observations.

All other modified branches (Branch 0 averaged, ungauged fallback) are restored
from their .pre_n_calib.csv backup to the canonical SRC CSV, and the backup is
deleted.  After this script runs, .pre_n_calib.csv files exist ONLY for the
truly calibrated branches, so find_calibrated_branches() and the plot script
will automatically show only those.

Run:
    .venv\\Scripts\\python.exe scripts/revert_uncalibrated_srcs.py
"""
import shutil
from pathlib import Path

import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
HUC8     = "03020102"
OUT_DIR  = Path("D:/SI/out")
DATA_DIR = Path("data")
# ─────────────────────────────────────────────────────────────────

AOI_ROOT  = OUT_DIR / f"HUC{HUC8}"
WATERSHED = AOI_ROOT / "watershed-data"
BRANCHES  = WATERSHED / "branches"


def get_directly_calibrated_branches() -> set[str]:
    """
    Returns branch IDs where n was individually back-calculated from a single
    USGS gauge with rating-curve data.  Excludes:
      - Branch 0  (trunk; received an average of two gauges)
      - Any branch whose gauge has no entry in usgs_rating_curves.parquet
    """
    elev = pd.read_csv(
        AOI_ROOT / "usgs_elev_table.csv",
        dtype={"location_id": str, "feature_id": "Int64"},
    )
    rc   = pd.read_parquet(DATA_DIR / "usgs_rating_curves.parquet")
    rc_ids = set(rc["location_id"].astype(str).unique())

    non_trunk = elev[elev["levpa_id"].astype(str) != "0"].copy()
    non_trunk["has_rc"] = non_trunk["location_id"].apply(
        lambda loc: str(loc).zfill(8) in rc_ids
    )
    return set(non_trunk[non_trunk["has_rc"]]["levpa_id"].astype(str).unique())


def main():
    directly_calibrated = get_directly_calibrated_branches()
    print(f"Directly calibrated branches (keeping): {sorted(directly_calibrated)}")
    print()

    reverted, kept, no_backup = [], [], []

    for branch_dir in sorted(d for d in BRANCHES.iterdir() if d.is_dir()):
        bid     = branch_dir.name
        backup  = branch_dir / f"src_full_crosswalked_{bid}.pre_n_calib.csv"
        main_csv = branch_dir / f"src_full_crosswalked_{bid}.csv"

        if not backup.exists():
            no_backup.append(bid)
            continue

        if bid in directly_calibrated:
            kept.append(bid)
            print(f"  KEEP    branch {bid}  (directly calibrated, backup preserved)")
            continue

        # Restore original SRC from backup, remove backup
        shutil.copy2(backup, main_csv)
        backup.unlink()
        reverted.append(bid)
        print(f"  REVERT  branch {bid}  -> original SRC restored, backup removed")

    print()
    print(f"Reverted : {len(reverted)} branch(es) -> {reverted}")
    print(f"Kept     : {len(kept)}     branch(es) -> {kept}")
    print(f"Untouched: {len(no_backup)} branch(es) (never modified)")


if __name__ == "__main__":
    main()
