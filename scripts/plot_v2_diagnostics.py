"""
Post-calibration diagnostics for s05 v2 (zone-n calibration).

Consumes the per-gauge ncalib_diagnostics.csv files written by s05 v2 plus the
calibrated / baseline SRCs, and produces figures that each answer one QC or
science question:

  v2_01_datum_gap.png       How big is the lidar bathymetry deficit (δ), and how
                            good are the PZF fits?  (CONUS-scaling metadata)
  v2_02_clip_rate.png       Acceptance test: % of n values pinned at the physical
                            bounds, per zone.  v1 reference = 28.6% at N_MIN.
  v2_03_n_profiles.png      Hypothesis-shape figure: calibrated n vs recurrence
                            zone, per gauge + median.  Does n vary with stage?
  v2_04_anchor_residuals.png  Does the calibrated SRC actually pass through its
                            anchors?  (Nonzero only where n was clipped.)
  v2_05_holdout_validation.png  Split-sample test: error at observed RC points
                            BETWEEN anchors (not used in calibration),
                            original vs calibrated.
  v2_06_starting_point.png  Origin match: SRC discharge at HAND stage 0 vs the
                            observed baseflow at the DEM water-surface elevation.
  v2_rc_panels/             One panel per gauge: observed RC, original SRC,
                            calibrated SRC, anchors, mode/δ annotation.

Run AFTER s05 v2 (any subset of HUCs is fine — it plots whatever diagnostics exist):
    .venv\\Scripts\\python.exe scripts/plot_v2_diagnostics.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

mpl.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10.5,
    "axes.labelsize": 12, "axes.titlesize": 11.5, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.20, "grid.linestyle": ":",
    "legend.fontsize": 9.5, "legend.framealpha": 0.92, "figure.dpi": 150,
})

C_ORIG  = "#2471a3"   # original SRC
C_CALIB = "#c0392b"   # calibrated SRC
C_OBS   = "#111111"   # USGS observed
C_GOOD  = "#187E86"
C_WARN  = "#C08A2D"

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH   = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
OUT_DIR      = Path("E:/SI/out")
DATA_DIR     = Path("data")
V2_DIR       = Path("E:/SI/out/calibration_analysis/v2_diagnostics")

N_MIN, N_MAX = 0.010, 0.300
RECURRENCE_YEARS = [2, 5, 10, 25, 50]
RECUR_COLS = {yr: f"{yr}_0_year_recurrence_flow_17C" for yr in RECURRENCE_YEARS}
V1_CLIP_LOW_PCT = 28.6          # v1 reference for the acceptance test
ANCHOR_EXCLUDE_M = 0.35         # hold-out: exclude RC points this close to an anchor
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# Data assembly
# ══════════════════════════════════════════════════════════════════

def load_shared():
    rc = pd.read_parquet(DATA_DIR / "usgs_rating_curves.parquet")
    rc["location_id"] = rc["location_id"].astype(str)
    rc["flow_cms"]    = rc["flow"].astype(float) / 35.3147
    rc["elev_m"]      = rc["elevation_navd88"].astype(float) / 3.28084
    recur = pd.read_parquet(DATA_DIR / "nwm3_17C_recurrence_flows_cfs.parquet")
    for yr, col in RECUR_COLS.items():
        recur[f"Q_{yr}yr_cms"] = recur[col].astype(float) * 0.028317
    recur["feature_id"] = recur["feature_id"].astype("Int64")
    return rc, recur


def load_diagnostics(hucs: list[str]) -> pd.DataFrame:
    frames = []
    for huc8 in hucs:
        p = OUT_DIR / f"HUC{huc8}" / "ncalib_diagnostics.csv"
        if p.exists():
            frames.append(pd.read_csv(p, dtype={"location_id": str, "branch": str}))
    if not frames:
        raise FileNotFoundError("No ncalib_diagnostics.csv found — run s05 v2 first.")
    diag = pd.concat(frames, ignore_index=True)
    diag["huc8"] = diag["huc8"].astype(str).str.zfill(8)
    log.info("Diagnostics: %d gauge records (%d calibrated) across %d HUCs",
             len(diag), (diag["status"] == "calibrated").sum(),
             diag["huc8"].nunique())
    return diag


def anchor_table(g: dict, rc: pd.DataFrame, recur: pd.DataFrame) -> list[tuple]:
    """[(yr, hand_stage, Q_r)] for one gauge — same math as s05."""
    grc = rc[rc["location_id"] == g["location_id"]].sort_values("flow_cms")
    grc = grc.drop_duplicates("flow_cms")
    feat = recur[recur["feature_id"] == g["feature_id"]]
    if grc.empty or feat.empty:
        return []
    out = []
    for yr in RECURRENCE_YEARS:
        Q_r = float(feat[f"Q_{yr}yr_cms"].iloc[0])
        if Q_r <= 0 or not (grc["flow_cms"].min() <= Q_r <= grc["flow_cms"].max()):
            continue
        elev_r = float(np.interp(Q_r, grc["flow_cms"], grc["elev_m"]))
        hand_r = elev_r - g["dem_adj_m"]
        if hand_r > 0:
            out.append((yr, hand_r, Q_r))
    return out


def gauge_records(diag: pd.DataFrame, rc: pd.DataFrame, recur: pd.DataFrame):
    """
    Yields, per calibrated gauge, everything the figures need:
    diagnostics row, anchors, orig/calib SRC slices, observed RC in HAND space.
    """
    for huc8, sub in diag[diag["status"] == "calibrated"].groupby("huc8"):
        elev_path = OUT_DIR / f"HUC{huc8}" / "usgs_elev_table.csv"
        if not elev_path.exists():
            continue
        elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
        elev["location_id"] = elev["location_id"].str.zfill(8)

        for bid, brows in sub.groupby("branch"):
            bdir = OUT_DIR / f"HUC{huc8}" / "watershed-data" / "branches" / str(bid)
            # hydroTable is the calibration's single source of truth:
            # current file = calibrated, .pre_n_calib backup = baseline.
            src_c_p = bdir / f"hydroTable_{bid}.csv"
            src_o_p = bdir / f"hydroTable_{bid}.pre_n_calib.csv"
            if not (src_c_p.exists() and src_o_p.exists()):
                continue
            _ren = {"stage": "Stage", "discharge_cms": "Discharge (m3s-1)"}
            src_c = pd.read_csv(src_c_p, low_memory=False).rename(columns=_ren)
            src_o = pd.read_csv(src_o_p, low_memory=False).rename(columns=_ren)
            src_c["HydroID"] = src_c["HydroID"].astype(int)
            src_o["HydroID"] = src_o["HydroID"].astype(int)

            for _, row in brows.iterrows():
                loc = str(row["location_id"]).zfill(8)
                erow = elev[(elev["location_id"] == loc) &
                            (elev["HydroID"] == row["hydroid"])]
                if erow.empty:
                    continue
                g = {
                    "huc8": huc8, "bid": str(bid),
                    "location_id": loc,
                    "hydroid": int(row["hydroid"]),
                    "feature_id": int(erow["feature_id"].iloc[0]),
                    "dem_adj_m": float(erow["dem_adj_elevation"].iloc[0]),
                    "diag": row,
                }
                anchors = anchor_table(g, rc, recur)
                if not anchors:
                    continue
                s_c = src_c[src_c["HydroID"] == g["hydroid"]].sort_values("Stage")
                s_o = src_o[src_o["HydroID"] == g["hydroid"]].sort_values("Stage")
                if s_c.empty or s_o.empty:
                    continue
                grc = rc[rc["location_id"] == loc].copy()
                grc["hand"] = grc["elev_m"] - g["dem_adj_m"]
                grc = grc[grc["hand"] > 0].sort_values("hand")
                yield g, anchors, s_o, s_c, grc


# ══════════════════════════════════════════════════════════════════
# Figures
# ══════════════════════════════════════════════════════════════════

def fig_datum_gap(diag: pd.DataFrame):
    d = diag[diag["status"] == "calibrated"]
    # mode names come from s05_ncalib_core.METHOD params; any wedge_* mode
    # carries a PZF-derived delta.
    is_wedge = d["mode"].astype(str).str.startswith(("wedge", "pzf"))
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6), constrained_layout=True)

    ax = axes[0]
    dd = d[is_wedge]["delta_m"].dropna()
    if len(dd):
        ax.hist(dd, bins=24, color=C_GOOD, alpha=0.85, edgecolor="white")
        ax.axvline(dd.median(), color="#111", lw=1.6, ls="--",
                   label=f"median δ = {dd.median():.2f} m")
        ax.legend()
    ax.set_xlabel("Datum gap δ  (m of channel invisible to lidar)")
    ax.set_ylabel("Gauges")
    ax.set_title("Bathymetry deficit recovered from PZF fits")

    ax = axes[1]
    counts = d["mode"].value_counts()
    labels = {"wedge_continuity": "continuity wedge\n(primary)",
              "wedge_topwidth":   "TopWidth wedge",
              "datum_shift":      "datum shift",
              "q0_offset":        "Q₀ offset\n(fallback)",
              "none":             "none"}
    order = [m for m in labels if m in counts.index]
    clrs  = {m: (C_GOOD if m.startswith(("wedge", "pzf")) else
                 C_WARN if m == "q0_offset" else "#999") for m in order}
    ax.bar(range(len(order)), [counts[m] for m in order],
           color=[clrs[m] for m in order], width=0.55)
    for i, m in enumerate(order):
        ax.text(i, counts[m], f" {counts[m]}", ha="center", va="bottom",
                fontsize=10, fontweight="bold")
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([labels[m] for m in order])
    ax.set_ylabel("Gauges")
    ax.set_title("Starting-point correction mode")

    ax = axes[2]
    dd = d[is_wedge]
    if len(dd):
        ax.scatter(dd["delta_m"], dd["pzf_b"], s=30, color=C_GOOD,
                   alpha=0.75, edgecolors="#333", linewidths=0.4)
    ax.axhspan(1.3, 2.7, color=C_GOOD, alpha=0.08)
    ax.text(0.98, 0.02, "shaded: typical natural-control range b ≈ 1.3–2.7",
            transform=ax.transAxes, ha="right", va="bottom",
            fontsize=8, color="#666", style="italic")
    ax.set_xlabel("Datum gap δ  (m)")
    ax.set_ylabel("PZF power-law exponent b")
    ax.set_title("Fit plausibility: δ vs hydraulic exponent")

    out = V2_DIR / "v2_01_datum_gap.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out.name)


def fig_clip_rate(diag: pd.DataFrame):
    d = diag[diag["status"] == "calibrated"]
    n_cols = [f"n_{yr}yr" for yr in RECURRENCE_YEARS]
    lo_pct, hi_pct, n_tot = [], [], []
    for c in n_cols:
        v = d[c].dropna()
        n_tot.append(len(v))
        lo_pct.append(100 * (v <= N_MIN + 1e-6).mean() if len(v) else 0)
        hi_pct.append(100 * (v >= N_MAX - 1e-6).mean() if len(v) else 0)

    x = np.arange(len(RECURRENCE_YEARS))
    fig, ax = plt.subplots(figsize=(10, 5.4), constrained_layout=True)
    ax.bar(x - 0.18, lo_pct, 0.34, color=C_CALIB, alpha=0.85,
           label=f"pinned at N_MIN = {N_MIN}")
    ax.bar(x + 0.18, hi_pct, 0.34, color=C_WARN, alpha=0.85,
           label=f"pinned at N_MAX = {N_MAX}")
    for xi, (lp, nt) in enumerate(zip(lo_pct, n_tot)):
        ax.text(xi - 0.18, lp, f"{lp:.0f}%", ha="center", va="bottom", fontsize=9)
    ax.axhline(V1_CLIP_LOW_PCT, color="#555", lw=1.6, ls="--")
    ax.text(len(x) - 0.5, V1_CLIP_LOW_PCT + 0.8,
            f"v1 (no datum correction): {V1_CLIP_LOW_PCT}% at N_MIN",
            ha="right", fontsize=9.5, color="#555")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{yr}-yr zone\n(n={nt})" for yr, nt in
                        zip(RECURRENCE_YEARS, n_tot)])
    ax.set_ylabel("% of calibrated n values at a physical bound")
    ax.set_title("Acceptance test — bound saturation after datum/bathymetry correction\n"
                 "(a pinned n means the anchor was NOT honored; the site needs review)")
    ax.legend()
    out = V2_DIR / "v2_02_clip_rate.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out.name)


def fig_n_profiles(diag: pd.DataFrame):
    d = diag[diag["status"] == "calibrated"]
    n_cols = [f"n_{yr}yr" for yr in RECURRENCE_YEARS]
    x = np.arange(len(RECURRENCE_YEARS))

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.6), constrained_layout=True)

    ax = axes[0]
    mat = d[n_cols].to_numpy(float)
    for row in mat:
        ax.plot(x, row, color="#777", lw=0.8, alpha=0.35)
    med = np.nanmedian(mat, axis=0)
    ax.plot(x, med, color=C_CALIB, lw=3.2, marker="o", ms=7, zorder=6,
            label="study-area median")
    ax.axhline(0.06, color=C_ORIG, ls="--", lw=1.4, label="OWP default n = 0.06")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{yr}-yr" for yr in RECURRENCE_YEARS])
    ax.set_xlabel("Recurrence zone (deeper →)")
    ax.set_ylabel("Calibrated Manning's n  (slice/band roughness)")
    ax.set_title("The hypothesis figure: n as a function of stage\n"
                 "(slice n = roughness of the flow added in each depth band; "
                 "one gray line per gauge)")
    ax.legend()

    ax = axes[1]
    mat_norm = mat / mat[:, [0]]
    for row in mat_norm:
        ax.plot(x, row, color="#777", lw=0.8, alpha=0.35)
    ax.plot(x, np.nanmedian(mat_norm, axis=0), color=C_CALIB, lw=3.2,
            marker="o", ms=7, zorder=6, label="median")
    ax.axhline(1.0, color="#555", ls=":", lw=1.4)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{yr}-yr" for yr in RECURRENCE_YEARS])
    ax.set_xlabel("Recurrence zone (deeper →)")
    ax.set_ylabel("n / n(2-yr)   — shape only")
    ax.set_title("Same profiles, normalized by the 2-yr value\n"
                 "(below 1.0 = smoother with depth; above = overbank roughening)")
    ax.legend()

    out = V2_DIR / "v2_03_n_profiles.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out.name)


def fig_anchor_residuals(records):
    rows = []
    for g, anchors, s_o, s_c, grc in records:
        st = s_c["Stage"].to_numpy(float)
        qc = s_c["Discharge (m3s-1)"].to_numpy(float)
        for yr, h_r, Q_r in anchors:
            q_at = float(np.interp(h_r, st, qc))
            rows.append({"yr": yr, "resid_pct": 100 * (q_at - Q_r) / Q_r,
                         "clipped": (g["diag"]["n_clipped_low"] > 0 or
                                     g["diag"]["n_clipped_high"] > 0)})
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    fig, ax = plt.subplots(figsize=(10, 5.4), constrained_layout=True)
    data, npts = [], []
    for yr in RECURRENCE_YEARS:
        v = df[df["yr"] == yr]["resid_pct"].to_numpy()
        data.append(v)
        npts.append(len(v))
    bp = ax.boxplot(data, patch_artist=True, showfliers=False,
                    medianprops=dict(color="#111", lw=2))
    for b in bp["boxes"]:
        b.set(facecolor=C_GOOD, alpha=0.35)
    for i, v in enumerate(data, 1):
        if len(v):
            ax.scatter(np.full(len(v), i) + np.random.uniform(-0.07, 0.07, len(v)),
                       v, s=22, color=C_GOOD, alpha=0.7,
                       edgecolors="#333", linewidths=0.4, zorder=5)
    ax.axhline(0, color="#555", lw=1.4, ls="--")
    ax.set_xticklabels([f"{yr}-yr\n(n={n})" for yr, n in zip(RECURRENCE_YEARS, npts)])
    # Clip the view so pinned-n outliers don't flatten the distribution;
    # annotate how many sit beyond the frame.
    y_lo, y_hi = -60, 150
    n_out = int((df["resid_pct"] > y_hi).sum() + (df["resid_pct"] < y_lo).sum())
    ax.set_ylim(y_lo, y_hi)
    if n_out:
        ax.text(0.98, 0.98, f"{n_out} outlier anchor(s) beyond ±frame\n"
                            "(pinned-n / datum-offset gauges — see diagnostics)",
                transform=ax.transAxes, ha="right", va="top",
                fontsize=9, color="#777", style="italic")
    ax.set_ylabel("Anchor residual  (Q_SRC − Q_r) / Q_r  ×100  [%]")
    ax.set_title("Does the calibrated SRC pass through its anchors?\n"
                 "(nonzero residuals occur only where n hit a physical bound "
                 "or a transition band overlaps the anchor)")
    out = V2_DIR / "v2_04_anchor_residuals.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out.name)
    return df


def fig_holdout(records):
    """Observed RC points between anchors were never used in calibration —
    error there is a genuine split-sample test."""
    rows = []
    for g, anchors, s_o, s_c, grc in records:
        if len(anchors) < 2 or grc.empty:
            continue
        h_lo = anchors[0][1]
        h_hi = anchors[-1][1]
        anchor_h = np.array([a[1] for a in anchors])
        pts = grc[(grc["hand"] > h_lo) & (grc["hand"] < h_hi)]
        st_o = s_o["Stage"].to_numpy(float); q_o = s_o["Discharge (m3s-1)"].to_numpy(float)
        st_c = s_c["Stage"].to_numpy(float); q_c = s_c["Discharge (m3s-1)"].to_numpy(float)
        for _, p in pts.iterrows():
            h, q_obs = float(p["hand"]), float(p["flow_cms"])
            if q_obs <= 0 or np.min(np.abs(anchor_h - h)) < ANCHOR_EXCLUDE_M:
                continue
            rows.append({
                "gauge": g["location_id"],
                "err_orig":  100 * (float(np.interp(h, st_o, q_o)) - q_obs) / q_obs,
                "err_calib": 100 * (float(np.interp(h, st_c, q_c)) - q_obs) / q_obs,
            })
    df = pd.DataFrame(rows)
    if df.empty:
        log.warning("Hold-out figure skipped — no between-anchor RC points found")
        return df

    per_g = df.groupby("gauge").agg(
        mae_orig=("err_orig", lambda v: np.mean(np.abs(v))),
        mae_calib=("err_calib", lambda v: np.mean(np.abs(v))),
        n_pts=("err_orig", "size"),
    ).reset_index()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.8), constrained_layout=True)

    ax = axes[0]
    lim = float(np.nanpercentile(
        np.concatenate([per_g["mae_orig"], per_g["mae_calib"]]), 98)) * 1.15
    lim = max(lim, 20)
    ax.scatter(per_g["mae_orig"], per_g["mae_calib"], s=34, color=C_CALIB,
               alpha=0.75, edgecolors="#333", linewidths=0.4)
    ax.plot([0, lim], [0, lim], color="#555", lw=1.3, ls="--")
    ax.set_xlim(0, lim); ax.set_ylim(0, lim)
    better = (per_g["mae_calib"] < per_g["mae_orig"]).mean() * 100
    ax.text(0.03, 0.97, f"{better:.0f}% of gauges improved\n(points below the 1:1 line)",
            transform=ax.transAxes, va="top", fontsize=10,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.85))
    ax.set_xlabel("MAE at held-out RC points — ORIGINAL SRC  [%]")
    ax.set_ylabel("MAE at held-out RC points — CALIBRATED  [%]")
    ax.set_title("Split-sample test, per gauge\n"
                 "(observed points between anchors; never used in calibration)")

    ax = axes[1]
    bins = np.linspace(-100, 100, 41)
    ax.hist(df["err_orig"].clip(-100, 100), bins=bins, color=C_ORIG, alpha=0.55,
            label=f"original (median {df['err_orig'].median():+.0f}%)")
    ax.hist(df["err_calib"].clip(-100, 100), bins=bins, color=C_CALIB, alpha=0.55,
            label=f"calibrated (median {df['err_calib'].median():+.0f}%)")
    ax.axvline(0, color="#333", lw=1.3)
    ax.set_xlabel("Discharge error at held-out RC points  [%]  (clipped at ±100)")
    ax.set_ylabel("RC points")
    ax.set_title("Pooled error distribution — all held-out points")
    ax.legend()

    out = V2_DIR / "v2_05_holdout_validation.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out.name)
    return df


def fig_starting_point(records, rc: pd.DataFrame):
    rows = []
    for g, anchors, s_o, s_c, grc in records:
        grc_all = rc[rc["location_id"] == g["location_id"]].sort_values("elev_m")
        if grc_all.empty:
            continue
        q_obs0 = float(np.interp(g["dem_adj_m"],
                                 grc_all["elev_m"], grc_all["flow_cms"]))
        s0 = s_c[s_c["Stage"] == 0.0]
        q_src0 = float(s0["Discharge (m3s-1)"].iloc[0]) if not s0.empty else np.nan
        rows.append({"gauge": g["location_id"], "q_obs0": q_obs0, "q_src0": q_src0,
                     "mode": g["diag"]["mode"]})
    df = pd.DataFrame(rows).dropna()
    if df.empty:
        return

    fig, ax = plt.subplots(figsize=(7.4, 6.6), constrained_layout=True)
    lim = max(df[["q_obs0", "q_src0"]].to_numpy().max() * 1.15, 1.0)
    is_wedge = df["mode"].astype(str).str.startswith(("wedge", "pzf"))
    for mask, clr, lbl in ((is_wedge, C_GOOD, "wedge (PZF δ)"),
                           (~is_wedge, C_WARN, "Q₀ offset / other")):
        sub = df[mask]
        if len(sub):
            ax.scatter(sub["q_obs0"], sub["q_src0"], s=40, color=clr, alpha=0.8,
                       edgecolors="#333", linewidths=0.4, label=lbl)
    ax.plot([0.1, lim], [0.1, lim], color="#555", lw=1.3, ls="--", label="1:1")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlim(0.1, lim); ax.set_ylim(0.1, lim)
    ax.set_xlabel("Observed baseflow at DEM water-surface elevation  (m³/s)")
    ax.set_ylabel("Calibrated SRC discharge at HAND stage 0  (m³/s)")
    ax.set_title("Do the curves now start in the same place?\n"
                 "(v1 forced every SRC to start at zero)")
    ax.legend()
    out = V2_DIR / "v2_06_starting_point.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out.name)


def fig_rc_panels(records):
    panel_dir = V2_DIR / "v2_rc_panels"
    panel_dir.mkdir(parents=True, exist_ok=True)
    n_done = 0
    for g, anchors, s_o, s_c, grc in records:
        fig, ax = plt.subplots(figsize=(9.5, 6.4), constrained_layout=True)

        h_max = max(a[1] for a in anchors) * 1.6
        q_max = max(a[2] for a in anchors) * 1.8

        for i, (yr, h_r, _) in enumerate(anchors):
            ax.axhspan(0 if i == 0 else anchors[i - 1][1], h_r,
                       color="#187E86", alpha=0.045 if i % 2 == 0 else 0.09)
        if not grc.empty:
            ax.plot(grc["flow_cms"], grc["hand"], color=C_OBS, lw=2.0,
                    label="USGS observed RC", zorder=5)
        ax.plot(s_o["Discharge (m3s-1)"], s_o["Stage"], color=C_ORIG, lw=1.8,
                label="Original SRC (n = 0.06)", zorder=4)
        ax.plot(s_c["Discharge (m3s-1)"], s_c["Stage"], color=C_CALIB, lw=2.6,
                ls="--", label="Calibrated SRC (v2)", zorder=6)
        for yr, h_r, Q_r in anchors:
            ax.scatter([Q_r], [h_r], s=52, color=C_CALIB, edgecolors="white",
                       linewidths=1.4, zorder=8)
            ax.annotate(f"{yr}-yr", (Q_r, h_r), textcoords="offset points",
                        xytext=(7, -3), fontsize=8.5, color=C_CALIB,
                        fontweight="bold")

        d = g["diag"]
        mode_txt = {"pzf_wedge": f"PZF wedge  δ = {d['delta_m']:.2f} m  (r² = {d['pzf_r2']:.3f})",
                    "q0_offset": f"baseflow offset  Q₀ = {d['q0_cms']:.1f} m³/s"}.get(
                        d["mode"], d["mode"])
        n_txt = "   ".join(f"n({yr}) = {d[f'n_{yr}yr']:.3f}"
                           for yr in RECURRENCE_YEARS
                           if pd.notna(d.get(f"n_{yr}yr")))
        ax.set_title(f"Gauge {g['location_id']}  ·  HydroID {g['hydroid']}  ·  "
                     f"HUC {g['huc8']}\n{mode_txt}\n{n_txt}", fontsize=10)
        ax.set_xlim(0, q_max)
        ax.set_ylim(0, h_max)
        ax.set_xlabel("Discharge  (m³/s)")
        ax.set_ylabel("HAND stage  (m)")
        ax.legend(loc="lower right")

        out = panel_dir / f"{g['huc8']}_{g['location_id']}.png"
        fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        n_done += 1
    log.info("Saved: %d RC panels → %s", n_done, panel_dir)


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main():
    V2_DIR.mkdir(parents=True, exist_ok=True)
    hucs = [str(int(c)).zfill(8)
            for c in pd.read_excel(EXCEL_PATH)[HUC_CODE_COL]]

    rc, recur = load_shared()
    diag = load_diagnostics(hucs)

    # Aggregate figures straight from diagnostics
    fig_datum_gap(diag)
    fig_clip_rate(diag)
    fig_n_profiles(diag)

    # Figures that need the SRC curves — assemble once, reuse
    records = list(gauge_records(diag, rc, recur))
    log.info("Gauge records with SRC + anchors: %d", len(records))
    fig_anchor_residuals(records)
    holdout = fig_holdout(records)
    fig_starting_point(records, rc)
    fig_rc_panels(records)

    if not holdout.empty:
        holdout.to_csv(V2_DIR / "holdout_errors.csv", index=False)
    log.info("Done → %s", V2_DIR)


if __name__ == "__main__":
    main()
