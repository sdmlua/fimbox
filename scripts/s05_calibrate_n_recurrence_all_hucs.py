"""
Manning's n calibration for all HUC8s using USGS rating curves at NWM recurrence stages.

Concept
-------
For each gauged branch, NWM recurrence flows (2, 5, 10, 25, 50 yr) are interpolated
through the USGS rating curve to HAND stage.  Those 5 stage boundaries define 6 depth
zones.  Manning's n is back-calculated at each boundary:

    n_r = A(h_r) · R(h_r)^(2/3) · √S / Q_r

The calibrated n is then applied piecewise, recomputing discharge at every SRC stage row.
All HydroIDs in a calibrated branch receive the same piecewise n structure.

Scope
-----
Only non-trunk branches that have at least one USGS gauge with rating-curve data are
processed.  Branch 0 and ungauged branches are left unchanged.

Prerequisite: s04_usgs_gage_crosswalk_all_hucs.py must have completed for each HUC
(usgs_elev_table.csv must exist at the AOI root).

Run:
    .venv\\Scripts\\python.exe scripts/s05_calibrate_n_recurrence_all_hucs.py
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH   = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
OUT_DIR      = Path("E:/SI/out")
DATA_DIR     = Path("data")
TASK_LOG     = Path("E:/SI/out/ncalib_status.txt")

N_MIN = 0.010
N_MAX = 0.300
RECURRENCE_YEARS = [2, 5, 10, 25, 50]
RECUR_COLS = {yr: f"{yr}_0_year_recurrence_flow_17C" for yr in RECURRENCE_YEARS}
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ── Progress tracking ─────────────────────────────────────────────

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
        f.write(f"ncalib {huc8} {status}{(' ' + note) if note else ''}\n")


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ── Zone boundary calculation ─────────────────────────────────────

def compute_zone_boundaries(
    location_id: str,
    feature_id: int,
    dem_adj_m: float,
    rc: pd.DataFrame,
    recur: pd.DataFrame,
) -> dict[int, tuple[float, float]] | None:
    loc_padded = str(location_id).zfill(8)
    gage_rc = rc[rc["location_id"] == loc_padded].copy()
    if gage_rc.empty:
        return None
    gage_rc = gage_rc.sort_values("flow_cms").drop_duplicates("flow_cms")

    feat_recur = recur[recur["feature_id"] == feature_id]
    if feat_recur.empty:
        return None

    boundaries: dict[int, tuple[float, float]] = {}
    for yr in RECURRENCE_YEARS:
        Q_r = float(feat_recur[f"Q_{yr}yr_cms"].iloc[0])
        if Q_r <= 0:
            continue
        if Q_r < gage_rc["flow_cms"].min() or Q_r > gage_rc["flow_cms"].max():
            continue
        elev_r = float(np.interp(Q_r, gage_rc["flow_cms"].values, gage_rc["elev_m"].values))
        hand_r = elev_r - dem_adj_m
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
    hdf = src[src["HydroID"] == hydroid].sort_values("Stage")
    if hdf.empty:
        return {}
    slope = float(hdf["SLOPE"].iloc[0])
    if slope <= 0:
        return {}

    n_zones: dict[int, float] = {}
    for yr, (h_r, Q_r) in sorted(boundaries.items()):
        idx = (hdf["Stage"] - h_r).abs().idxmin()
        row = hdf.loc[idx]
        A = float(row.get("WetArea (m2)", 0.0))
        R = float(row.get("HydraulicRadius (m)", 0.0))
        if A <= 0 or R <= 0 or Q_r <= 0:
            continue
        n_r = A * (R ** (2.0 / 3.0)) * (slope ** 0.5) / Q_r
        n_zones[yr] = float(np.clip(n_r, N_MIN, N_MAX))
        log.debug("    HydroID %d  yr=%d  h=%.2fm  Q=%.1f cms  n=%.4f",
                  hydroid, yr, h_r, Q_r, n_zones[yr])

    return n_zones


# ── Apply piecewise n to a full SRC ──────────────────────────────

def apply_n_zones_to_src(
    src: pd.DataFrame,
    hydroid: int,
    boundaries: dict[int, tuple[float, float]],
    n_zones: dict[int, float],
) -> pd.DataFrame:
    hdf = src[src["HydroID"] == hydroid].copy()
    if hdf.empty or not n_zones:
        return src
    slope = float(hdf["SLOPE"].iloc[0])

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
        return sorted_zones[-1][1]

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
    src.loc[rows_mask, "zonal_n_applied"] = [_zone_n(float(h)) for h in hdf["Stage"]]
    return src


# ── Multi-gauge averaging ─────────────────────────────────────────

def average_zones(
    all_boundaries: list[dict[int, tuple[float, float]]],
    all_n_zones: list[dict[int, float]],
) -> tuple[dict[int, tuple[float, float]], dict[int, float]]:
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


# ── File I/O helpers ──────────────────────────────────────────────

def _backup(path: Path) -> None:
    backup = path.with_suffix(".pre_n_calib.csv")
    if not backup.exists():
        shutil.copy2(path, backup)


def _safe_write_csv(df: pd.DataFrame, path: Path, bid: str) -> None:
    tmp = path.with_suffix(".tmp.csv")
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, path)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise PermissionError(
            f"Branch {bid}: cannot write {path.name} — close it in any open application."
        ) from exc


def _sync_hydrotable(ht_path: Path, src: pd.DataFrame, bid: str) -> None:
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


# ── Per-branch processing ─────────────────────────────────────────

def process_branch(
    bid: str,
    branch_dir: Path,
    elev: pd.DataFrame,
    rc: pd.DataFrame,
    recur: pd.DataFrame,
) -> None:
    src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
    ht_path  = branch_dir / f"hydroTable_{bid}.csv"

    if not src_path.exists():
        log.warning("  Branch %s: SRC not found — skip", bid)
        return
    if not ht_path.exists():
        log.warning("  Branch %s: hydroTable not found — skip", bid)
        return

    src = pd.read_csv(src_path, dtype={"feature_id": str})
    src["HydroID"] = src["HydroID"].astype(int)

    branch_elev = elev[elev["levpa_id"].astype(str) == str(bid)]
    log.info("  Branch %s: %d gauge row(s)", bid, len(branch_elev))

    all_bounds_list: list[dict] = []
    all_n_list: list[dict] = []
    calibrated_hids: set[int] = set()

    for _, gage in branch_elev.iterrows():
        loc_id  = str(gage["location_id"])
        hydroid = int(gage["HydroID"])
        feat_id = int(gage["feature_id"]) if pd.notna(gage["feature_id"]) else -1
        dem_adj = float(gage["dem_adj_elevation"])

        if feat_id < 0:
            log.warning("  Gauge %s: no feature_id — skip", loc_id)
            continue

        bounds = compute_zone_boundaries(loc_id, feat_id, dem_adj, rc, recur)
        if bounds is None:
            log.warning("  Gauge %s: no zone boundaries — skip", loc_id)
            continue

        n_zones = calibrate_n_zones(hydroid, src, bounds)
        if not n_zones:
            log.warning("  Gauge %s HydroID %d: no valid n — skip", loc_id, hydroid)
            continue

        log.info("  Gauge %s HydroID %d: n = %s", loc_id, hydroid,
                 {yr: f"{n:.4f}" for yr, n in sorted(n_zones.items())})

        src = apply_n_zones_to_src(src, hydroid, bounds, n_zones)
        all_bounds_list.append(bounds)
        all_n_list.append(n_zones)
        calibrated_hids.add(hydroid)

    if not all_bounds_list:
        log.warning("  Branch %s: no valid gauge calibrations — SRC not modified", bid)
        return

    branch_bounds, branch_n = (
        (all_bounds_list[0], all_n_list[0])
        if len(all_bounds_list) == 1
        else average_zones(all_bounds_list, all_n_list)
    )

    for hid in src["HydroID"].unique():
        if hid not in calibrated_hids:
            src = apply_n_zones_to_src(src, hid, branch_bounds, branch_n)

    _backup(src_path)
    _safe_write_csv(src, src_path, bid)
    _sync_hydrotable(ht_path, src, bid)
    log.info("  Branch %s: updated %d HydroIDs", bid, src["HydroID"].nunique())


# ── Per-HUC entry point ───────────────────────────────────────────

def run_huc(huc8: str, rc: pd.DataFrame, recur: pd.DataFrame) -> None:
    aoi_root = OUT_DIR / f"HUC{huc8}"
    branches = aoi_root / "watershed-data" / "branches"

    elev_path = aoi_root / "usgs_elev_table.csv"
    if not elev_path.exists():
        raise FileNotFoundError(
            f"usgs_elev_table.csv not found — run s04 crosswalk first: {elev_path}"
        )

    elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
    log.info("  usgs_elev_table: %d rows", len(elev))

    rc_ids    = set(rc["location_id"].astype(str))
    non_trunk = elev[elev["levpa_id"].astype(str) != "0"].copy()
    non_trunk["has_rc"] = non_trunk["location_id"].apply(
        lambda loc: str(loc).zfill(8) in rc_ids
    )
    directly_calibrated = set(non_trunk[non_trunk["has_rc"]]["levpa_id"].astype(str))

    if not directly_calibrated:
        log.info("  No directly calibrated branches for HUC %s — nothing to do", huc8)
        return
    log.info("  Calibrated branches: %s", sorted(directly_calibrated))

    for branch_dir in sorted(d for d in branches.iterdir() if d.is_dir()):
        bid = branch_dir.name
        if bid in directly_calibrated:
            process_branch(bid, branch_dir, elev, rc, recur)


# ── Main ──────────────────────────────────────────────────────────

def main():
    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    done = _load_done()
    remaining = [h for h in hucs if h not in done]

    log.info(f"n-Calibration: {len(hucs)} total | {len(done)} already done | {len(remaining)} to run")
    log.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Load shared tables once — identical for all HUCs
    rc = pd.read_parquet(DATA_DIR / "usgs_rating_curves.parquet")
    rc["location_id"] = rc["location_id"].astype(str)
    rc["flow_cms"]    = rc["flow"].astype(float) / 35.3147
    rc["elev_m"]      = rc["elevation_navd88"].astype(float) / 3.28084
    log.info(f"USGS rating curves: {len(rc)} rows, {rc['location_id'].nunique()} gauges")

    recur = pd.read_parquet(DATA_DIR / "nwm3_17C_recurrence_flows_cfs.parquet")
    for yr, col in RECUR_COLS.items():
        recur[f"Q_{yr}yr_cms"] = recur[col].astype(float) * 0.028317
    recur["feature_id"] = recur["feature_id"].astype("Int64")
    log.info(f"NWM recurrence flows: {len(recur)} feature_ids")

    batch_start = time.time()
    huc_times: list[tuple[str, float, str]] = []
    passed, failed = list(done), []

    for i, huc8 in enumerate(remaining, 1):
        huc_start = time.time()
        log.info(f"  [{i}/{len(remaining)}] HUC8 = {huc8}  |  "
                 f"batch elapsed: {_fmt(huc_start - batch_start)}")
        try:
            run_huc(huc8, rc, recur)
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
    log.info(f"n-Calibration complete  |  total time: {_fmt(total)}")
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
