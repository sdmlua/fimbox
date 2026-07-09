"""
Shared engine for zone-based Manning's n calibration — hydroTable edition.

The hydroTable is the single source of truth: geometry (WetArea, HydraulicRadius,
TopWidth, SLOPE) is READ from hydroTable_{bid}.csv and calibrated discharge +
zonal_n_applied are WRITTEN back to it.  src_full_crosswalked files are restored
to baseline and no longer modified — the hydroTable is what FIM generation
consumes, so calibration lives where the runtime looks.

Four starting-point matching methods share this engine (see s05a–s05d):

  A   wedge_topwidth   rectangular bathymetry wedge below HAND 0; depth δ from
                       the PZF power-law fit, width w0 from SRC TopWidth at the
                       lowest wet stages
  B1  datum_shift      no geometry change; the SRC is re-referenced to the RC
                       origin (anchors and applied discharge evaluated at h + δ)
  B2  q0_offset        no geometry change; observed baseflow Q0 added to the
                       SRC discharge (Garousi-Nejad et al. 2019)
  B3  wedge_continuity wedge with depth δ from PZF and width solved from
                       hydraulic continuity so the excavated channel carries
                       exactly the observed baseflow:
                           w0 = Q0·n_ch / (δ^(5/3)·√S),   n_ch = 0.05
                       capped to [1 m, TopWidth estimate]

All methods share identical downstream machinery: n back-calculated at NWM
recurrence anchors on smoothed conveyance, applied piecewise-constant per zone
with ±1-row linear transitions, and a final cumulative-max monotone guarantee.

Each method has its own status file (ncalib_status_{method}.txt); the per-gauge
record goes to <AOI>/ncalib_diagnostics.csv with a `method` column.  Methods
are mutually exclusive on disk — every run restores the hydroTable baseline
first, so the diagnostics always describe the calibration currently applied.

Run a method via its wrapper:
    .venv\\Scripts\\python.exe scripts/s05d_calibrate_B3_wedge_continuity.py
Justify the choice of method with:
    .venv\\Scripts\\python.exe scripts/s05e_evaluate_methods.py
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
DATA_DIR     = Path(__file__).resolve().parents[1] / "data"   # repo-root data/, CWD-independent

N_MIN = 0.010
N_MAX = 0.300
N_CH_CONTINUITY = 0.05        # assumed in-channel n for the continuity wedge width
RECURRENCE_YEARS = [2, 5, 10, 25, 50]
RECUR_COLS = {yr: f"{yr}_0_year_recurrence_flow_17C" for yr in RECURRENCE_YEARS}

TRANS_W = 0.3048              # zone-boundary transition half-width (m; one row)

# PZF power-law fit QC thresholds
PZF_MIN_PTS   = 6
PZF_MAX_PTS   = 12
PZF_B_RANGE   = (1.0, 5.0)
PZF_MIN_R2    = 0.95
PZF_DELTA_RNG = (0.05, 15.0)

METHOD_LABELS = {
    "A":  "wedge_topwidth",
    "B1": "datum_shift",
    "B2": "q0_offset",
    "B3": "wedge_continuity",
}
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# Shared data loading
# ══════════════════════════════════════════════════════════════════

def load_shared() -> tuple[pd.DataFrame, pd.DataFrame]:
    rc = pd.read_parquet(DATA_DIR / "usgs_rating_curves.parquet")
    rc["location_id"] = rc["location_id"].astype(str)
    rc["flow_cms"]    = rc["flow"].astype(float) / 35.3147
    rc["elev_m"]      = rc["elevation_navd88"].astype(float) / 3.28084
    recur = pd.read_parquet(DATA_DIR / "nwm3_17C_recurrence_flows_cfs.parquet")
    for yr, col in RECUR_COLS.items():
        recur[f"Q_{yr}yr_cms"] = recur[col].astype(float) * 0.028317
    recur["feature_id"] = recur["feature_id"].astype("Int64")
    return rc, recur


def study_hucs() -> list[str]:
    df = pd.read_excel(EXCEL_PATH)
    return [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]


# ══════════════════════════════════════════════════════════════════
# Starting-point observations: PZF fit and baseflow
# ══════════════════════════════════════════════════════════════════

def fit_pzf(gage_rc: pd.DataFrame, dem_adj_m: float) -> dict | None:
    """
    Fit  Q = C·(elev − h0)^b  to the lowest RC points via grid search on h0.
    Returns datum gap δ = dem_adj − h0 and fit stats, or None on QC failure.
    """
    g = gage_rc.sort_values("elev_m").drop_duplicates("elev_m")
    q = g["flow_cms"].to_numpy(float)
    e = g["elev_m"].to_numpy(float)
    pos = q > 0
    q, e = q[pos], e[pos]
    if len(q) < PZF_MIN_PTS:
        return None

    k = min(PZF_MAX_PTS, max(PZF_MIN_PTS, len(q) // 4))
    q_lo, e_lo = q[:k], e[:k]
    e_min = float(e_lo[0])

    best = None  # (r2, h0, b)
    for h0 in np.linspace(e_min - 8.0, e_min - 0.02, 200):
        x = np.log(e_lo - h0)
        y = np.log(q_lo)
        b, a = np.polyfit(x, y, 1)
        if not (PZF_B_RANGE[0] <= b <= PZF_B_RANGE[1]):
            continue
        yhat = a + b * x
        ss_res = float(((y - yhat) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        if best is None or r2 > best[0]:
            best = (r2, float(h0), float(b))

    if best is None or best[0] < PZF_MIN_R2:
        return None
    r2, h0, b = best
    delta = dem_adj_m - h0
    if not (PZF_DELTA_RNG[0] <= delta <= PZF_DELTA_RNG[1]):
        return None
    return {"h0_elev_m": h0, "delta_m": delta, "b": b, "r2": r2}


def baseflow_q0(gage_rc: pd.DataFrame, dem_adj_m: float) -> float:
    """Observed flow at the DEM water-surface elevation."""
    g = gage_rc.sort_values("elev_m").drop_duplicates("elev_m")
    q = g["flow_cms"].to_numpy(float)
    e = g["elev_m"].to_numpy(float)
    if dem_adj_m <= e[0]:
        return float(q[0])
    return float(np.interp(dem_adj_m, e, q))


# ══════════════════════════════════════════════════════════════════
# Zone boundaries (anchors)
# ══════════════════════════════════════════════════════════════════

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
            continue
        boundaries[yr] = (hand_r, Q_r)

    return boundaries if boundaries else None


# ══════════════════════════════════════════════════════════════════
# Geometry (hydroTable columns) + method parameterization
# ══════════════════════════════════════════════════════════════════

def build_geometry(hdf: pd.DataFrame, delta: float, w0: float
                   ) -> tuple[np.ndarray, np.ndarray]:
    """
    (stages, K_smooth) for one HydroID from hydroTable rows, stage-sorted.
    Wedge (delta, w0) added below HAND 0 when both > 0:
        A* = A + w0·δ,   WP* = WP + 2δ   (WP recovered as A/R)
    K = A*·R*^(2/3), then rolling-median(3) + isotonic non-decreasing —
    within-zone wiggles are raster artifacts, not hydraulics.
    """
    stages = hdf["stage"].to_numpy(float)
    A = hdf["WetArea (m2)"].to_numpy(float)
    R = hdf["HydraulicRadius (m)"].to_numpy(float)

    with np.errstate(divide="ignore", invalid="ignore"):
        WP = np.where(R > 0, A / R, 0.0)

    if delta > 0 and w0 > 0:
        A_adj  = A + w0 * delta
        WP_adj = np.where(WP > 0, WP + 2.0 * delta, w0 + 2.0 * delta)
        R_adj  = np.where(WP_adj > 0, A_adj / WP_adj, 0.0)
    else:
        A_adj, R_adj = A, R

    K = A_adj * np.power(R_adj, 2.0 / 3.0, where=R_adj > 0,
                         out=np.zeros_like(R_adj))
    K = pd.Series(K).rolling(3, center=True, min_periods=1).median().to_numpy()
    K = np.maximum.accumulate(K)
    return stages, K


def wedge_width_topwidth(hdf: pd.DataFrame) -> float:
    """Method-A width: median hydroTable TopWidth at the lowest wet stages."""
    if "TopWidth (m)" in hdf.columns:
        tw = hdf.loc[hdf["stage"] > 0, "TopWidth (m)"].to_numpy(float)
        tw = tw[tw > 0]
        if len(tw):
            return float(np.median(tw[:3]))
    wet = hdf[(hdf["stage"] > 0) & (hdf["WetArea (m2)"] > 0)]
    if not wet.empty:
        r = wet.iloc[0]
        return float(r["WetArea (m2)"] / r["stage"])
    return 0.0


def wedge_width_continuity(Q0: float, delta: float, slope: float,
                           w0_cap: float) -> float:
    """
    Method-B3 width: size the wedge so the excavated rectangular channel
    carries the observed baseflow at depth δ (wide-channel Manning):
        Q0 = (1/n_ch)·(w0·δ)·δ^(2/3)·√S   →   w0 = Q0·n_ch / (δ^(5/3)·√S)
    Capped to [1 m, TopWidth estimate] — the raster width is an upper bound.
    """
    if Q0 <= 0 or delta <= 0 or slope <= 0:
        return 0.0
    w0 = Q0 * N_CH_CONTINUITY / (delta ** (5.0 / 3.0) * slope ** 0.5)
    return float(np.clip(w0, 1.0, max(w0_cap, 1.0)))


def method_params(method: str, hdf: pd.DataFrame, slope: float,
                  pzf: dict | None, Q0: float) -> dict:
    """
    Resolve a method tag to concrete correction parameters.
    Returns dict(mode, delta, w0, q_offset, shift).
    Fallback for every method when its inputs are unavailable: q0_offset.
    """
    delta = pzf["delta_m"] if pzf else 0.0
    w0_tw = wedge_width_topwidth(hdf)

    if method == "A" and pzf and w0_tw > 0:
        return dict(mode="wedge_topwidth", delta=delta, w0=w0_tw,
                    q_offset=0.0, shift=0.0)
    if method == "B1" and pzf:
        return dict(mode="datum_shift", delta=delta, w0=0.0,
                    q_offset=0.0, shift=delta)
    if method == "B3" and pzf:
        w0c = wedge_width_continuity(Q0, delta, slope, w0_tw)
        if w0c > 0:
            return dict(mode="wedge_continuity", delta=delta, w0=w0c,
                        q_offset=0.0, shift=0.0)
    # B2 and all fallbacks
    return dict(mode="q0_offset", delta=0.0, w0=0.0, q_offset=Q0, shift=0.0)


# ══════════════════════════════════════════════════════════════════
# Calibration math (shared by all methods)
# ══════════════════════════════════════════════════════════════════

def calibrate_n_zones(stages: np.ndarray, K: np.ndarray, slope: float,
                      boundaries: dict[int, tuple[float, float]],
                      q_offset: float, shift: float
                      ) -> tuple[dict[int, float], int, int]:
    """
    n_r = K(h_r + shift)·√S / (Q_r − q_offset), K interpolated to the exact
    anchor stage.  Returns (n per year, clipped_low, clipped_high).
    """
    n_zones: dict[int, float] = {}
    clip_lo = clip_hi = 0
    for yr, (h_r, Q_r) in sorted(boundaries.items()):
        Q_eff = Q_r - q_offset
        if Q_eff <= 0:
            continue
        K_r = float(np.interp(h_r + shift, stages, K))
        if K_r <= 0 or slope <= 0:
            continue
        n_raw = K_r * (slope ** 0.5) / Q_eff
        n_val = float(np.clip(n_raw, N_MIN, N_MAX))
        clip_lo += int(n_raw < N_MIN)
        clip_hi += int(n_raw > N_MAX)
        n_zones[yr] = n_val
    return n_zones, clip_lo, clip_hi


# ══════════════════════════════════════════════════════════════════
# INCREMENTAL (slice / divided-channel) scheme — alternative to the
# whole-section scheme above.  Each depth band keeps its own n and
# discharge is the sum of slice contributions (Lotter composite):
#
#     Q(h) = q_offset + √S · Σ_i [K(min(h,h_i)) − K(h_{i-1})] / n_i
#
# Rising water only ADDS conveyance with the new zone's n — the lower
# flow is never re-rated — so Q(h) is continuous and monotone BY
# CONSTRUCTION: no transition band, no cumulative-max guard needed.
# n_i here means "roughness of the flow added between anchors i-1 and
# i" (≈ true floodplain roughness for overbank zones), not the
# effective whole-section value.
# ══════════════════════════════════════════════════════════════════

def calibrate_n_zones_incremental(
    stages: np.ndarray, K: np.ndarray, slope: float,
    boundaries: dict[int, tuple[float, float]], q_offset: float,
) -> tuple[dict[int, float], int, int]:
    """
    Sequential back-calculation on conveyance/discharge INCREMENTS:
        n_i = √S · (K_i − K_{i-1}) / (Q_i − Q_achieved_{i-1})
    Uses the ACHIEVED cumulative discharge, so a clipped zone is
    compensated by the zones above it.  Returns (n per year, clip_lo, clip_hi).
    """
    n_zones: dict[int, float] = {}
    clip_lo = clip_hi = 0
    if slope <= 0:
        return n_zones, 0, 0
    rootS = slope ** 0.5
    prev_K = 0.0
    q_achieved = q_offset
    for yr, (h_r, Q_r) in sorted(boundaries.items(), key=lambda kv: kv[1][0]):
        K_r = float(np.interp(h_r, stages, K))
        dK = K_r - prev_K
        dQ = Q_r - q_achieved
        if dK <= 0 or dQ <= 0:
            continue                    # unusable increment — skip this anchor
        n_raw = dK * rootS / dQ
        n_val = float(np.clip(n_raw, N_MIN, N_MAX))
        clip_lo += int(n_raw < N_MIN)
        clip_hi += int(n_raw > N_MAX)
        n_zones[yr] = n_val
        q_achieved += dK * rootS / n_val
        prev_K = K_r
    return n_zones, clip_lo, clip_hi


def discharge_incremental(
    stages: np.ndarray, K: np.ndarray, slope: float,
    boundaries: dict[int, tuple[float, float]],
    n_zones: dict[int, float], q_offset: float,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Evaluate the slice-composite discharge on the stage grid.
    Returns (q, n_applied) — n_applied is the zone n governing each row's
    slice (constant per zone, no transitions).
    """
    items = sorted(((h, n_zones[yr]) for yr, (h, _) in boundaries.items()
                    if yr in n_zones), key=lambda x: x[0])
    rootS = slope ** 0.5
    if not items:
        return np.zeros_like(stages), np.full_like(stages, np.nan)
    h_b = np.array([h for h, _ in items])
    n_b = np.array([n for _, n in items])
    K_b = np.interp(h_b, stages, K)

    # cumulative √S-scaled discharge at each slice top
    dK = np.diff(np.concatenate([[0.0], K_b]))
    q_at_tops = q_offset + rootS * np.cumsum(dK / n_b)

    idx = np.searchsorted(h_b, stages)            # slice index per row
    idx_c = np.minimum(idx, len(items) - 1)       # rows above last anchor use n_last
    K_base = np.where(idx == 0, 0.0, K_b[np.maximum(idx - 1, 0)])
    q_base = np.where(idx == 0, q_offset, q_at_tops[np.maximum(idx - 1, 0)])
    q = q_base + rootS * (K - K_base) / n_b[idx_c]
    return q, n_b[idx_c]


def n_profile(stages: np.ndarray, boundaries: dict[int, tuple[float, float]],
              n_zones: dict[int, float], shift: float = 0.0) -> np.ndarray:
    """
    n(h): constant within each zone, linear across ±TRANS_W at interior
    boundaries (bands clamped so they never overlap).  n_i applies to stages
    ≤ h_i; above the last anchor n stays at n_last.  `shift` moves the
    boundaries for the datum-shift method.
    """
    items = sorted((h + shift, n_zones[yr])
                   for yr, (h, _) in boundaries.items() if yr in n_zones)
    h_b = [h for h, _ in items]
    n_b = [n for _, n in items]
    m = len(items)
    if m == 0:
        return np.full_like(stages, np.nan)
    if m == 1:
        return np.full_like(stages, n_b[0])

    xp: list[float] = []
    fp: list[float] = []
    for i in range(m - 1):
        gap_lo = h_b[i] - (h_b[i - 1] if i > 0 else 0.0)
        gap_hi = h_b[i + 1] - h_b[i]
        w = max(min(TRANS_W, 0.4 * gap_lo, 0.4 * gap_hi), 1e-6)
        xp += [h_b[i] - w, h_b[i] + w]
        fp += [n_b[i], n_b[i + 1]]
    return np.interp(stages, xp, fp)


def _total_scheme_curve(stages: np.ndarray, K: np.ndarray, slope: float,
                        boundaries: dict[int, tuple[float, float]],
                        n_zones_total: dict[int, float], q_offset: float
                        ) -> tuple[np.ndarray, np.ndarray, int]:
    """
    Independently evaluate the TOTAL (whole-section) scheme's own curve:
    n(h) constant per zone with ±TRANS_W transitions (n_profile), applied to
    the WHOLE cross-section, then a cumulative-max monotone guard — the exact
    method validated against "incremental" in s05e/s05f_evaluate_schemes.py.
    This is a genuinely separate curve from the applied (incremental) one,
    not a back-derivation from it.  Returns (q_mono, n_arr, rows_fixed).
    """
    n_arr = n_profile(stages, boundaries, n_zones_total)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = np.where(n_arr > 0, K * (slope ** 0.5) / n_arr, 0.0)
    q = q + q_offset
    q_mono = np.maximum.accumulate(q)
    fixed = int((q_mono > q + 1e-9).sum())
    return q_mono, n_arr, fixed


def apply_to_hydroid(ht: pd.DataFrame, hydroid: int,
                     boundaries: dict[int, tuple[float, float]],
                     n_zones: dict[int, float], params: dict,
                     scheme: str = "incremental",
                     n_zones_total: dict[int, float] | None = None,
                     ) -> tuple[pd.DataFrame, int]:
    """
    Recompute discharge_cms for one HydroID under the chosen (applied) scheme,
    AND independently compute the total-scheme curve for comparison/reporting.

      incremental  Q(h) = q_offset + √S·Σ slice contributions  (continuous +
                   monotone by construction; n_zones are SLICE roughness) —
                   this is what's WRITTEN to discharge_cms (what FIM consumes).
      total        Q(h) = K(h)·√S / n(h) + q_offset with boundary transitions
                   + cumulative-max guard — a SEPARATE, independently
                   calibrated curve (n_zones_total), stored alongside for
                   comparison, never used by FIM generation.

    Columns written:
      discharge_cms             the APPLIED scheme's curve (drives FIM)
      zonal_n_applied            the applied scheme's n per row (slice n)
      discharge_cms_wholesection  the total scheme's OWN curve (diagnostic only)
      whole_section_n            the total scheme's OWN n per row (matches
                                 n_eff_{yr}yr in ncalib_diagnostics.csv)

    Returns (ht, rows_fixed_by_monotone_guard) for the APPLIED scheme.
    """
    mask = ht["HydroID"] == hydroid
    hdf = ht[mask].sort_values("stage")
    slope = float(hdf["SLOPE"].iloc[0])
    if slope <= 0 or not n_zones:
        return ht, 0

    stages, K = build_geometry(hdf, params["delta"], params["w0"])
    qoff = params["q_offset"]

    if scheme == "incremental":
        q, n_arr = discharge_incremental(stages, K, slope, boundaries,
                                         n_zones, qoff)
    else:
        shift = params["shift"]
        stages_eval = stages + shift
        K_eval = np.interp(stages_eval, stages, K)      # flat beyond top row
        n_arr  = n_profile(stages_eval, boundaries, n_zones, shift=shift)
        with np.errstate(divide="ignore", invalid="ignore"):
            q = np.where(n_arr > 0, K_eval * (slope ** 0.5) / n_arr, 0.0)
        q = q + qoff

    q_mono = np.maximum.accumulate(q)
    fixed = int((q_mono > q + 1e-9).sum())

    order = hdf.index.to_numpy()
    ht.loc[order, "discharge_cms"]   = q_mono
    ht.loc[order, "zonal_n_applied"] = n_arr

    # Independent total-scheme curve — a genuine second curve, not a
    # relabeling of the applied one.  Skipped only if the applied scheme
    # already IS total (nothing separate to compute) or n_zones_total is
    # unavailable (e.g. total-scheme back-calc found no valid anchors).
    if scheme != "total" and n_zones_total:
        q_tot, n_tot_arr, _ = _total_scheme_curve(
            stages, K, slope, boundaries, n_zones_total, qoff)
        ht.loc[order, "discharge_cms_wholesection"] = q_tot
        ht.loc[order, "whole_section_n"] = n_tot_arr
    elif scheme == "total":
        ht.loc[order, "discharge_cms_wholesection"] = q_mono
        ht.loc[order, "whole_section_n"] = n_arr

    return ht, fixed


# ══════════════════════════════════════════════════════════════════
# File I/O — hydroTable is the single write target
# ══════════════════════════════════════════════════════════════════

def _backup_or_restore(path: Path) -> None:
    """First run: create .pre_n_calib backup.  Rerun: restore from it."""
    backup = path.with_suffix(".pre_n_calib.csv")
    if backup.exists():
        shutil.copy2(backup, path)
    else:
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


# ══════════════════════════════════════════════════════════════════
# Per-branch / per-HUC / batch drivers
# ══════════════════════════════════════════════════════════════════

def process_branch(method: str, bid: str, branch_dir: Path,
                   elev: pd.DataFrame, rc: pd.DataFrame, recur: pd.DataFrame,
                   diagnostics: list[dict], huc8: str,
                   scheme: str = "incremental") -> None:
    ht_path  = branch_dir / f"hydroTable_{bid}.csv"
    src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
    if not ht_path.exists():
        log.warning("  Branch %s: hydroTable not found — skip", bid)
        return

    # Restore baselines (idempotent).  src_full is restored so it stays a
    # clean baseline artifact, but it is never written again.
    _backup_or_restore(ht_path)
    if src_path.exists():
        _backup_or_restore(src_path)

    ht = pd.read_csv(ht_path, low_memory=False)
    ht["HydroID"] = ht["HydroID"].astype(int)

    branch_elev = elev[elev["levpa_id"].astype(str) == str(bid)]
    log.info("  Branch %s: %d gauge row(s)", bid, len(branch_elev))

    seen_hids: set[int] = set()
    n_calibrated = 0

    for _, gage in branch_elev.iterrows():
        loc_id  = str(gage["location_id"])
        hydroid = int(gage["HydroID"])
        feat_id = int(gage["feature_id"]) if pd.notna(gage["feature_id"]) else -1
        dem_adj = float(gage["dem_adj_elevation"])

        diag = {
            "method": method, "scheme": scheme, "huc8": huc8, "branch": bid,
            "hydroid": hydroid,
            "location_id": loc_id.zfill(8), "feature_id": feat_id,
            "mode": "none", "delta_m": np.nan, "h0_elev_m": np.nan,
            "pzf_b": np.nan, "pzf_r2": np.nan, "w0_m": np.nan,
            "q0_cms": np.nan, "n_clipped_low": 0, "n_clipped_high": 0,
            "monotone_fixed_rows": 0, "status": "",
        }
        for yr in RECURRENCE_YEARS:
            diag[f"n_{yr}yr"] = np.nan       # applied scheme's n (slice n when incremental)
            diag[f"n_eff_{yr}yr"] = np.nan   # effective whole-section n (compatibility)

        if feat_id < 0:
            diag["status"] = "no_feature_id"; diagnostics.append(diag); continue
        if hydroid in seen_hids:
            diag["status"] = "hydroid_already_calibrated"; diagnostics.append(diag)
            continue

        gage_rc = rc[rc["location_id"] == loc_id.zfill(8)]
        if gage_rc.empty:
            diag["status"] = "no_rating_curve"; diagnostics.append(diag); continue

        bounds = compute_zone_boundaries(loc_id, feat_id, dem_adj, rc, recur)
        if bounds is None:
            diag["status"] = "no_zone_boundaries"; diagnostics.append(diag); continue

        hdf = ht[ht["HydroID"] == hydroid].sort_values("stage")
        if hdf.empty:
            diag["status"] = "hydroid_not_in_hydrotable"; diagnostics.append(diag)
            continue
        slope = float(hdf["SLOPE"].iloc[0])

        pzf = fit_pzf(gage_rc, dem_adj)
        Q0  = baseflow_q0(gage_rc, dem_adj)
        params = method_params(method, hdf, slope, pzf, Q0)
        diag.update(mode=params["mode"], delta_m=params["delta"],
                    w0_m=params["w0"], q0_cms=params["q_offset"])
        if pzf and params["mode"] != "q0_offset":
            diag.update(h0_elev_m=pzf["h0_elev_m"], pzf_b=pzf["b"],
                        pzf_r2=pzf["r2"])

        stages, K = build_geometry(hdf, params["delta"], params["w0"])
        if scheme == "incremental":
            n_zones, clip_lo, clip_hi = calibrate_n_zones_incremental(
                stages, K, slope, bounds, params["q_offset"])
        else:
            n_zones, clip_lo, clip_hi = calibrate_n_zones(
                stages, K, slope, bounds, params["q_offset"], params["shift"])
        if not n_zones:
            diag["status"] = "no_valid_n"; diagnostics.append(diag)
            log.warning("  Gauge %s HydroID %d: no valid n — skip", loc_id, hydroid)
            continue

        # Independent total-scheme (whole-section) back-calculation at the
        # SAME anchors — computed BEFORE apply_to_hydroid so the hydroTable's
        # discharge_cms_wholesection curve and this gauge's n_eff_{yr}yr are
        # driven by the identical n_eff dict (no drift between the two).
        n_eff, _, _ = calibrate_n_zones(
            stages, K, slope, bounds, params["q_offset"], 0.0)

        ht, fixed = apply_to_hydroid(ht, hydroid, bounds, n_zones, params,
                                     scheme=scheme, n_zones_total=n_eff)

        diag.update(n_clipped_low=clip_lo, n_clipped_high=clip_hi,
                    monotone_fixed_rows=fixed, status="calibrated")
        for yr, n in n_zones.items():
            diag[f"n_{yr}yr"] = round(n, 4)
        for yr, n in n_eff.items():
            diag[f"n_eff_{yr}yr"] = round(n, 4)
        diagnostics.append(diag)
        seen_hids.add(hydroid)
        n_calibrated += 1

        log.info("  Gauge %s HydroID %d [%s δ=%.2fm w0=%.1fm Q0=%.1f]: n = %s",
                 loc_id, hydroid, params["mode"], params["delta"],
                 params["w0"], params["q_offset"],
                 {yr: f"{n:.4f}" for yr, n in sorted(n_zones.items())})

    if n_calibrated == 0:
        log.warning("  Branch %s: no gauge calibrated — hydroTable left at baseline", bid)
        return

    _safe_write_csv(ht, ht_path, bid)
    log.info("  Branch %s: calibrated %d gauged HydroID(s) in hydroTable", bid, n_calibrated)


def run_huc(method: str, huc8: str, rc: pd.DataFrame, recur: pd.DataFrame,
            scheme: str = "incremental") -> None:
    aoi_root = OUT_DIR / f"HUC{huc8}"
    branches = aoi_root / "watershed-data" / "branches"
    elev_path = aoi_root / "usgs_elev_table.csv"
    if not elev_path.exists():
        raise FileNotFoundError(
            f"usgs_elev_table.csv not found — run s04 crosswalk first: {elev_path}")

    elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
    rc_ids    = set(rc["location_id"].astype(str))
    non_trunk = elev[elev["levpa_id"].astype(str) != "0"].copy()
    non_trunk["has_rc"] = non_trunk["location_id"].apply(
        lambda loc: str(loc).zfill(8) in rc_ids)
    gauged_branches = set(non_trunk[non_trunk["has_rc"]]["levpa_id"].astype(str))
    if not gauged_branches:
        log.info("  No gauged branches for HUC %s — nothing to do", huc8)
        return

    diagnostics: list[dict] = []
    for branch_dir in sorted(d for d in branches.iterdir() if d.is_dir()):
        if branch_dir.name in gauged_branches:
            process_branch(method, branch_dir.name, branch_dir,
                           elev, rc, recur, diagnostics, huc8, scheme=scheme)

    if diagnostics:
        diag_path = aoi_root / "ncalib_diagnostics.csv"
        pd.DataFrame(diagnostics).to_csv(diag_path, index=False)
        n_ok = sum(1 for d in diagnostics if d["status"] == "calibrated")
        log.info("  Diagnostics → %s  (%d/%d gauges calibrated, method=%s, scheme=%s)",
                 diag_path.name, n_ok, len(diagnostics), method, scheme)


def run_batch(method: str, scheme: str = "incremental") -> None:
    """Run one calibration method across the whole study area.

    scheme: "incremental" (slice composite — continuous/monotone by
    construction, n = band roughness) or "total" (whole-section n with
    boundary transitions).  The status file is scheme-specific so switching
    scheme forces a full rerun."""
    if method not in METHOD_LABELS:
        raise ValueError(f"unknown method {method!r} — one of {list(METHOD_LABELS)}")
    if scheme not in ("incremental", "total"):
        raise ValueError(f"unknown scheme {scheme!r}")
    task_log = OUT_DIR / f"ncalib_status_{method}_{scheme}.txt"

    def _done() -> set[str]:
        if not task_log.exists():
            return set()
        return {p[1] for line in task_log.read_text().splitlines()
                if len(p := line.strip().split()) >= 3 and p[2] == "PASS"}

    hucs = study_hucs()
    done = _done()
    remaining = [h for h in hucs if h not in done]
    log.info("n-Calibration method %s (%s) scheme=%s: %d total | %d done | %d to run",
             method, METHOD_LABELS[method], scheme, len(hucs), len(done), len(remaining))

    rc, recur = load_shared()
    t0 = time.time()
    failed = []
    for i, huc8 in enumerate(remaining, 1):
        t = time.time()
        log.info("  [%d/%d] HUC8 = %s", i, len(remaining), huc8)
        try:
            run_huc(method, huc8, rc, recur, scheme=scheme)
            with task_log.open("a") as f:
                f.write(f"ncalib {huc8} PASS\n")
        except Exception:
            err = traceback.format_exc().splitlines()[-1]
            with task_log.open("a") as f:
                f.write(f"ncalib {huc8} FAIL {err}\n")
            failed.append(huc8)
            log.error("  [%s] FAIL  |  %s", huc8, err)
        else:
            log.info("  [%s] PASS  |  %.0fs", huc8, time.time() - t)

    log.info("Method %s complete in %.0fs  |  failed: %s",
             method, time.time() - t0, failed or "none")
