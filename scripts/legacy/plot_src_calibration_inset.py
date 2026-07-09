"""
Calibration comparison plots — variant with bottom-right inset for n vs HAND stage.

Identical to plot_src_calibration.py except:
  • No secondary top x-axis (twiny).
  • Bottom-right inset axes showing Manning's n as a step function vs HAND stage,
    with the same zone colouring as the main plot.

Outputs saved as  src_calib_inset_branch_{bid}.png  (distinct from the top-axis variant).

Run:
    .venv\\Scripts\\python.exe scripts/plot_src_calibration_inset.py
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


def _src_q_at_h(h, src_hid):
    if src_hid.empty:
        return 0.0
    return float(np.interp(
        h, src_hid["Stage"].values, src_hid["Discharge (m3s-1)"].values,
        left=0.0, right=float(src_hid["Discharge (m3s-1)"].max()),
    ))


def n_step_curve(zp, n_dict, y_max):
    """(n_vals, h_vals) for a horizontal step function: n on x, HAND stage on y."""
    yrs   = [yr for yr, _, _ in zp]
    n_arr = [n_dict.get(yr, 0.06) for yr in yrs]
    n_arr.append(n_dict.get(yrs[-1], 0.06) if yrs else 0.06)

    h_brk = [0.0] + [h for _, h, _ in zp]
    if h_brk[-1] < y_max:
        h_brk.append(y_max)

    nx, hy = [], []
    for i, (h_lo, h_hi) in enumerate(zip(h_brk[:-1], h_brk[1:])):
        n_v = n_arr[i]
        nx.extend([n_v, n_v])
        hy.extend([h_lo, h_hi])
        if i < len(h_brk) - 2:
            nx.append(n_arr[i + 1])
            hy.append(h_hi)

    return nx, hy


# ── Bottom-right inset: n vs HAND stage ───────────────────────────

def draw_n_inset(ax, zp, n_dict, y_max):
    """
    Adds a small inset in the bottom-right corner of ax showing Manning's n
    as a piecewise step function vs HAND stage.  Zone colours match the main plot.
    The bottom-right corner is typically empty in a stage-discharge plot because
    discharge increases with stage, leaving high-Q / low-h space unused.
    """
    inset = ax.inset_axes([0.68, 0.04, 0.27, 0.33])   # [x0, y0, w, h] in axes fraction

    yrs   = [yr for yr, _, _ in zp]
    h_brk = [0.0] + [h for _, h, _ in zp]
    if h_brk[-1] < y_max:
        h_brk.append(y_max)
    n_arr = [n_dict.get(yr, 0.06) for yr in yrs]
    n_arr.append(n_dict.get(yrs[-1], 0.06) if yrs else 0.06)

    all_n = [v for v in n_arr if v is not None]
    if not all_n:
        return

    n_lo  = 0.0
    n_hi  = max(all_n) * 1.45

    # ── Zone-coloured horizontal bands ──
    for i, (h_lo, h_hi) in enumerate(zip(h_brk[:-1], h_brk[1:])):
        col   = ZONE_COLOURS[min(i, len(ZONE_COLOURS) - 1)]
        h_hi_c = min(h_hi, y_max)
        inset.axhspan(h_lo, h_hi_c, color=col, alpha=0.55, lw=0, zorder=0)

    # ── Step function line ──
    nx, hy = n_step_curve(zp, n_dict, y_max)
    inset.plot(nx, hy, color="#1a1a1a", lw=2.4, zorder=4, solid_capstyle="butt")

    # ── n value label centred in each zone band ──
    for i, (h_lo, h_hi) in enumerate(zip(h_brk[:-1], h_brk[1:])):
        n_v   = n_arr[i]
        h_mid = (h_lo + min(h_hi, y_max)) / 2
        if h_mid <= y_max and n_v is not None:
            # Place text just to the right of the step line, near the right edge
            txt_x = n_v + (n_hi - n_v) * 0.20
            txt_x = min(txt_x, n_hi * 0.95)
            inset.text(
                txt_x, h_mid, f"{n_v:.3f}",
                ha="left", va="center", fontsize=7.5,
                color="#1a1a1a", fontweight="bold",
            )

    # ── Zone boundary dotted lines ──
    for _, h, _ in zp:
        inset.axhline(h, color="#888", ls=":", lw=0.8, alpha=0.7, zorder=2)

    # ── Axes cosmetics ──
    inset.set_xlim(n_lo, n_hi)
    inset.set_ylim(0, y_max)
    inset.set_xlabel("Manning's  n", fontsize=8.5, labelpad=3, color="#333")
    inset.set_ylabel("h (m)", fontsize=8.5, labelpad=3, color="#333")
    inset.tick_params(labelsize=7.5, colors="#555", length=3)
    inset.xaxis.set_major_locator(mticker.MaxNLocator(3, prune="both"))
    inset.yaxis.set_major_locator(mticker.MaxNLocator(4, prune="both"))
    inset.set_title("n  vs  stage", fontsize=8.5, pad=4, color="#333",
                    fontweight="bold")

    # White semi-transparent background so it floats above main plot
    inset.patch.set_facecolor("white")
    inset.patch.set_alpha(0.88)
    for sp in ["top", "right"]:
        inset.spines[sp].set_visible(False)
    for sp in ["bottom", "left"]:
        inset.spines[sp].set_color("#999")
        inset.spines[sp].set_linewidth(0.8)
    inset.grid(True, alpha=0.18, color="#aaa", linestyle=":")

    # Thin border box around the whole inset
    for sp in inset.spines.values():
        sp.set_linewidth(0.8)


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
    rc_raw   = rc_all[rc_all["location_id"] == loc_pad].sort_values("elev_m")
    rc_below = rc_raw[rc_raw["elev_m"] <= target_elev]
    rc_q_max = float(rc_below["flow_cms"].max()) if not rc_below.empty else 0.0
    x_max    = max(rc_q_max, max((Q for _, _, Q in zp), default=50) * 1.4) * 1.08

    go_h = src_orig[src_orig["HydroID"]  == hydroid].sort_values("Stage")
    gc_h = src_calib[src_calib["HydroID"] == hydroid].sort_values("Stage")

    # ── Zone shading + right-side labels ──
    yrs_list = [yr for yr, _, _ in zp]
    h_edges  = [0.0] + [h for _, h, _ in zp]
    if h_edges[-1] < y_max:
        h_edges.append(y_max)

    prev = 0
    zone_labels = []
    for yr in yrs_list:
        zone_labels.append(f"{prev}-{yr} yr")
        prev = yr
    zone_labels.append(f"> {prev} yr")

    n_per_zone = [n_dict.get(yr) for yr in yrs_list]
    n_per_zone.append(n_dict.get(yrs_list[-1]) if yrs_list else None)

    # Inset occupies axes fraction [0.68, 0.04, 0.27, 0.33]:
    #   x: 0.68–0.95 of x_max    y: 0.04–0.37 of y_max
    # Lower zone labels are placed LEFT of the inset; upper zone labels go right
    # and are staggered into two columns when they stack too close vertically.
    INSET_Y_TOP  = 0.37   # inset top edge as fraction of y_max
    INSET_X_LEFT = 0.68   # inset left edge as fraction of x_max

    prev_upper_h  = -1.0   # last placed upper-zone label h_mid
    stagger_right = True   # alternates column when upper labels are compressed

    for i, (h_lo, h_hi) in enumerate(zip(h_edges[:-1], h_edges[1:])):
        h_hi_c = min(h_hi, y_max)
        col    = ZONE_COLOURS[min(i, len(ZONE_COLOURS) - 1)]
        ax.axhspan(h_lo, h_hi_c, color=col, alpha=ZONE_ALPHA, zorder=0, lw=0)

        h_mid     = (h_lo + h_hi_c) / 2
        q_max_src = max(_src_q_at_h(h_mid, go_h), _src_q_at_h(h_mid, gc_h))

        if h_mid < INSET_Y_TOP * y_max:
            # Lower zone: keep label LEFT of inset so they never overlap
            lx = float(np.clip(
                max(q_max_src * 2.0, x_max * 0.26),
                q_max_src * 1.4,
                INSET_X_LEFT * x_max * 0.86,   # cap well below inset left edge
            ))
            prev_upper_h  = -1.0   # reset upper stagger when we re-enter lower zones
            stagger_right = True
        else:
            # Upper zone: right side, stagger into two columns when compressed
            if prev_upper_h > 0 and abs(h_mid - prev_upper_h) < 0.12 * y_max:
                if stagger_right:
                    lx = float(np.clip(max(q_max_src * 1.25, x_max * 0.74),
                                       0, x_max * 0.95))
                    stagger_right = False
                else:
                    lx = float(np.clip(max(q_max_src * 1.25, x_max * 0.55),
                                       0, x_max * 0.73))
                    stagger_right = True
            else:
                lx = float(np.clip(max(q_max_src * 1.25, x_max * 0.65),
                                   0, x_max * 0.95))
                stagger_right = True   # next close label starts in right column
            prev_upper_h = h_mid

        n_v = n_per_zone[i]
        lbl = zone_labels[i] + (f"\nn = {n_v:.3f}" if n_v is not None else "")
        ax.text(lx, h_mid, lbl,
                ha="center", va="center", fontsize=9.5, color="#222",
                bbox=dict(boxstyle="round,pad=0.35", fc="white",
                          ec=col, linewidth=1.4, alpha=0.95),
                zorder=9)

    for _, h, _ in zp:
        ax.axhline(h, color="#888", ls=":", lw=0.9, alpha=0.60, zorder=1)

    # ── USGS RC ──
    if not grc.empty:
        rc_plt = grc[grc["hand"] <= y_max * 1.02]
        ax.plot(rc_plt["flow_cms"], rc_plt["hand"],
                color="#1a7a4a", lw=2.5, ls="--", zorder=5,
                label=f"USGS RC  (gauge {loc_id})")

    # ── Recurrence markers — cascade stagger to prevent label overlap ──
    MIN_SEP_FRAC = 0.09
    label_ys = []

    for yr, h, Q in zp:
        ly = h
        while any(abs(ly - py) < MIN_SEP_FRAC * y_max for py in label_ys):
            ly += MIN_SEP_FRAC * y_max * 0.55
        ly = min(ly, y_max * 0.97)
        label_ys.append(ly)

        ax.scatter(Q, h, color="#1a7a4a", s=110, zorder=7, marker="D")

        lx = Q + x_max * 0.04
        needs_arrow = abs(ly - h) > y_max * 0.04

        if needs_arrow:
            ax.annotate(
                f"{yr} yr",
                xy=(Q, h),
                xytext=(lx, ly),
                arrowprops=dict(arrowstyle="->", color="#1a7a4a",
                                lw=0.85, connectionstyle="arc3,rad=0.0"),
                fontsize=9.5, color="#1a7a4a", fontweight="bold",
                ha="left", va="center", zorder=8,
            )
        else:
            ax.text(lx, ly, f"{yr} yr",
                    fontsize=9.5, color="#1a7a4a", fontweight="bold",
                    ha="left", va="center", zorder=8)

    # ── SRC curves ──
    if not go_h.empty:
        ax.plot(go_h["Discharge (m3s-1)"], go_h["Stage"],
                color="#2471a3", lw=3.0, zorder=4,
                label=f"Original SRC  (HydroID {hydroid})")
    if not gc_h.empty:
        ax.plot(gc_h["Discharge (m3s-1)"], gc_h["Stage"],
                color="#c0392b", lw=3.0, zorder=4,
                label="Calibrated SRC")

    # ── Bottom-right inset: n vs HAND stage ──
    draw_n_inset(ax, zp, n_dict, y_max)

    # ── Main axis cosmetics ──
    ax.set_xlim(0, x_max)
    ax.set_ylim(0, y_max)
    ax.set_xlabel("Discharge  (m3/s)", labelpad=8)
    ax.set_ylabel("HAND Stage  (m)   [height above thalweg]", labelpad=8)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    title = f"Branch {bid}   -   Gauge {loc_id}   -   HydroID {hydroid}"
    if branch_avg:
        title += "\n(branch 0 : branch-wide average n of both gauges applied)"
    ax.set_title(title, fontweight="bold", pad=10)
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
            print(f"Branch {bid}: no RC data - skip")
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
            f"{n_reaches} reaches  -  {n_g} USGS gauge(s)  -  "
            f"x-axis: ~100-yr discharge  -  inset: n vs HAND stage",
            fontsize=12, fontweight="bold",
        )

        out = SAVE_TO / f"src_calib_inset_branch_{bid}.png"
        fig.savefig(out, dpi=200, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"  Saved: {out}")

    print(f"\nDone -> {SAVE_TO}")


if __name__ == "__main__":
    main()
