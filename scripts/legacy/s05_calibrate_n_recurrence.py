"""
Post-Stage-2 Manning's n calibration using USGS rating curves at NWM recurrence stages.

Concept
-------
For each USGS gauge in the HUC8, the NWM supplies 5 recurrence-interval flows
(2yr, 5yr, 10yr, 25yr, 50yr in cfs).  The USGS rating curve maps each of those
flows to an absolute water-surface elevation, which we convert to a HAND stage
using the thalweg DEM elevation sampled at the gauge point.

Those 5 HAND stages become 6 depth zones:

    Zone 1  :  0 < h <= h_2yr
    Zone 2  :  h_2yr  < h <= h_5yr
    Zone 3  :  h_5yr  < h <= h_10yr
    Zone 4  :  h_10yr < h <= h_25yr
    Zone 5  :  h_25yr < h <= h_50yr
    Zone 6  :  h > h_50yr           (same n as Zone 5)

For each zone boundary h_r, Manning's equation is inverted against the SRC
hydraulic geometry at that stage to back-calculate n:

    n_r = A(h_r) . R(h_r)^(2/3) . sqrt(S) / Q_r

where Q_r is the NWM recurrence flow and A, R, S come from the SRC table.

The calibrated n values are then applied piecewise to recompute discharge at
every stage in the SRC, replacing the original constant n=0.06 baseline.

Scope
-----
Only "directly calibrated" branches are processed:
  - Non-trunk branches (levpa_id != "0")
  - That have at least one USGS gauge with available rating-curve data

Branch 0 (the watershed trunk) and all ungauged branches are intentionally
left unchanged.  Ungauged branches will be handled by a separate ML-based
regionalisation step.

Prerequisite
------------
run_usgs_gage_crosswalk.py must have run and produced
    D:/SI/out/HUC03020102/usgs_elev_table.csv

Run
---
    .venv\\Scripts\\python.exe scripts/calibrate_n_recurrence.py
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
HUC8     = "03020102"
OUT_DIR  = Path("D:/SI/out")
DATA_DIR = Path("data")

# Physical bounds for back-calculated n (Chow 1959 range).
N_MIN = 0.010   # very smooth concrete
N_MAX = 0.300   # dense brush / floodplain forest
# ─────────────────────────────────────────────────────────────────

RECURRENCE_YEARS = [2, 5, 10, 25, 50]
RECUR_COLS = {yr: f"{yr}_0_year_recurrence_flow_17C" for yr in RECURRENCE_YEARS}

AOI_ROOT  = OUT_DIR / f"HUC{HUC8}"
WATERSHED = AOI_ROOT / "watershed-data"
BRANCHES  = WATERSHED / "branches"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ── Data loading ──────────────────────────────────────────────────

def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load the three external tables needed for calibration."""
    elev_path = AOI_ROOT / "usgs_elev_table.csv"
    if not elev_path.exists():
        raise FileNotFoundError(
            f"usgs_elev_table.csv not found at {elev_path}\n"
            "Run scripts/run_usgs_gage_crosswalk.py first."
        )

    elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
    log.info("usgs_elev_table: %d rows", len(elev))

    rc = pd.read_parquet(DATA_DIR / "usgs_rating_curves.parquet")
    rc["location_id"] = rc["location_id"].astype(str)
    rc["flow_cms"]    = rc["flow"] / 35.3147            # cfs → m³/s
    rc["elev_m"]      = rc["elevation_navd88"] / 3.28084  # ft NAVD88 → m
    log.info("USGS rating curves: %d rows, %d gauges",
             len(rc), rc["location_id"].nunique())

    recur = pd.read_parquet(DATA_DIR / "nwm3_17C_recurrence_flows_cfs.parquet")
    for yr, col in RECUR_COLS.items():
        recur[f"Q_{yr}yr_cms"] = recur[col].astype(float) * 0.028317  # cfs → m³/s
    recur["feature_id"] = recur["feature_id"].astype("Int64")
    log.info("NWM recurrence flows: %d feature_ids", len(recur))

    return elev, rc, recur


def load_src(branch_dir: Path, bid: str) -> pd.DataFrame | None:
    """Load src_full_crosswalked CSV for a branch; return None if missing."""
    p = branch_dir / f"src_full_crosswalked_{bid}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype={"feature_id": str})
    df["HydroID"] = df["HydroID"].astype(int)
    return df


# ── Zone boundary calculation ─────────────────────────────────────

def compute_zone_boundaries(
    location_id: str,
    feature_id: int,
    dem_adj_elevation_m: float,
    rc: pd.DataFrame,
    recur: pd.DataFrame,
) -> dict[int, tuple[float, float]] | None:
    """
    Returns {recurrence_year: (hand_stage_m, target_Q_cms)} for each year.
    Returns None when the gauge or feature_id has insufficient data.
    """
    # Rating curve file uses zero-padded 8-digit IDs (e.g. "02082950").
    loc_padded = location_id.zfill(8)
    gage_rc = rc[rc["location_id"] == loc_padded].copy()
    if gage_rc.empty:
        return None
    gage_rc = gage_rc.sort_values("flow_cms").drop_duplicates("flow_cms")

    feat_recur = recur[recur["feature_id"] == feature_id]
    if feat_recur.empty:
        return None

    boundaries = {}
    for yr in RECURRENCE_YEARS:
        Q_r = float(feat_recur[f"Q_{yr}yr_cms"].iloc[0])
        if Q_r <= 0:
            continue

        # Interpolate the USGS rating curve to find the absolute elevation at Q_r.
        if Q_r < gage_rc["flow_cms"].min() or Q_r > gage_rc["flow_cms"].max():
            continue  # outside observed range — skip this recurrence
        elev_r = float(np.interp(Q_r, gage_rc["flow_cms"], gage_rc["elev_m"]))
        hand_r = elev_r - dem_adj_elevation_m

        if hand_r <= 0:
            log.debug("  gage %s yr=%d: HAND<0 (%.2fm) — skipping", location_id, yr, hand_r)
            continue

        boundaries[yr] = (hand_r, Q_r)

    return boundaries if boundaries else None


# ── Back-calculate n per zone ─────────────────────────────────────

def calibrate_n_zones(
    hydroid: int,
    src: pd.DataFrame,
    boundaries: dict[int, tuple[float, float]],
) -> dict[int, float]:
    """
    Back-calculate Manning's n for each zone boundary.

    At each recurrence stage h_r, solve:
        n_r = A(h_r) · R(h_r)^(2/3) · √S / Q_r

    Returns {recurrence_year: n_value} for successfully calibrated zones.
    """
    hdf = src[src["HydroID"] == hydroid].sort_values("Stage")
    if hdf.empty:
        return {}

    # Use the subdivided discharge column if subdiv already ran; else Manning raw.
    slope = hdf["SLOPE"].iloc[0]
    if slope <= 0:
        return {}

    n_zones: dict[int, float] = {}
    for yr, (h_r, Q_r) in sorted(boundaries.items()):
        # Find the SRC row whose stage is closest to h_r.
        idx = (hdf["Stage"] - h_r).abs().idxmin()
        row = hdf.loc[idx]

        A = float(row.get("WetArea (m2)", 0.0))
        R = float(row.get("HydraulicRadius (m)", 0.0))

        if A <= 0 or R <= 0 or Q_r <= 0:
            continue

        n_r = A * (R ** (2.0 / 3.0)) * (slope ** 0.5) / Q_r
        n_r = float(np.clip(n_r, N_MIN, N_MAX))
        n_zones[yr] = n_r
        log.debug("    HydroID %d  yr=%d  h=%.2fm  Q=%.1f cms  n=%.4f",
                  hydroid, yr, h_r, Q_r, n_r)

    return n_zones


# ── Apply piecewise n to a full SRC ──────────────────────────────

def apply_n_zones_to_src(
    src: pd.DataFrame,
    hydroid: int,
    boundaries: dict[int, tuple[float, float]],
    n_zones: dict[int, float],
) -> pd.DataFrame:
    """
    Recompute discharge for one HydroID using piecewise n.

    Zone lookup (sorted ascending by h_r):
      Stage <= h_2yr   → n_2yr
      h_2yr < stage <= h_5yr  → n_5yr
      ...
      stage > h_50yr   → n of the highest available zone
    """
    hdf = src[src["HydroID"] == hydroid].copy()
    if hdf.empty or not n_zones:
        return src

    slope = hdf["SLOPE"].iloc[0]
    # Sorted zone boundaries: [(h_2yr, n_2yr), (h_5yr, n_5yr), ...]
    sorted_zones = sorted(
        ((h, n_zones[yr]) for yr, (h, _) in boundaries.items() if yr in n_zones),
        key=lambda x: x[0],
    )
    if not sorted_zones:
        return src

    def _zone_n(stage_h: float) -> float:
        for h_boundary, n_val in sorted_zones:
            if stage_h <= h_boundary:
                return n_val
        return sorted_zones[-1][1]  # above all boundaries → highest zone n

    rows_mask = src["HydroID"] == hydroid

    new_q = []
    for _, row in hdf.iterrows():
        h = float(row["Stage"])
        if h == 0.0:
            new_q.append(0.0)
            continue
        A = float(row.get("WetArea (m2)", 0.0))
        R = float(row.get("HydraulicRadius (m)", 0.0))
        n = _zone_n(h)
        if A <= 0 or R <= 0 or slope <= 0:
            new_q.append(float(row.get("Discharge (m3s-1)", 0.0)))
        else:
            new_q.append(A * (R ** (2.0 / 3.0)) * (slope ** 0.5) / n)

    src.loc[rows_mask, "Discharge (m3s-1)"] = new_q
    # Record the zone-n for the lowest recurrence year on each row (informational).
    src.loc[rows_mask, "zonal_n_applied"] = [_zone_n(float(h))
                                              for h in hdf["Stage"]]
    return src


# ── Propagation helpers ───────────────────────────────────────────

def average_zones(
    all_boundaries: list[dict[int, tuple[float, float]]],
    all_n_zones: list[dict[int, float]],
) -> tuple[dict[int, tuple[float, float]], dict[int, float]]:
    """Average calibrated zone data across multiple gauges in a branch."""
    # Average the HAND boundary stages and n values independently.
    avg_boundaries: dict[int, list] = {}
    avg_n: dict[int, list] = {}
    for bounds, n_zones in zip(all_boundaries, all_n_zones):
        for yr, (h, _) in bounds.items():
            avg_boundaries.setdefault(yr, []).append(h)
        for yr, n in n_zones.items():
            avg_n.setdefault(yr, []).append(n)

    merged_bounds = {yr: (float(np.mean(hs)), 0.0) for yr, hs in avg_boundaries.items()}
    merged_n = {yr: float(np.mean(ns)) for yr, ns in avg_n.items()}
    return merged_bounds, merged_n


# ── Per-branch processing ─────────────────────────────────────────

def process_branch(
    bid: str,
    branch_dir: Path,
    elev: pd.DataFrame,
    rc: pd.DataFrame,
    recur: pd.DataFrame,
) -> None:
    """
    Calibrate n for a directly calibrated branch and write back to the SRC +
    hydroTable CSVs.

    The gauge's calibrated n is applied to every HydroID in the branch.
    If the branch somehow has multiple gauges with RC data, their zone n values
    are averaged before propagation.
    """
    src = load_src(branch_dir, bid)
    if src is None:
        log.warning("Branch %s: src_full_crosswalked not found — skipping", bid)
        return

    ht_path = branch_dir / f"hydroTable_{bid}.csv"
    if not ht_path.exists():
        log.warning("Branch %s: hydroTable not found — skipping", bid)
        return

    branch_elev = elev[elev["levpa_id"].astype(str) == str(bid)]
    log.info("Branch %s: %d gauge row(s)", bid, len(branch_elev))

    all_bounds_list = []
    all_n_list = []

    for _, gage in branch_elev.iterrows():
        loc_id  = str(gage["location_id"])
        hydroid = int(gage["HydroID"])
        feat_id = int(gage["feature_id"]) if pd.notna(gage["feature_id"]) else -1
        dem_adj = float(gage["dem_adj_elevation"])

        if feat_id < 0:
            log.warning("  Gauge %s: no feature_id — skipping", loc_id)
            continue

        bounds = compute_zone_boundaries(loc_id, feat_id, dem_adj, rc, recur)
        if bounds is None:
            log.warning("  Gauge %s: could not compute zone boundaries — skipping", loc_id)
            continue

        n_zones = calibrate_n_zones(hydroid, src, bounds)
        if not n_zones:
            log.warning("  Gauge %s HydroID %d: no valid n values — skipping", loc_id, hydroid)
            continue

        log.info("  Gauge %s HydroID %d: calibrated n = %s",
                 loc_id, hydroid,
                 {yr: f"{n:.4f}" for yr, n in sorted(n_zones.items())})

        src = apply_n_zones_to_src(src, hydroid, bounds, n_zones)
        all_bounds_list.append(bounds)
        all_n_list.append(n_zones)

    if not all_bounds_list:
        log.warning("Branch %s: no valid gauge calibrations — SRC not modified", bid)
        return

    # Single gauge: use directly.  Multiple gauges: average before propagation.
    if len(all_bounds_list) == 1:
        branch_bounds, branch_n = all_bounds_list[0], all_n_list[0]
    else:
        branch_bounds, branch_n = average_zones(all_bounds_list, all_n_list)

    # Apply branch n to all HydroIDs not already individually calibrated.
    calibrated_hids = {
        int(gage["HydroID"])
        for _, gage in branch_elev.iterrows()
        if pd.notna(gage["feature_id"])
    }
    for hid in src["HydroID"].unique():
        if hid not in calibrated_hids:
            src = apply_n_zones_to_src(src, hid, branch_bounds, branch_n)

    src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
    _backup(src_path)
    _safe_write_csv(src, src_path, bid)
    _sync_hydrotable(ht_path, src, bid)

    log.info("Branch %s: updated %d HydroIDs", bid, src["HydroID"].nunique())


def _backup(path: Path) -> None:
    """Copy to *.pre_n_calib.csv if backup doesn't already exist."""
    backup = path.with_suffix(".pre_n_calib.csv")
    if not backup.exists():
        shutil.copy2(path, backup)


def _safe_write_csv(df: pd.DataFrame, path: Path, bid: str) -> None:
    """Write CSV via a temp file to avoid Windows file-lock errors."""
    import os
    tmp = path.with_suffix(".tmp.csv")
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, path)   # atomic on same filesystem
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise PermissionError(
            f"Branch {bid}: cannot write {path.name} — close it in any open "
            f"application and re-run.\n  ({exc})"
        ) from exc


def _sync_hydrotable(ht_path: Path, src: pd.DataFrame, bid: str) -> None:
    """Push the updated discharge + zonal_n_applied from SRC into the hydroTable."""
    _backup(ht_path)
    ht = pd.read_csv(ht_path, low_memory=False)

    pull = src[["HydroID", "Stage", "Discharge (m3s-1)"]].copy()
    if "zonal_n_applied" in src.columns:
        pull["zonal_n_applied"] = src["zonal_n_applied"]
    pull = pull.rename(columns={"Stage": "stage", "Discharge (m3s-1)": "discharge_cms"})

    ht["HydroID"] = ht["HydroID"].astype(int)
    ht = ht.drop(columns=["discharge_cms", "zonal_n_applied"], errors="ignore")
    ht = ht.merge(pull, on=["HydroID", "stage"], how="left")
    ht["discharge_cms"] = ht["discharge_cms"].fillna(0.0)
    ht.to_csv(ht_path, index=False)


# ── Main ──────────────────────────────────────────────────────────

def _get_directly_calibrated_branches(elev: pd.DataFrame, rc: pd.DataFrame) -> set[str]:
    """
    Returns branch IDs that qualify for direct calibration:
      - levpa_id != "0"  (not the watershed trunk)
      - gauge has at least one entry in the USGS rating-curve parquet
    """
    rc_ids    = set(rc["location_id"].astype(str).unique())
    non_trunk = elev[elev["levpa_id"].astype(str) != "0"].copy()
    non_trunk["has_rc"] = non_trunk["location_id"].apply(
        lambda loc: str(loc).zfill(8) in rc_ids
    )
    return set(non_trunk[non_trunk["has_rc"]]["levpa_id"].astype(str).unique())


def main():
    log.info("=== n-Recurrence Calibration — HUC8 %s ===", HUC8)

    elev, rc, recur = load_inputs()

    directly_calibrated = _get_directly_calibrated_branches(elev, rc)
    if not directly_calibrated:
        log.warning("No directly calibrated branches found — nothing to do.")
        return
    log.info("Directly calibrated branches: %s", sorted(directly_calibrated))

    branch_dirs = sorted(d for d in BRANCHES.iterdir() if d.is_dir())
    for bdir in branch_dirs:
        bid = bdir.name
        if bid not in directly_calibrated:
            log.debug("Branch %s: not directly calibrated — skip", bid)
            continue
        process_branch(bid, bdir, elev, rc, recur)

    log.info("=== n-Recurrence Calibration complete ===")
    log.info("Calibrated branches: %s", sorted(directly_calibrated))
    log.info("Original SRCs backed up as *.pre_n_calib.csv")


if __name__ == "__main__":
    main()
