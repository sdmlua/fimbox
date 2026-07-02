"""
Calibration analysis and visualization — full study area.

Individual plots  (E:/SI/out/HUC{huc8}/src_plots/):
    rc_{huc8}_{bid}.png          Rating curve comparison per calibrated branch

Study-area aggregate  (E:/SI/out/calibration_analysis/):
    01_n_profiles.png            Manning's n vs HAND stage — all gauges overlaid
    02_q_scatter.png             Q_orig vs Q_USGS  /  Q_calib vs Q_USGS (log scale)
    03_adj_ratio.png             Q_calib / Q_orig by zone — violin plots
    04_n_by_zone.png             Calibrated n distribution by zone — box plots
    05_coverage.png              Calibrated vs total branches per HUC
    06_metrics.png               NSE / KGE / PBIAS / RMSE before-vs-after scatter
    07_n_vs_slope.png            Calibrated n vs channel slope by zone
    08_stage_shift.png           HAND stage shift (Δh) at each recurrence flow
    09_n_direction.png           Count/% of gauges where n increased vs decreased (stacked bar + table)
    10_src_ensemble_original.png All original SRCs overlaid with median + IQR band
    11_src_ensemble_calibrated.png All calibrated SRCs overlaid with median + IQR band
    metrics_summary.csv          Per-gauge tabular metrics

Run:
    .venv\\Scripts\\python.exe scripts/plot_calibration_analysis.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib as mpl
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

# ── Style ─────────────────────────────────────────────────────────
mpl.rcParams.update({
    "font.family":        "DejaVu Sans",
    "font.size":          10.5,
    "axes.labelsize":     12,
    "axes.titlesize":     11.5,
    "axes.titleweight":   "bold",
    "axes.spines.top":    False,
    "axes.spines.right":  False,
    "axes.grid":          True,
    "grid.alpha":         0.22,
    "grid.linestyle":     ":",
    "legend.fontsize":    9.5,
    "legend.framealpha":  0.92,
    "figure.dpi":         150,
})

# ── Palette ───────────────────────────────────────────────────────
C_USGS  = "#111111"
C_ORIG  = "#2471a3"
C_CALIB = "#c0392b"
C_NAVY  = "#1b4f72"
C_GRAY  = "#7f8c8d"
ZONE_CLR = ["#aed6f1", "#a9dfbf", "#fad7a0", "#f9e79f", "#f1948a", "#d2b4de"]
GAUGE_PALETTE = [
    "#e41a1c", "#377eb8", "#4daf4a", "#984ea3",
    "#ff7f00", "#a65628", "#f781bf", "#999999",
    "#1b9e77", "#d95f02", "#7570b3", "#e7298a",
    "#66a61e", "#e6ab02", "#a6761d", "#666666",
    "#8dd3c7", "#fb8072", "#80b1d3", "#fdb462",
]

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH       = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL     = "HUC_CODE"
OUT_DIR          = Path("E:/SI/out")
DATA_DIR         = Path("data")
SUMMARY_DIR      = Path("E:/SI/out/calibration_analysis")
RECURRENCE_YEARS = [2, 5, 10, 25, 50]
RECUR_COLS       = {yr: f"{yr}_0_year_recurrence_flow_17C" for yr in RECURRENCE_YEARS}
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# SECTION 1 — Metrics
# ══════════════════════════════════════════════════════════════════

def _nse(obs, sim):
    d = np.sum((obs - obs.mean()) ** 2)
    return float(1 - np.sum((sim - obs) ** 2) / d) if d > 0 else np.nan

def _kge(obs, sim):
    if obs.std() == 0 or sim.std() == 0:
        return np.nan
    r     = np.corrcoef(obs, sim)[0, 1]
    alpha = sim.std()  / obs.std()
    beta  = sim.mean() / obs.mean() if obs.mean() != 0 else np.nan
    return float(1 - np.sqrt((r - 1)**2 + (alpha - 1)**2 + (beta - 1)**2))

def _pbias(obs, sim):
    return float(100 * (sim - obs).sum() / obs.sum()) if obs.sum() != 0 else np.nan

def _rmse(obs, sim):
    return float(np.sqrt(((sim - obs) ** 2).mean()))


# ══════════════════════════════════════════════════════════════════
# SECTION 2 — Data helpers
# ══════════════════════════════════════════════════════════════════

def _load_shared_data():
    rc = pd.read_parquet(DATA_DIR / "usgs_rating_curves.parquet")
    rc["location_id"] = rc["location_id"].astype(str)
    rc["flow_cms"]    = rc["flow"].astype(float) / 35.3147
    rc["elev_m"]      = rc["elevation_navd88"].astype(float) / 3.28084

    recur = pd.read_parquet(DATA_DIR / "nwm3_17C_recurrence_flows_cfs.parquet")
    for yr, col in RECUR_COLS.items():
        recur[f"Q_{yr}yr_cms"] = recur[col].astype(float) * 0.028317
    recur["feature_id"] = recur["feature_id"].astype("Int64")
    return rc, recur


def _load_src(branch_dir: Path, bid: str, original: bool) -> pd.DataFrame | None:
    suffix = ".pre_n_calib.csv" if original else ".csv"
    p = branch_dir / f"src_full_crosswalked_{bid}{suffix}"
    if not p.exists():
        if original:
            p = branch_dir / f"src_full_crosswalked_{bid}.csv"
        if not p.exists():
            return None
    df = pd.read_csv(p, low_memory=False)
    df["HydroID"] = df["HydroID"].astype(int)
    return df


def _zone_pts(loc_id, feat_id, dem_adj_m, rc, recur):
    loc_pad = str(loc_id).zfill(8)
    grc = rc[rc["location_id"] == loc_pad].sort_values("flow_cms").drop_duplicates("flow_cms")
    if grc.empty:
        return []
    feat = recur[recur["feature_id"] == feat_id]
    if feat.empty:
        return []
    pts = []
    for yr in RECURRENCE_YEARS:
        Q = float(feat[f"Q_{yr}yr_cms"].iloc[0])
        if Q < grc["flow_cms"].min() or Q > grc["flow_cms"].max():
            continue
        elev = float(np.interp(Q, grc["flow_cms"].values, grc["elev_m"].values))
        h = elev - dem_adj_m
        if h > 0:
            pts.append((yr, h, Q))
    return sorted(pts, key=lambda x: x[0])


def _n_step(zp, src_calib, hydroid, y_max):
    """Return (n_vals, h_vals) step arrays for the Manning's n twin axis."""
    hdf = src_calib[src_calib["HydroID"] == hydroid].sort_values("Stage")
    if hdf.empty or "zonal_n_applied" not in hdf.columns:
        return [], []
    h_brk = [0.0] + [h for _, h, _ in zp]
    if h_brk[-1] < y_max:
        h_brk.append(y_max)
    nx, hy = [], []
    for i, (h_lo, h_hi) in enumerate(zip(h_brk[:-1], h_brk[1:])):
        idx = (hdf["Stage"] - (h_lo + h_hi) / 2).abs().idxmin()
        n_v = float(hdf.loc[idx, "zonal_n_applied"])
        nx.extend([n_v, n_v])
        hy.extend([h_lo, h_hi])
        if i < len(h_brk) - 2:
            nx.append(float(hdf.loc[
                (hdf["Stage"] - h_brk[i+1]).abs().idxmin(), "zonal_n_applied"]))
            hy.append(h_hi)
    return nx, hy


def _discover_calibrated(huc8: str, rc: pd.DataFrame, elev: pd.DataFrame):
    """
    Returns list of dicts with gauge info for branches that were actually
    calibrated (have a .pre_n_calib.csv backup AND have USGS RC data).
    """
    branches_dir = OUT_DIR / f"HUC{huc8}" / "watershed-data" / "branches"
    if not branches_dir.exists():
        return []
    rc_ids = set(rc["location_id"].astype(str))
    results = []
    for branch_dir in sorted(branches_dir.iterdir()):
        if not branch_dir.is_dir():
            continue
        bid = branch_dir.name
        if bid == "0":
            continue
        pre_exists = (branch_dir / f"src_full_crosswalked_{bid}.pre_n_calib.csv").exists()
        if not pre_exists:
            continue
        branch_elev = elev[elev["levpa_id"].astype(str) == bid]
        for _, gage in branch_elev.iterrows():
            loc_id = str(gage["location_id"]).zfill(8)
            if loc_id not in rc_ids:
                continue
            if pd.isna(gage.get("feature_id")):
                continue
            results.append({
                "bid": bid,
                "location_id": loc_id,
                "hydroid": int(gage["HydroID"]),
                "feature_id": int(gage["feature_id"]),
                "dem_adj_m": float(gage["dem_adj_elevation"]),
                "branch_dir": branch_dir,
            })
    return results


def _total_branches(huc8: str) -> int:
    lst = OUT_DIR / f"HUC{huc8}" / "watershed-data" / "branch_ids.lst"
    if not lst.exists():
        return 0
    lines = [l.strip() for l in lst.read_text().splitlines() if l.strip()]
    return len(lines) + 1   # +1 for Branch Zero


# ══════════════════════════════════════════════════════════════════
# SECTION 3 — Individual RC plots
# ══════════════════════════════════════════════════════════════════

def _draw_rc_panel(ax, bid, loc_id, hydroid, feat_id, dem_adj_m,
                   src_orig, src_calib, rc, recur):
    zp = _zone_pts(loc_id, feat_id, dem_adj_m, rc, recur)
    if not zp:
        return

    loc_pad = str(loc_id).zfill(8)
    grc = rc[rc["location_id"] == loc_pad].sort_values("flow_cms").copy()
    grc["hand"] = grc["elev_m"] - dem_adj_m
    grc = grc[grc["hand"] > 0]

    h_max_zone = max(h for _, h, _ in zp)
    y_max = max(h_max_zone * 1.5, 3.0)
    rc_q_max = float(grc[grc["hand"] <= y_max * 1.05]["flow_cms"].max()) if not grc.empty else 0.0
    x_max = max(rc_q_max, max(Q for _, _, Q in zp) * 1.4) * 1.08

    # Zone shading
    h_edges = [0.0] + [h for _, h, _ in zp]
    if h_edges[-1] < y_max:
        h_edges.append(y_max)
    for i, (hlo, hhi) in enumerate(zip(h_edges[:-1], h_edges[1:])):
        ax.axhspan(hlo, min(hhi, y_max), color=ZONE_CLR[min(i, 5)],
                   alpha=0.28, zorder=0, lw=0)

    # Zone lines + labels
    prev_lh = -1.0
    min_sep  = y_max * 0.07
    for yr, h, _ in zp:
        ax.axhline(h, color="#888", ls=":", lw=0.9, alpha=0.7, zorder=1)
        lh = max(h, prev_lh + min_sep)
        ax.text(x_max * 0.985, lh, f"{yr} yr",
                ha="right", va="bottom", fontsize=8.5,
                color="#555", fontweight="bold", zorder=10)
        prev_lh = lh

    # USGS RC
    if not grc.empty:
        rcp = grc[grc["hand"] <= y_max * 1.02]
        ax.plot(rcp["flow_cms"], rcp["hand"],
                color=C_USGS, lw=2.5, zorder=5, label="USGS Rating Curve")
    for _, h, Q in zp:
        ax.scatter(Q, h, color=C_USGS, s=55, zorder=9,
                   edgecolors="white", linewidths=0.8)

    # SRC curves
    s_o = src_orig[src_orig["HydroID"]  == hydroid].sort_values("Stage")
    s_c = src_calib[src_calib["HydroID"] == hydroid].sort_values("Stage")
    if not s_o.empty:
        ax.plot(s_o["Discharge (m3s-1)"], s_o["Stage"],
                color=C_ORIG, lw=1.8, ls="-", zorder=4, label="Original SRC (n=0.06)")
    if not s_c.empty:
        ax.plot(s_c["Discharge (m3s-1)"], s_c["Stage"],
                color=C_CALIB, lw=2.8, ls="--", zorder=6, label="Calibrated SRC")

    # Per-zone n value labels inside each zone band
    if not s_c.empty and "zonal_n_applied" in s_c.columns and zp:
        h_brk = [0.0] + [h for _, h, _ in zp] + [y_max]
        for i, (hlo, hhi) in enumerate(zip(h_brk[:-1], h_brk[1:])):
            hlo_c, hhi_c = min(hlo, y_max), min(hhi, y_max)
            if hhi_c <= hlo_c:
                continue
            mid_h = (hlo_c + hhi_c) / 2
            idx   = (s_c["Stage"] - mid_h).abs().idxmin()
            n_val = float(s_c.loc[idx, "zonal_n_applied"])
            ax.text(x_max * 0.015, mid_h, f"n = {n_val:.3f}",
                    ha="left", va="center", fontsize=8.5, color="#222",
                    fontweight="bold", zorder=12,
                    bbox=dict(boxstyle="round,pad=0.22", facecolor="white",
                              alpha=0.80, edgecolor="none"))
    # Footer note explaining kinks
    ax.text(0.5, -0.10,
            "Kinks in calibrated SRC at zone boundaries are expected — Manning's n changes\n"
            "abruptly between zones; the curve is continuous within each zone.",
            transform=ax.transAxes, ha="center", va="top",
            fontsize=8, color="#666", style="italic")

    # Manning's n twin axis
    ax_n = ax.twiny()
    nx, hy = _n_step(zp, src_calib, hydroid, y_max)
    if nx:
        ax_n.plot(nx, hy, color="#444", alpha=0.4, lw=2.0, zorder=3)
        ax_n.fill_betweenx(hy, 0, nx, color="#888", alpha=0.07, zorder=2)
    n_vals = [v for v in nx if v > 0] if nx else [0.06]
    ax_n.set_xlim(0, max(max(n_vals) * 1.5, 0.35))
    ax_n.set_xlabel("Manning's n →", fontsize=9, color="#666", labelpad=4)
    ax_n.tick_params(axis="x", colors="#777", labelsize=8)
    for sp in ["right", "left", "bottom"]:
        ax_n.spines[sp].set_visible(False)
    ax_n.spines["top"].set_color("#aaa")

    ax.set_xlim(0, x_max)
    ax.set_ylim(0, y_max)
    ax.set_xlabel("Discharge  (m³/s)", labelpad=6)
    ax.set_ylabel("HAND Stage  (m above thalweg)", labelpad=6)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.set_title(f"Gauge {loc_id}   ·   HydroID {hydroid}", pad=26)
    ax.legend(loc="upper left", fontsize=9)


def plot_individual_branches(hucs, rc, recur):
    log.info("=== Individual RC plots ===")
    for huc8 in hucs:
        elev_path = OUT_DIR / f"HUC{huc8}" / "usgs_elev_table.csv"
        if not elev_path.exists():
            continue
        elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
        gauges = _discover_calibrated(huc8, rc, elev)
        if not gauges:
            continue

        # Group by branch
        by_branch: dict[str, list] = {}
        for g in gauges:
            by_branch.setdefault(g["bid"], []).append(g)

        save_dir = OUT_DIR / f"HUC{huc8}" / "src_plots"
        save_dir.mkdir(parents=True, exist_ok=True)

        for bid, gage_list in by_branch.items():
            branch_dir = gage_list[0]["branch_dir"]
            src_orig  = _load_src(branch_dir, bid, original=True)
            src_calib = _load_src(branch_dir, bid, original=False)
            if src_orig is None or src_calib is None:
                continue

            n_g = len(gage_list)
            fig, axes = plt.subplots(1, n_g, figsize=(12 * n_g, 8.5),
                                     squeeze=False, constrained_layout=True)
            for col, g in enumerate(gage_list):
                _draw_rc_panel(axes[0, col], bid,
                               g["location_id"], g["hydroid"], g["feature_id"],
                               g["dem_adj_m"],
                               src_orig, src_calib, rc, recur)

            fig.suptitle(
                f"Manning's n Recurrence Calibration   ·   "
                f"HUC8 {huc8}   ·   Branch {bid}",
                fontsize=13, fontweight="bold", y=1.01,
            )
            out = save_dir / f"rc_{huc8}_{bid}.png"
            fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
            plt.close(fig)
            log.info("  Saved: %s", out.name)


# ══════════════════════════════════════════════════════════════════
# SECTION 4 — Data aggregation loop
# ══════════════════════════════════════════════════════════════════

def collect_all_data(hucs, rc, recur):
    """
    Returns (master_df, metrics_df, zone_df, coverage_df).

    master_df   : one row per USGS RC data point (gauge × stage)
    metrics_df  : one row per gauge (NSE, KGE, PBIAS, RMSE before/after)
    zone_df     : one row per (gauge × zone) with calibrated n and stage shift
    coverage_df : one row per HUC with total and calibrated branch counts
    """
    master_rows  = []
    metric_rows  = []
    zone_rows    = []
    coverage_rows = []

    for huc8 in hucs:
        elev_path = OUT_DIR / f"HUC{huc8}" / "usgs_elev_table.csv"
        n_total = _total_branches(huc8)
        if not elev_path.exists():
            coverage_rows.append({"huc8": huc8, "total": n_total, "calibrated": 0})
            continue
        elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
        gauges = _discover_calibrated(huc8, rc, elev)
        calibrated_bids = {g["bid"] for g in gauges}
        coverage_rows.append({
            "huc8": huc8,
            "total": n_total,
            "calibrated": len(calibrated_bids),
        })
        if not gauges:
            continue

        by_branch: dict[str, list] = {}
        for g in gauges:
            by_branch.setdefault(g["bid"], []).append(g)

        for bid, gage_list in by_branch.items():
            branch_dir = gage_list[0]["branch_dir"]
            src_orig  = _load_src(branch_dir, bid, original=True)
            src_calib = _load_src(branch_dir, bid, original=False)
            if src_orig is None or src_calib is None:
                continue

            for g in gage_list:
                loc_id   = g["location_id"]
                hydroid  = g["hydroid"]
                feat_id  = g["feature_id"]
                dem_adj  = g["dem_adj_m"]

                zp = _zone_pts(loc_id, feat_id, dem_adj, rc, recur)
                if not zp:
                    continue

                # USGS RC in HAND space
                loc_pad = str(loc_id).zfill(8)
                grc = rc[rc["location_id"] == loc_pad].copy()
                grc["hand"] = grc["elev_m"] - dem_adj
                grc = grc[(grc["hand"] > 0)].sort_values("hand")
                if grc.empty:
                    continue

                s_o = src_orig[src_orig["HydroID"]  == hydroid].sort_values("Stage")
                s_c = src_calib[src_calib["HydroID"] == hydroid].sort_values("Stage")
                if s_o.empty or s_c.empty:
                    continue

                slope = float(s_o["SLOPE"].iloc[0])
                src_max = float(s_o["Stage"].max())
                valid = grc[grc["hand"] <= src_max * 1.05]

                q_usgs  = valid["flow_cms"].values
                q_orig  = np.interp(valid["hand"].values,
                                    s_o["Stage"].values,
                                    s_o["Discharge (m3s-1)"].values)
                q_calib = np.interp(valid["hand"].values,
                                    s_c["Stage"].values,
                                    s_c["Discharge (m3s-1)"].values)

                # Zone assignment
                h_bounds = [0.0] + [h for _, h, _ in zp]
                zones    = np.searchsorted(h_bounds, valid["hand"].values, side="right")

                # n_applied (from calibrated SRC if column present, else NaN)
                if "zonal_n_applied" in s_c.columns:
                    n_app = np.interp(valid["hand"].values,
                                      s_c["Stage"].values,
                                      s_c["zonal_n_applied"].values)
                else:
                    n_app = np.full(len(valid), np.nan)

                # Master records
                for i in range(len(valid)):
                    master_rows.append({
                        "huc8": huc8, "bid": bid,
                        "location_id": loc_id, "hydroid": hydroid,
                        "hand_m": float(valid["hand"].values[i]),
                        "q_usgs": float(q_usgs[i]),
                        "q_orig": float(q_orig[i]),
                        "q_calib": float(q_calib[i]),
                        "zone": int(zones[i]),
                        "n_applied": float(n_app[i]),
                        "slope": slope,
                    })

                # Metrics
                mask = (q_usgs > 0) & (q_orig > 0) & (q_calib > 0)
                if mask.sum() >= 5:
                    m = {
                        "huc8": huc8, "bid": bid, "location_id": loc_id,
                        "hydroid": hydroid, "n_pts": int(mask.sum()),
                        "nse_orig":    _nse(q_usgs[mask],   q_orig[mask]),
                        "nse_calib":   _nse(q_usgs[mask],   q_calib[mask]),
                        "kge_orig":    _kge(q_usgs[mask],   q_orig[mask]),
                        "kge_calib":   _kge(q_usgs[mask],   q_calib[mask]),
                        "pbias_orig":  _pbias(q_usgs[mask], q_orig[mask]),
                        "pbias_calib": _pbias(q_usgs[mask], q_calib[mask]),
                        "rmse_orig":   _rmse(q_usgs[mask],  q_orig[mask]),
                        "rmse_calib":  _rmse(q_usgs[mask],  q_calib[mask]),
                    }
                    metric_rows.append(m)

                # Zone-level stats
                h_sorted = sorted(zp, key=lambda x: x[0])
                for z_idx, (yr, h_zone, Q_r) in enumerate(h_sorted, 1):
                    idx   = (s_o["Stage"] - h_zone).abs().idxmin()
                    row_o = s_o.loc[idx]
                    idx_c = (s_c["Stage"] - h_zone).abs().idxmin()
                    # Use the actually-applied n (already clipped to N_MIN/N_MAX by s05)
                    if "zonal_n_applied" in s_c.columns:
                        n_zone = float(s_c.loc[idx_c, "zonal_n_applied"])
                    else:
                        A = float(row_o.get("WetArea (m2)", 0))
                        R = float(row_o.get("HydraulicRadius (m)", 0))
                        n_zone = float(A * (R ** (2/3)) * slope**0.5 / Q_r) if Q_r > 0 and A > 0 and R > 0 else np.nan

                    # Stage shift at Q_r (invert SRC)
                    qo = s_o["Discharge (m3s-1)"].values
                    ho = s_o["Stage"].values
                    qc = s_c["Discharge (m3s-1)"].values
                    hc = s_c["Stage"].values
                    # Only invert where Q is monotonically increasing
                    h_at_Qr_orig  = float(np.interp(Q_r, qo, ho))  if Q_r <= qo.max() else np.nan
                    h_at_Qr_calib = float(np.interp(Q_r, qc, hc))  if Q_r <= qc.max() else np.nan
                    delta_h = h_at_Qr_calib - h_at_Qr_orig if not (np.isnan(h_at_Qr_orig) or np.isnan(h_at_Qr_calib)) else np.nan

                    zone_rows.append({
                        "huc8": huc8, "bid": bid, "location_id": loc_id,
                        "hydroid": hydroid, "recurrence_yr": yr,
                        "zone": z_idx, "h_zone": h_zone, "Q_r": Q_r,
                        "n_calibrated": n_zone, "slope": slope,
                        "h_at_Qr_orig": h_at_Qr_orig,
                        "h_at_Qr_calib": h_at_Qr_calib,
                        "delta_h": delta_h,
                    })

    master_df   = pd.DataFrame(master_rows)
    metrics_df  = pd.DataFrame(metric_rows)
    zone_df     = pd.DataFrame(zone_rows)
    coverage_df = pd.DataFrame(coverage_rows)
    return master_df, metrics_df, zone_df, coverage_df


# ══════════════════════════════════════════════════════════════════
# SECTION 5 — Aggregate plots
# ══════════════════════════════════════════════════════════════════

def _gauge_label(loc_id, huc8):
    return f"{loc_id}\n({huc8})"


def plot_01_n_profiles(zone_df: pd.DataFrame, out_dir: Path):
    """Manning's n vs HAND stage — step-function for every gauge."""
    gauges = zone_df[["location_id", "huc8"]].drop_duplicates()
    if gauges.empty:
        return

    fig, ax = plt.subplots(figsize=(12, 7), constrained_layout=True)
    legend_handles = []

    for ci, (_, row) in enumerate(gauges.iterrows()):
        loc_id = row["location_id"]
        huc8   = row["huc8"]
        gdf    = zone_df[(zone_df["location_id"] == loc_id) &
                         (zone_df["huc8"] == huc8)].sort_values("zone")
        if gdf.empty:
            continue
        col = GAUGE_PALETTE[ci % len(GAUGE_PALETTE)]

        # Build step function: [(h_lo, h_hi, n)]
        h_vals = [0.0] + gdf["h_zone"].tolist()
        n_vals = gdf["n_calibrated"].tolist()
        for i, (hlo, hhi) in enumerate(zip(h_vals[:-1], h_vals[1:])):
            ax.plot([n_vals[i], n_vals[i]], [hlo, hhi],
                    color=col, lw=1.8, alpha=0.85, zorder=4)
            if i < len(n_vals) - 1:
                ax.plot([n_vals[i], n_vals[i + 1]], [hhi, hhi],
                        color=col, lw=1.8, alpha=0.85, zorder=4)
        # Extend to 0 at bottom
        ax.plot([n_vals[0], n_vals[0]], [0, h_vals[1]],
                color=col, lw=1.8, alpha=0.85, zorder=4)
        legend_handles.append(Line2D([0], [0], color=col, lw=2,
                                      label=_gauge_label(loc_id, huc8)))

    ax.axvline(0.06, color="#333", ls="--", lw=1.2, alpha=0.6, label="Default n = 0.06")
    ax.axvline(0.12, color="#777", ls=":",  lw=1.2, alpha=0.6, label="Overbank n = 0.12")
    ax.set_xlabel("Manning's n", labelpad=8)
    ax.set_ylabel("HAND Stage  (m above thalweg)", labelpad=8)
    ax.set_title("Calibrated Manning's n Profiles — All Gauges", pad=12)
    ax.legend(handles=legend_handles + [
        Line2D([0], [0], color="#333", ls="--", lw=1.2, label="Default n = 0.06"),
        Line2D([0], [0], color="#777", ls=":",  lw=1.2, label="Overbank n = 0.12"),
    ], loc="upper right", fontsize=8.5, ncol=2)
    # Clip x-axis to 95th percentile — suppresses outliers without losing the main distribution
    all_n = zone_df["n_calibrated"].dropna().values
    x_clip = float(np.percentile(all_n, 95)) * 1.2 if len(all_n) > 0 else 0.5
    x_clip = max(x_clip, 0.4)
    n_excl = int((all_n > x_clip).sum()) if len(all_n) > 0 else 0
    ax.set_xlim(0, x_clip)
    if n_excl > 0:
        ax.text(0.98, 0.02,
                f"Note: {n_excl} n value(s) > {x_clip:.2f} not shown (outliers)",
                transform=ax.transAxes, ha="right", va="bottom",
                fontsize=8.5, color="#777", style="italic")
    out = out_dir / "01_n_profiles.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def plot_02_q_scatter(master_df: pd.DataFrame, out_dir: Path):
    """Q comparison scatter (log scale) — original vs calibrated vs USGS."""
    if master_df.empty:
        return
    df = master_df[(master_df["q_usgs"] > 0) &
                   (master_df["q_orig"] > 0) &
                   (master_df["q_calib"] > 0)].copy()
    df["zone"] = df["zone"].clip(1, 6)

    zone_colors = {z: ZONE_CLR[z - 1] for z in range(1, 7)}
    ec = "#333"

    fig, axes = plt.subplots(1, 3, figsize=(17, 6), constrained_layout=True)
    pairs = [
        (axes[0], "q_orig",  "q_usgs", "Original SRC  vs  USGS RC"),
        (axes[1], "q_calib", "q_usgs", "Calibrated SRC  vs  USGS RC"),
        (axes[2], "q_orig",  "q_calib","Original SRC  vs  Calibrated SRC"),
    ]
    for ax, xcol, ycol, title in pairs:
        x, y = df[xcol].values, df[ycol].values
        colors = [zone_colors.get(z, "#ccc") for z in df["zone"]]
        ax.scatter(x, y, c=colors, s=12, alpha=0.6, edgecolors="none", zorder=4)
        lims = [min(x.min(), y.min()) * 0.9, max(x.max(), y.max()) * 1.1]
        ax.plot(lims, lims, "k--", lw=1.0, alpha=0.5, zorder=3, label="1:1")
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_xlim(lims); ax.set_ylim(lims)
        ax.set_xlabel(xcol.replace("_", " ").replace("q ", "Q ") + " (m³/s)")
        ax.set_ylabel(ycol.replace("_", " ").replace("q ", "Q ") + " (m³/s)")
        ax.set_title(title)
        ax.legend(loc="upper left", fontsize=9)

    # Zone legend
    patches = [mpatches.Patch(color=ZONE_CLR[i], label=f"Zone {i+1}")
               for i in range(6)]
    fig.legend(handles=patches, loc="lower center", ncol=6,
               bbox_to_anchor=(0.5, -0.04), fontsize=9,
               title="Zone", title_fontsize=9)
    fig.suptitle("Discharge Comparison  —  All USGS RC Stages (log scale)", fontsize=13)
    out = out_dir / "02_q_scatter.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def plot_03_adj_ratio(master_df: pd.DataFrame, out_dir: Path):
    """Q_calib / Q_orig by zone — violin + box plots."""
    if master_df.empty:
        return
    df = master_df[(master_df["q_orig"] > 0) & (master_df["q_calib"] > 0)].copy()
    df["ratio"] = df["q_calib"] / df["q_orig"]
    df["zone"]  = df["zone"].clip(1, 6)

    fig, ax = plt.subplots(figsize=(11, 6), constrained_layout=True)
    zone_labels = [f"Zone {z}" for z in range(1, 7)]
    data_by_zone = [df[df["zone"] == z]["ratio"].dropna().values for z in range(1, 7)]

    vp = ax.violinplot(data_by_zone, positions=range(1, 7),
                       showmedians=True, showextrema=False, widths=0.6)
    for i, body in enumerate(vp["bodies"]):
        body.set_facecolor(ZONE_CLR[i])
        body.set_alpha(0.65)
    vp["cmedians"].set_color("#111")
    vp["cmedians"].set_linewidth(2)

    # Overlay box
    bp = ax.boxplot(data_by_zone, positions=range(1, 7),
                    widths=0.18, patch_artist=False,
                    medianprops=dict(color="#333", lw=0),
                    whiskerprops=dict(color="#555"),
                    capprops=dict(color="#555"),
                    flierprops=dict(marker=".", ms=3, color="#aaa"),
                    manage_ticks=False)

    ax.axhline(1.0, color="#c0392b", lw=1.5, ls="--", alpha=0.8,
               label="No change (ratio = 1)")
    ax.set_xticks(range(1, 7))
    ax.set_xticklabels([f"Zone {z}\n({len(d)} pts)" for z, d in
                        zip(range(1, 7), data_by_zone)])
    ax.set_ylabel("Q_calibrated  /  Q_original")
    ax.set_title("Discharge Adjustment Ratio by Zone\n"
                 "(ratio > 1 = calibration increased discharge; < 1 = decreased)")
    ax.legend()
    out = out_dir / "03_adj_ratio.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def plot_04_n_by_zone(zone_df: pd.DataFrame, out_dir: Path):
    """Calibrated n distribution by zone — box + strip."""
    if zone_df.empty:
        return
    df = zone_df.dropna(subset=["n_calibrated"])

    fig, ax = plt.subplots(figsize=(11, 6), constrained_layout=True)
    data = [df[df["zone"] == z]["n_calibrated"].values for z in range(1, 6)]
    labels = [f"Zone {z}\n({RECURRENCE_YEARS[z-1]} yr boundary)" for z in range(1, 6)]
    n_pts  = [len(d) for d in data]

    bp = ax.boxplot(data, patch_artist=True,
                    medianprops=dict(color="#111", lw=2),
                    whiskerprops=dict(color="#555"),
                    capprops=dict(color="#555"),
                    flierprops=dict(marker="o", ms=5, alpha=0.5))
    for patch, col in zip(bp["boxes"], ZONE_CLR[:5]):
        patch.set_facecolor(col)
        patch.set_alpha(0.7)

    # Strip plot
    rng = np.random.default_rng(42)
    for z_idx, d in enumerate(data, 1):
        jitter = rng.uniform(-0.12, 0.12, len(d))
        ax.scatter(np.full(len(d), z_idx) + jitter, d,
                   color=ZONE_CLR[z_idx - 1], s=28, alpha=0.7,
                   edgecolors="#333", linewidths=0.5, zorder=5)

    ax.axhline(0.06, color=C_NAVY, ls="--", lw=1.5, alpha=0.8, label="Default n = 0.06")
    ax.axhline(0.12, color=C_GRAY, ls=":",  lw=1.5, alpha=0.8, label="Overbank n = 0.12")
    # Clip y-axis to 97th percentile — outlier protection
    n_all = df["n_calibrated"].values
    y_clip = float(np.percentile(n_all, 97)) * 1.15 if len(n_all) > 0 else 0.5
    y_clip = max(y_clip, 0.4)
    n_excl = int((n_all > y_clip).sum()) if len(n_all) > 0 else 0
    ax.set_ylim(0, y_clip)
    if n_excl > 0:
        ax.text(0.98, 0.98,
                f"Note: {n_excl} outlier(s) > {y_clip:.2f} not shown",
                transform=ax.transAxes, ha="right", va="top",
                fontsize=9, color="#777", style="italic")
    ax.set_xticks(range(1, 6))
    ax.set_xticklabels([f"{l}\n(n={n})" for l, n in zip(labels, n_pts)])
    ax.set_ylabel("Calibrated Manning's n")
    ax.set_title("Distribution of Calibrated Manning's n by Zone\n"
                 "(each point = one gauge; zone number = depth interval)")
    ax.legend()
    out = out_dir / "04_n_by_zone.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def plot_05_coverage(coverage_df: pd.DataFrame, out_dir: Path):
    """Calibrated vs total branches per HUC — horizontal stacked bar."""
    if coverage_df.empty:
        return
    df = coverage_df.sort_values("huc8").reset_index(drop=True)
    df["uncalibrated"] = df["total"] - df["calibrated"]

    fig, ax = plt.subplots(figsize=(11, max(5, len(df) * 0.45 + 1.5)),
                            constrained_layout=True)
    y = np.arange(len(df))
    ax.barh(y, df["uncalibrated"], color="#d0d3d4", label="Uncalibrated")
    ax.barh(y, df["calibrated"],   color=C_CALIB, alpha=0.8,
            left=df["uncalibrated"], label="Calibrated")

    for i, row in df.iterrows():
        if row["calibrated"] > 0:
            ax.text(row["total"] + 0.15, i,
                    f"{row['calibrated']}/{row['total']}",
                    va="center", fontsize=9, color="#333")

    ax.set_yticks(y)
    ax.set_yticklabels(df["huc8"])
    ax.set_xlabel("Number of Branches")
    ax.set_ylabel("HUC8")
    ax.set_title("Calibration Coverage per HUC\n"
                 f"(Total: {df['calibrated'].sum()} calibrated / "
                 f"{df['total'].sum()} branches across {len(df)} HUCs)")
    ax.legend(loc="lower right")
    out = out_dir / "05_coverage.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def plot_06_metrics(metrics_df: pd.DataFrame, out_dir: Path):
    """Before-vs-after metric scatter — one panel per metric."""
    if metrics_df.empty:
        return

    fig, axes = plt.subplots(2, 2, figsize=(12, 10), constrained_layout=True)
    pairs = [
        (axes[0, 0], "nse_orig",    "nse_calib",    "NSE",
         "higher is better", True),
        (axes[0, 1], "kge_orig",    "kge_calib",    "KGE",
         "higher is better", True),
        (axes[1, 0], "pbias_orig",  "pbias_calib",  "Percent Bias (%)",
         "closer to 0 is better", False),
        (axes[1, 1], "rmse_orig",   "rmse_calib",   "RMSE  (m³/s)",
         "lower is better", False),
    ]

    for ax, xcol, ycol, label, note, higher_better in pairs:
        x = metrics_df[xcol].values
        y = metrics_df[ycol].values
        valid = ~(np.isnan(x) | np.isnan(y))
        x, y = x[valid], y[valid]
        if len(x) == 0:
            continue

        lims = [min(x.min(), y.min()) - abs(min(x.min(), y.min())) * 0.05,
                max(x.max(), y.max()) * 1.05]
        ax.plot(lims, lims, "--", color="#aaa", lw=1.2, zorder=2, label="No change")

        colors = []
        for xi, yi in zip(x, y):
            if higher_better:
                colors.append("#27ae60" if yi > xi else "#c0392b")
            else:
                colors.append("#27ae60" if abs(yi) < abs(xi) else "#c0392b")

        for xi, yi, c in zip(x, y, colors):
            ax.annotate("", xy=(yi, xi), xytext=(xi, xi),
                        arrowprops=dict(arrowstyle="->", color=c, lw=1.0, alpha=0.4))
        ax.scatter(x, x, marker="o", s=60, color="#555", zorder=5, label="Before")
        ax.scatter(y, x, marker="D", s=60, color=colors, zorder=6, edgecolors="#333",
                   linewidths=0.6, label="After")

        ax.set_xlim(lims); ax.set_ylim(lims)
        ax.set_xlabel(f"{label}  (before)")
        ax.set_ylabel(f"{label}  (after)")
        ax.set_title(f"{label}  —  {note}")
        ax.legend(fontsize=8.5)

        n_imp = sum(1 for xi, yi in zip(x, y)
                    if (yi > xi if higher_better else abs(yi) < abs(xi)))
        ax.text(0.03, 0.97, f"Improved: {n_imp}/{len(x)}",
                transform=ax.transAxes, va="top", fontsize=9.5,
                color="#27ae60", fontweight="bold")

    fig.suptitle("Calibration Performance Metrics  —  Before vs After\n"
                 "(green arrow = improvement, red = degradation)", fontsize=13)
    out = out_dir / "06_metrics.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def plot_07_n_vs_slope(zone_df: pd.DataFrame, out_dir: Path):
    """Calibrated n vs channel slope by zone — scatter + per-zone trend."""
    df = zone_df.dropna(subset=["n_calibrated", "slope"])
    df = df[df["slope"] > 0]
    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(11, 6.5), constrained_layout=True)
    legend_handles = []
    for z in range(1, 6):
        sub = df[df["zone"] == z]
        if sub.empty:
            continue
        col = ZONE_CLR[z - 1]
        ax.scatter(sub["slope"], sub["n_calibrated"],
                   color=col, s=65, alpha=0.8,
                   edgecolors="#333", linewidths=0.6, zorder=5)
        # Log-linear trend line
        log_s = np.log10(sub["slope"].values)
        n_v   = sub["n_calibrated"].values
        if len(sub) >= 3:
            p = np.polyfit(log_s, n_v, 1)
            s_rng = np.logspace(np.log10(sub["slope"].min()),
                                np.log10(sub["slope"].max()), 50)
            ax.plot(s_rng, np.polyval(p, np.log10(s_rng)),
                    color=col, lw=1.8, ls="-", alpha=0.6, zorder=4)
        legend_handles.append(
            mpatches.Patch(color=col, alpha=0.8,
                           label=f"Zone {z}  ({RECURRENCE_YEARS[z-1]} yr)"))

    ax.set_xscale("log")
    ax.set_xlabel("Channel Slope  (IRIS-SWORD, m/m)", labelpad=8)
    ax.set_ylabel("Calibrated Manning's n", labelpad=8)
    ax.set_title("Manning's n vs Channel Slope by Zone\n"
                 "(line = log-linear trend per zone)", pad=10)
    ax.legend(handles=legend_handles, loc="upper right")
    out = out_dir / "07_n_vs_slope.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def plot_08_stage_shift(zone_df: pd.DataFrame, out_dir: Path):
    """HAND stage shift (Δh = h_calib - h_orig) at recurrence flows — box plot."""
    df = zone_df.dropna(subset=["delta_h"])
    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(11, 6), constrained_layout=True)
    yr_order = RECURRENCE_YEARS
    data = [df[df["recurrence_yr"] == yr]["delta_h"].values for yr in yr_order]
    n_pts = [len(d) for d in data]

    bp = ax.boxplot(data, patch_artist=True,
                    medianprops=dict(color="#111", lw=2.2),
                    whiskerprops=dict(color="#555"),
                    capprops=dict(color="#555"),
                    flierprops=dict(marker="o", ms=5, alpha=0.5))
    for patch, col in zip(bp["boxes"], ZONE_CLR[:5]):
        patch.set_facecolor(col)
        patch.set_alpha(0.7)

    rng = np.random.default_rng(0)
    for i, d in enumerate(data, 1):
        jitter = rng.uniform(-0.14, 0.14, len(d))
        ax.scatter(np.full(len(d), i) + jitter, d,
                   color=ZONE_CLR[i - 1], s=30, alpha=0.75,
                   edgecolors="#333", linewidths=0.5, zorder=5)

    ax.axhline(0, color="#c0392b", ls="--", lw=1.5, alpha=0.8,
               label="No change (Δh = 0)")
    ax.set_xticks(range(1, 6))
    ax.set_xticklabels([f"{yr} yr\n(n={n})" for yr, n in zip(yr_order, n_pts)])
    ax.set_xlabel("Recurrence Interval")
    ax.set_ylabel("HAND Stage Shift  Δh  (m)\n(positive = calibration raises flood stage)")
    ax.set_title("Flood Stage Shift from Calibration at NWM Recurrence Flows\n"
                 "(impact on FIM extent: Δh > 0 means larger predicted inundation)")
    ax.legend()
    out = out_dir / "08_stage_shift.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def plot_09_n_direction(zone_df: pd.DataFrame, out_dir: Path):
    """Stacked bar + table: how many gauges had n increase vs decrease from 0.06."""
    df = zone_df.dropna(subset=["n_calibrated"]).copy()
    tol = 0.005
    df["direction"] = "No change"
    df.loc[df["n_calibrated"] > 0.06 + tol, "direction"] = "Increased"
    df.loc[df["n_calibrated"] < 0.06 - tol, "direction"] = "Decreased"

    zones     = list(range(1, 6))
    yr_labels = [f"Zone {z}\n({RECURRENCE_YEARS[z-1]} yr)" for z in zones]
    counts    = {k: [] for k in ("Increased", "Decreased", "No change")}
    for z in zones:
        sub = df[df["zone"] == z]
        for k in counts:
            counts[k].append((sub["direction"] == k).sum())

    fig = plt.figure(figsize=(13, 9), constrained_layout=False)
    fig.subplots_adjust(top=0.91, bottom=0.30, left=0.08, right=0.96)
    ax = fig.add_subplot(111)

    x     = np.arange(len(zones))
    w     = 0.52
    c_inc  = "#c0392b"
    c_dec  = "#2471a3"
    c_none = "#bdc3c7"

    b_inc  = ax.bar(x, counts["Increased"],  w, label="Increased  (n > 0.06 + ε)", color=c_inc,  alpha=0.85)
    b_dec  = ax.bar(x, counts["Decreased"],  w, bottom=counts["Increased"],
                    label="Decreased  (n < 0.06 − ε)", color=c_dec,  alpha=0.85)
    b_none = ax.bar(x, counts["No change"],  w,
                    bottom=[i + d for i, d in zip(counts["Increased"], counts["Decreased"])],
                    label=f"No change  (|n − 0.06| ≤ {tol})", color=c_none, alpha=0.75)

    for i in range(len(zones)):
        total = sum(v[i] for v in counts.values())
        if total == 0:
            continue
        n_i = counts["Increased"][i]
        n_d = counts["Decreased"][i]
        n_s = counts["No change"][i]
        if n_i > 0:
            ax.text(i, n_i / 2, f"{n_i}\n({100*n_i/total:.0f}%)",
                    ha="center", va="center", fontsize=9.5, color="white", fontweight="bold")
        if n_d > 0:
            ax.text(i, n_i + n_d / 2, f"{n_d}\n({100*n_d/total:.0f}%)",
                    ha="center", va="center", fontsize=9.5, color="white", fontweight="bold")
        if n_s > 2:
            ax.text(i, n_i + n_d + n_s / 2, f"{n_s}\n({100*n_s/total:.0f}%)",
                    ha="center", va="center", fontsize=9, color="#444")

    ax.set_xticks(x)
    ax.set_xticklabels(yr_labels, fontsize=10.5)
    ax.set_ylabel("Number of Gauges")
    ax.set_title("Direction of Manning's n Change from Default  (n = 0.06)\nby Calibration Zone",
                 pad=10)
    ax.legend(loc="upper right", fontsize=9.5)

    # Summary table below the chart
    t_inc  = sum(counts["Increased"])
    t_dec  = sum(counts["Decreased"])
    t_none = sum(counts["No change"])
    grand  = t_inc + t_dec + t_none

    totals = [sum(v[i] for v in counts.values()) for i in range(len(zones))]
    pct = lambda n, t: f"{100*n/t:.0f}%" if t > 0 else "—"

    col_lbl  = [f"Zone {z}" for z in zones] + ["TOTAL"]
    row_data = [
        [str(counts["Increased"][i]) for i in range(5)]  + [str(t_inc)],
        [pct(counts["Increased"][i], totals[i]) for i in range(5)] + [pct(t_inc, grand)],
        [str(counts["Decreased"][i]) for i in range(5)]  + [str(t_dec)],
        [pct(counts["Decreased"][i], totals[i]) for i in range(5)] + [pct(t_dec, grand)],
        [str(counts["No change"][i]) for i in range(5)]  + [str(t_none)],
        [pct(counts["No change"][i], totals[i]) for i in range(5)] + [pct(t_none, grand)],
        [str(t) for t in totals] + [str(grand)],
    ]
    row_lbl  = ["n Increased", "  % Increased",
                "n Decreased", "  % Decreased",
                "No Change",   "  % No Change",
                "TOTAL"]

    ax_tab = fig.add_axes([0.05, 0.02, 0.91, 0.24])
    ax_tab.axis("off")
    tbl = ax_tab.table(cellText=row_data, rowLabels=row_lbl, colLabels=col_lbl,
                       loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9.5)
    tbl.scale(1, 1.35)
    row_bg = {0: c_inc, 2: c_dec, 4: c_none}
    for (r, c), cell in tbl.get_celld().items():
        if r == 0:
            cell.set_facecolor("#2c3e50")
            cell.set_text_props(color="white", fontweight="bold")
        elif (r - 1) in row_bg:
            cell.set_facecolor(row_bg[r - 1])
            cell.set_alpha(0.35)
        cell.set_edgecolor("#ccc")

    fig.suptitle("Manning's n Calibration Direction Analysis", fontsize=14, fontweight="bold")
    out = out_dir / "09_n_direction.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("  Saved: %s", out.name)


def collect_src_curves(hucs, rc, recur):
    """
    Returns two parallel lists (orig_curves, calib_curves).
    Each element: dict with keys huc8, bid, loc_id, q (ndarray), h (ndarray).
    One entry per calibrated HydroID.
    """
    orig_curves, calib_curves = [], []
    for huc8 in hucs:
        elev_path = OUT_DIR / f"HUC{huc8}" / "usgs_elev_table.csv"
        if not elev_path.exists():
            continue
        elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
        gauges = _discover_calibrated(huc8, rc, elev)
        if not gauges:
            continue
        by_branch: dict[str, list] = {}
        for g in gauges:
            by_branch.setdefault(g["bid"], []).append(g)
        for bid, gage_list in by_branch.items():
            branch_dir = gage_list[0]["branch_dir"]
            src_o = _load_src(branch_dir, bid, original=True)
            src_c = _load_src(branch_dir, bid, original=False)
            if src_o is None or src_c is None:
                continue
            for g in gage_list:
                hid = g["hydroid"]
                s_o = src_o[src_o["HydroID"] == hid].sort_values("Stage")
                s_c = src_c[src_c["HydroID"] == hid].sort_values("Stage")
                if s_o.empty or s_c.empty:
                    continue
                base = {"huc8": huc8, "bid": bid, "loc_id": g["location_id"]}
                orig_curves.append({**base,
                                    "q": s_o["Discharge (m3s-1)"].values.copy(),
                                    "h": s_o["Stage"].values.copy()})
                calib_curves.append({**base,
                                     "q": s_c["Discharge (m3s-1)"].values.copy(),
                                     "h": s_c["Stage"].values.copy()})
    return orig_curves, calib_curves


def _draw_src_ensemble(ax, curves, title, line_color,
                       shared_xlim=None, shared_ylim=None):
    """Draw individual SRC ensemble with median and IQR band.

    shared_xlim / shared_ylim: pass the same values to both original and
    calibrated figures so the axes are directly comparable.
    """
    h_max = float(max(c["h"].max() for c in curves))
    h_grid = np.linspace(0.01, h_max, 350)

    # Interpolate each curve onto common h grid (Q as function of h)
    q_mat = np.full((len(curves), len(h_grid)), np.nan)
    for i, c in enumerate(curves):
        q_mat[i] = np.interp(h_grid, c["h"], c["q"], left=np.nan, right=np.nan)

    # Individual curves — slightly darker so they read against white background
    for c in curves:
        ax.plot(c["q"], c["h"], color="#777", lw=0.7, alpha=0.28, zorder=2)

    # Percentile bands
    with np.errstate(all="ignore"):
        q_med = np.nanmedian(q_mat, axis=0)
        q_p25 = np.nanpercentile(q_mat, 25, axis=0)
        q_p75 = np.nanpercentile(q_mat, 75, axis=0)
        q_p10 = np.nanpercentile(q_mat, 10, axis=0)
        q_p90 = np.nanpercentile(q_mat, 90, axis=0)

    ax.fill_betweenx(h_grid, q_p10, q_p90, color=line_color, alpha=0.13,
                     zorder=3, label="10th–90th %ile")
    ax.fill_betweenx(h_grid, q_p25, q_p75, color=line_color, alpha=0.28,
                     zorder=4, label="IQR (25th–75th %ile)")
    ax.plot(q_med, h_grid, color=line_color, lw=3.0, zorder=6, label="Median")

    # Axes — use shared limits when provided so both plots are directly comparable
    if shared_xlim is not None:
        ax.set_xlim(0, shared_xlim)
    else:
        q_all = np.concatenate([c["q"] for c in curves])
        ax.set_xlim(0, float(np.nanpercentile(q_all, 99)) * 1.05)
    ax.set_ylim(0, shared_ylim if shared_ylim is not None else h_max)
    ax.set_xlabel("Discharge  (m³/s)", labelpad=8)
    ax.set_ylabel("HAND Stage  (m above thalweg)", labelpad=8)
    ax.set_title(title, pad=10)
    ax.legend(loc="upper left", fontsize=9.5)
    ax.text(0.98, 0.02, f"n = {len(curves)} SRC curves",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=9.5, color="#555")
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))


def plot_src_overlay(orig_curves, calib_curves, out_dir: Path):
    """Two figures: all original SRCs overlaid / all calibrated SRCs overlaid.

    Both figures use the same x and y axis limits so ensemble shapes can be
    compared directly side-by-side.
    """
    if not orig_curves:
        return

    # Compute shared scales from both ensembles combined
    all_q = np.concatenate([c["q"] for c in orig_curves + calib_curves])
    shared_xlim = float(np.nanpercentile(all_q, 99)) * 1.05
    shared_ylim = float(max(c["h"].max() for c in orig_curves + calib_curves))

    fig1, ax1 = plt.subplots(figsize=(12, 7.5), constrained_layout=True)
    _draw_src_ensemble(ax1, orig_curves,
                       "All Original SRCs — Gauged Branches  (pre-calibration, n = 0.06)",
                       C_ORIG, shared_xlim=shared_xlim, shared_ylim=shared_ylim)
    fig1.suptitle("Synthetic Rating Curve Ensemble  —  Original", fontsize=13, fontweight="bold")
    out1 = out_dir / "10_src_ensemble_original.png"
    fig1.savefig(out1, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig1)
    log.info("  Saved: %s", out1.name)

    fig2, ax2 = plt.subplots(figsize=(12, 7.5), constrained_layout=True)
    _draw_src_ensemble(ax2, calib_curves,
                       "All Calibrated SRCs — Gauged Branches  (post-calibration)",
                       C_CALIB, shared_xlim=shared_xlim, shared_ylim=shared_ylim)
    fig2.suptitle("Synthetic Rating Curve Ensemble  —  Calibrated", fontsize=13, fontweight="bold")
    out2 = out_dir / "11_src_ensemble_calibrated.png"
    fig2.savefig(out2, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig2)
    log.info("  Saved: %s", out2.name)


# ══════════════════════════════════════════════════════════════════
# SECTION 6 — Main
# ══════════════════════════════════════════════════════════════════

def main():
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    log.info("Study area: %d HUCs", len(hucs))

    log.info("Loading shared tables …")
    rc, recur = _load_shared_data()

    # ── Individual RC plots ────────────────────────────────────────
    plot_individual_branches(hucs, rc, recur)

    # ── Collect aggregated data ────────────────────────────────────
    log.info("Collecting data for aggregate plots …")
    master_df, metrics_df, zone_df, coverage_df = collect_all_data(hucs, rc, recur)

    n_gauges = metrics_df["location_id"].nunique() if not metrics_df.empty else 0
    log.info("  %d calibrated gauge(s) with metrics", n_gauges)

    # ── Aggregate plots ────────────────────────────────────────────
    log.info("=== Aggregate plots ===")
    plot_01_n_profiles(zone_df,     SUMMARY_DIR)
    plot_02_q_scatter(master_df,    SUMMARY_DIR)
    plot_03_adj_ratio(master_df,    SUMMARY_DIR)
    plot_04_n_by_zone(zone_df,      SUMMARY_DIR)
    plot_05_coverage(coverage_df,   SUMMARY_DIR)
    plot_06_metrics(metrics_df,     SUMMARY_DIR)
    plot_07_n_vs_slope(zone_df,     SUMMARY_DIR)
    plot_08_stage_shift(zone_df,    SUMMARY_DIR)
    plot_09_n_direction(zone_df,    SUMMARY_DIR)

    # ── SRC ensemble plots ─────────────────────────────────────────
    log.info("Collecting SRC curves for ensemble plots …")
    orig_curves, calib_curves = collect_src_curves(hucs, rc, recur)
    log.info("  %d SRC curves collected", len(orig_curves))
    plot_src_overlay(orig_curves, calib_curves, SUMMARY_DIR)

    # ── Metrics summary CSV ────────────────────────────────────────
    if not metrics_df.empty:
        cols_order = [
            "huc8", "bid", "location_id", "hydroid", "n_pts",
            "nse_orig", "nse_calib", "kge_orig", "kge_calib",
            "pbias_orig", "pbias_calib", "rmse_orig", "rmse_calib",
        ]
        metrics_df[cols_order].round(4).to_csv(
            SUMMARY_DIR / "metrics_summary.csv", index=False
        )
        log.info("  Saved: metrics_summary.csv")

        # Print summary
        log.info("\n── Performance Summary ──────────────────────────────────")
        for _, row in metrics_df.iterrows():
            log.info(
                "  %-10s %-13s  NSE %+.3f→%+.3f  KGE %+.3f→%+.3f  "
                "PBIAS %+.1f%%→%+.1f%%  RMSE %.1f→%.1f m³/s",
                row["huc8"], row["location_id"],
                row["nse_orig"], row["nse_calib"],
                row["kge_orig"], row["kge_calib"],
                row["pbias_orig"], row["pbias_calib"],
                row["rmse_orig"], row["rmse_calib"],
            )

    log.info("\nAll outputs written to:")
    log.info("  Individual plots → E:/SI/out/HUC{{huc8}}/src_plots/")
    log.info("  Aggregate plots  → %s", SUMMARY_DIR)


if __name__ == "__main__":
    main()
