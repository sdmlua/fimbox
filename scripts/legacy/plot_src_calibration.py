"""
Publication / PPT-ready calibration comparison plots.

Each subplot shows:
  • Coloured horizontal bands  — the 6 piecewise-n zones
  • White box label (right)    — zone range + calibrated Manning's n
  • Blue (thick)               — original SRC for the gauged HydroID
  • Red (thick)                — calibrated SRC for the same HydroID
  • Green dashed               — USGS rating curve in HAND coordinates
  • Diamond + straight arrow   — NWM recurrence-Q points (cascade-staggered)
  • Grey step curve (top axis) — Manning's n as a function of HAND stage

x-axis: USGS RC Q at 1.5 × h_50yr  (~100-yr view)
y-axis: 1.5 × h_50yr HAND stage
Top x-axis: Manning's n  (step function across zones, semi-transparent)

HAND stage = height above thalweg (dem.tif elevation sampled at snapped gauge).

Run:
    .venv\\Scripts\\python.exe scripts/plot_src_calibration.py
"""
from __future__ import annotations
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# ── Publication style ─────────────────────────────────────────────
mpl.rcParams.update({
    "font.family":         "DejaVu Sans",
    "font.size":           11,
    "axes.labelsize":      13,
    "axes.titlesize":      12,
    "xtick.labelsize":     11,
    "ytick.labelsize":     11,
    "legend.fontsize":     10.5,
    "legend.framealpha":   0.93,
    "legend.edgecolor":    "#cccccc",
    "axes.spines.top":     False,
    "axes.spines.right":   False,
    "axes.grid":           True,
    "grid.alpha":          0.28,
    "grid.linestyle":      ":",
})

# ── CONFIG ────────────────────────────────────────────────────────
HUC8     = "03020102"
OUT_DIR  = Path("D:/SI/out")
DATA_DIR = Path("data")
# ─────────────────────────────────────────────────────────────────

AOI_ROOT  = OUT_DIR / f"HUC{HUC8}"
WATERSHED = AOI_ROOT / "watershed-data"
BRANCHES  = WATERSHED / "branches"
SAVE_TO   = AOI_ROOT / "src_plots"

RECURRENCE_YEARS = [2, 5, 10, 25, 50]

ZONE_COLOURS = ["#aed6f1", "#a9dfbf", "#fad7a0", "#f9e79f", "#f1948a", "#d2b4de"]
ZONE_ALPHA   = 0.40


# ── Loaders ───────────────────────────────────────────────────────

def load_data():
    elev = pd.read_csv(
        AOI_ROOT / "usgs_elev_table.csv",
        dtype={"location_id": str, "feature_id": "Int64"},
    )
    rc = pd.read_parquet(DATA_DIR / "usgs_rating_curves.parquet")
    rc["flow_cms"] = rc["flow"].astype(float) / 35.3147
    rc["elev_m"]   = rc["elevation_navd88"].astype(float) / 3.28084
    recur = pd.read_parquet(DATA_DIR / "nwm3_17C_recurrence_flows_cfs.parquet")
    for yr in RECURRENCE_YEARS:
        recur[f"Q_{yr}yr_cms"] = (
            recur[f"{yr}_0_year_recurrence_flow_17C"].astype(float) * 0.028317
        )
    return elev, rc, recur


def load_src(bid: str, original: bool) -> pd.DataFrame:
    branch_dir = BRANCHES / bid
    fname = f"src_full_crosswalked_{bid}"
    path  = branch_dir / (fname + (".pre_n_calib.csv" if original else ".csv"))
    if original and not path.exists():
        path = branch_dir / f"{fname}.csv"
    return pd.read_csv(path, low_memory=False)


def find_calibrated_branches() -> list[str]:
    return [
        bd.name for bd in sorted(BRANCHES.iterdir())
        if bd.is_dir()
        and (bd / f"src_full_crosswalked_{bd.name}.pre_n_calib.csv").exists()
    ]


# ── Zone helpers ──────────────────────────────────────────────────

def zone_pts(loc_id, feature_id, dem_adj_m, rc_all, recur):
    loc = loc_id.zfill(8)
    grc = rc_all[rc_all["location_id"] == loc].sort_values("flow_cms")
    if grc.empty:
        return []
    feat = recur[recur["feature_id"] == feature_id]
    if feat.empty:
        return []
    pts = []
    for yr in RECURRENCE_YEARS:
        Q = float(feat[f"Q_{yr}yr_cms"].iloc[0])
        if Q < grc["flow_cms"].min() or Q > grc["flow_cms"].max():
            continue
        elev = float(np.interp(Q, grc["flow_cms"].values, grc["elev_m"].values))
        h    = elev - dem_adj_m
        if h > 0:
            pts.append((yr, h, Q))
    return sorted(pts, key=lambda x: x[0])


def back_calc_n(hydroid, src_df, zp):
    hdf   = src_df[src_df["HydroID"] == hydroid].sort_values("Stage")
    if hdf.empty:
        return {}
    slope = float(hdf["SLOPE"].iloc[0])
    if slope <= 0:
        return {}
    out = {}
    for yr, h, Q in zp:
        idx = (hdf["Stage"] - h).abs().idxmin()
        row = hdf.loc[idx]
        A   = float(row.get("WetArea (m2)", 0.0))
        R   = float(row.get("HydraulicRadius (m)", 0.0))
        if A > 0 and R > 0 and Q > 0:
            out[yr] = float(np.clip(A * (R ** (2 / 3)) * slope**0.5 / Q, 0.01, 0.30))
    return out


# ── n-step function for secondary axis ───────────────────────────

def n_step_curve(zp, n_dict, y_max):
    """
    Returns (n_vals, h_vals) describing a horizontal step function:
    n is constant within each zone, steps at zone boundaries.
    Suitable for plotting on a twiny() axis (n on x, HAND on y).
    """
    yrs    = [yr for yr, _, _ in zp]
    n_arr  = [n_dict.get(yr, 0.06) for yr in yrs]
    n_arr.append(n_dict.get(yrs[-1], 0.06) if yrs else 0.06)   # >50yr zone

    h_brk  = [0.0] + [h for _, h, _ in zp]
    if h_brk[-1] < y_max:
        h_brk.append(y_max)

    nx, hy = [], []
    for i, (h_lo, h_hi) in enumerate(zip(h_brk[:-1], h_brk[1:])):
        n_v = n_arr[i]
        nx.extend([n_v, n_v])
        hy.extend([h_lo, h_hi])
        if i < len(h_brk) - 2:       # horizontal jump at zone boundary
            nx.append(n_arr[i + 1])
            hy.append(h_hi)

    return nx, hy


# ── Main subplot drawing ──────────────────────────────────────────

def draw_gauge(ax, bid, loc_id, hydroid, feature_id, dem_adj_m,
               src_orig, src_calib, rc_all, recur, branch_avg):

    zp     = zone_pts(loc_id, feature_id, dem_adj_m, rc_all, recur)
    n_dict = back_calc_n(hydroid, src_orig, zp)

    loc_pad = loc_id.zfill(8)
    grc = rc_all[rc_all["location_id"] == loc_pad].sort_values("flow_cms").copy()
    grc["hand"] = grc["elev_m"] - dem_adj_m
    grc = grc[grc["hand"] > 0]

    # ── Axis limits (~100-yr view) ──
    h_max_yr = max((h for _, h, _ in zp), default=5.0)
    y_max    = max(h_max_yr * 1.50, 3.0)

    target_elev = dem_adj_m + y_max
    rc_raw  = rc_all[rc_all["location_id"] == loc_pad].sort_values("elev_m")
    rc_below = rc_raw[rc_raw["elev_m"] <= target_elev]
    rc_q_max = float(rc_below["flow_cms"].max()) if not rc_below.empty else 0.0
    x_max    = max(rc_q_max, max((Q for _, _, Q in zp), default=50) * 1.4) * 1.08

    go_h = src_orig[src_orig["HydroID"]  == hydroid].sort_values("Stage")
    gc_h = src_calib[src_calib["HydroID"] == hydroid].sort_values("Stage")

    # ── Zone shading ──
    h_edges = [0.0] + [h for _, h, _ in zp]
    if h_edges[-1] < y_max:
        h_edges.append(y_max)

    for i, (h_lo, h_hi) in enumerate(zip(h_edges[:-1], h_edges[1:])):
        col = ZONE_COLOURS[min(i, len(ZONE_COLOURS) - 1)]
        ax.axhspan(h_lo, min(h_hi, y_max), color=col, alpha=0.28, zorder=0, lw=0)

    # ── Zone transition lines + return period labels (right-aligned, staggered) ──
    min_lbl_sep = y_max * 0.07
    prev_lbl_h  = -1.0
    for yr, h, _ in zp:
        ax.axhline(h, color="#777", ls=":", lw=1.0, alpha=0.75, zorder=1)
        lh = max(h, prev_lbl_h + min_lbl_sep)
        lh = min(lh, y_max * 0.96)
        ax.text(x_max * 0.988, lh, f"{yr} yr",
                ha="right", va="bottom", fontsize=9.0, color="#555",
                fontweight="bold", zorder=10)
        prev_lbl_h = lh

    # ── USGS RC — solid dark reference line ──
    if not grc.empty:
        rc_plt = grc[grc["hand"] <= y_max * 1.02]
        ax.plot(rc_plt["flow_cms"], rc_plt["hand"],
                color="#222222", lw=2.8, ls="-", zorder=5,
                label=f"USGS RC")

    # ── Recurrence markers — small circles on USGS RC, no text ──
    for _, h, Q in zp:
        ax.scatter(Q, h, color="#222222", s=50, zorder=8, marker="o",
                   edgecolors="white", linewidths=0.9)

    # ── SRC curves — original thin solid, calibrated thick dashed ──
    if not go_h.empty:
        ax.plot(go_h["Discharge (m3s-1)"], go_h["Stage"],
                color="#2471a3", lw=1.8, ls="-", zorder=4,
                label=f"Original SRC")
    if not gc_h.empty:
        ax.plot(gc_h["Discharge (m3s-1)"], gc_h["Stage"],
                color="#c0392b", lw=3.0, ls="--", zorder=5,
                label="Calibrated SRC")
    # Legend proxy for recurrence markers
    ax.scatter([], [], color="#222222", s=50, marker="o",
               edgecolors="white", linewidths=0.9,
               label="Recurrence interval")

    # ── Secondary top x-axis: Manning's n step function ──
    ax_n = ax.twiny()
    nx, hy = n_step_curve(zp, n_dict, y_max)
    ax_n.plot(nx, hy, color="#444", alpha=0.38, lw=2.2, ls="-", zorder=3)
    ax_n.fill_betweenx(hy, 0, nx, color="#888", alpha=0.07, zorder=2)

    all_n = [v for v in n_dict.values() if v is not None]
    n_ax_max = max(max(all_n) * 1.45, 0.35) if all_n else 0.40
    ax_n.set_xlim(0, n_ax_max)
    ax_n.set_xlabel("Manning's  n  →", fontsize=10, color="#555", labelpad=5)
    ax_n.tick_params(axis="x", colors="#666", labelsize=9)
    # Make sure the top spine is drawn (overriding the rcParam default)
    for sp in ["right", "left", "bottom"]:
        ax_n.spines[sp].set_visible(False)
    ax_n.spines["top"].set_visible(True)
    ax_n.spines["top"].set_color("#aaa")
    ax_n.spines["top"].set_linewidth(0.9)

    # ── Main axis cosmetics ──
    ax.set_xlim(0, x_max)
    ax.set_ylim(0, y_max)
    ax.set_xlabel("Discharge  (m³/s)", labelpad=8)
    ax.set_ylabel("Stage  (m)   [height above thalweg]", labelpad=8)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    title = f"Branch {bid}   ·   Gauge {loc_id}   ·   HydroID {hydroid}"
    if branch_avg:
        title += "\n(branch 0 : branch-wide average n of both gauges applied)"
    ax.set_title(title, fontweight="bold", pad=28)   # pad leaves room for top axis
    ax.legend(loc="upper left")


# ── Main ──────────────────────────────────────────────────────────

def main():
    SAVE_TO.mkdir(parents=True, exist_ok=True)
    elev, rc_all, recur = load_data()

    for bid in find_calibrated_branches():
        branch_gauges = elev[elev["levpa_id"].astype(str) == str(bid)].copy()
        branch_gauges = branch_gauges[
            branch_gauges["location_id"].apply(
                lambda loc: not rc_all[rc_all["location_id"] == str(loc).zfill(8)].empty
            )
        ]
        if branch_gauges.empty:
            print(f"Branch {bid}: no RC data — skip")
            continue

        src_orig  = load_src(bid, original=True)
        src_calib = load_src(bid, original=False)
        n_g       = len(branch_gauges)

        fig, axes = plt.subplots(
            1, n_g,
            figsize=(11 * n_g, 8.5),
            squeeze=False,
            constrained_layout=True,
        )

        for col, (_, g) in enumerate(branch_gauges.iterrows()):
            draw_gauge(
                axes[0, col], bid,
                str(g["location_id"]),
                int(g["HydroID"]),
                int(g["feature_id"]),
                float(g["dem_adj_elevation"]),
                src_orig, src_calib, rc_all, recur,
                branch_avg=(bid == "0"),
            )

        n_reaches = src_orig["HydroID"].nunique()
        fig.suptitle(
            f"Manning's  n  Recurrence Calibration     Branch {bid}     HUC8 {HUC8}\n"
            f"{n_reaches} reaches  ·  {n_g} USGS gauge(s)  ·  "
            f"x-axis: ~100-yr discharge  ·  top axis: n vs HAND stage",
            fontsize=12, fontweight="bold",
        )

        out = SAVE_TO / f"src_calib_branch_{bid}.png"
        fig.savefig(out, dpi=200, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"  Saved: {out}")

    print(f"\nDone -> {SAVE_TO}")


if __name__ == "__main__":
    main()
