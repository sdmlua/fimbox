"""
SRC ensemble plots — clean, physically informative version.

Produces three figures in E:/SI/out/calibration_analysis/:

  src_ensemble_sidebyside.png
      Two panels side-by-side: original SRCs (left) and calibrated SRCs (right).
      Individual gray spaghetti lines + bold colored median. Same axis limits
      on both panels. No percentile bands.

  src_ensemble_absolute.png
      Both ensembles overlaid on the same axes (log x-scale).
      Individual spaghetti lines (light) + bold study-area median per ensemble.

  src_ensemble_normalized.png
      Both ensembles normalized by each gauge's NWM 2-year recurrence flow
      (Q / Q_2yr), removing the river-size effect.

Run:
    .venv\\Scripts\\python.exe scripts/plot_src_ensemble.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

# ── Style ─────────────────────────────────────────────────────────
mpl.rcParams.update({
    "font.family":        "DejaVu Sans",
    "font.size":          11,
    "axes.labelsize":     13,
    "axes.titlesize":     12,
    "axes.titleweight":   "bold",
    "axes.spines.top":    False,
    "axes.spines.right":  False,
    "axes.grid":          True,
    "grid.alpha":         0.20,
    "grid.linestyle":     ":",
    "legend.fontsize":    10,
    "legend.framealpha":  0.92,
    "figure.dpi":         150,
})

C_ORIG  = "#2471a3"   # blue  — original SRC
C_CALIB = "#c0392b"   # red   — calibrated SRC
C_USGS  = "#111111"   # black — USGS observed RC

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
# SECTION 1 — Data helpers
# ══════════════════════════════════════════════════════════════════

def _load_shared():
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
    if not p.exists() and original:
        p = branch_dir / f"src_full_crosswalked_{bid}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, low_memory=False)
    df["HydroID"] = df["HydroID"].astype(int)
    return df


def _discover_calibrated(huc8: str, rc: pd.DataFrame, elev: pd.DataFrame) -> list[dict]:
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
        if not (branch_dir / f"src_full_crosswalked_{bid}.pre_n_calib.csv").exists():
            continue
        for _, gage in elev[elev["levpa_id"].astype(str) == bid].iterrows():
            loc_id = str(gage["location_id"]).zfill(8)
            if loc_id not in rc_ids or pd.isna(gage.get("feature_id")):
                continue
            results.append({
                "bid":        bid,
                "location_id": loc_id,
                "hydroid":    int(gage["HydroID"]),
                "feature_id": int(gage["feature_id"]),
                "dem_adj_m":  float(gage["dem_adj_elevation"]),
                "branch_dir": branch_dir,
            })
    return results


def collect_curves(hucs: list[str], rc: pd.DataFrame, recur: pd.DataFrame):
    """
    Returns (orig, calib, usgs_rc) — three parallel lists of dicts:

      q     : ndarray  discharge (m³/s)
      h     : ndarray  HAND stage (m)
      Q_2yr : float    NWM 2-yr recurrence flow (m³/s) — normalization factor
    """
    orig, calib, usgs_rc = [], [], []

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
            bdir   = gage_list[0]["branch_dir"]
            src_o  = _load_src(bdir, bid, original=True)
            src_c  = _load_src(bdir, bid, original=False)
            if src_o is None or src_c is None:
                continue

            for g in gage_list:
                feat = recur[recur["feature_id"] == g["feature_id"]]
                if feat.empty:
                    continue
                Q_2yr = float(feat["Q_2yr_cms"].iloc[0])
                if Q_2yr <= 0:
                    continue

                hid = g["hydroid"]
                s_o = src_o[src_o["HydroID"] == hid].sort_values("Stage")
                s_c = src_c[src_c["HydroID"] == hid].sort_values("Stage")
                if s_o.empty or s_c.empty:
                    continue

                base = {"Q_2yr": Q_2yr}
                orig.append({**base,
                             "q": s_o["Discharge (m3s-1)"].values.copy(),
                             "h": s_o["Stage"].values.copy()})
                calib.append({**base,
                              "q": s_c["Discharge (m3s-1)"].values.copy(),
                              "h": s_c["Stage"].values.copy()})

                # USGS RC converted to HAND space
                grc = rc[rc["location_id"] == g["location_id"]].copy()
                grc["hand"] = grc["elev_m"] - g["dem_adj_m"]
                grc = grc[grc["hand"] > 0].sort_values("hand")
                if not grc.empty:
                    usgs_rc.append({**base,
                                    "q": grc["flow_cms"].values.copy(),
                                    "h": grc["hand"].values.copy()})

    log.info("  %d SRC pairs | %d USGS RC sets", len(orig), len(usgs_rc))
    return orig, calib, usgs_rc


# ══════════════════════════════════════════════════════════════════
# SECTION 2 — Ensemble median
# ══════════════════════════════════════════════════════════════════

def _median_on_grid(curves: list[dict], h_grid: np.ndarray,
                    normalized: bool) -> np.ndarray:
    """
    Interpolate each curve onto h_grid, return nanmedian across all curves.
    If normalized=True, divide each curve's discharge by its Q_2yr first.
    """
    q_mat = np.full((len(curves), len(h_grid)), np.nan)
    for i, c in enumerate(curves):
        q = c["q"] / c["Q_2yr"] if normalized else c["q"]
        valid = c["h"] > 0
        if valid.sum() < 2:
            continue
        q_mat[i] = np.interp(h_grid, c["h"][valid], q[valid],
                              left=np.nan, right=np.nan)
    return np.nanmedian(q_mat, axis=0)


# ══════════════════════════════════════════════════════════════════
# SECTION 3 — Plot 1: Side-by-side panels (original | calibrated)
# ══════════════════════════════════════════════════════════════════

def plot_sidebyside(orig: list, calib: list, out_path: Path) -> None:
    """
    Two panels with the same axes: original SRCs on the left, calibrated on
    the right.  Individual curves are drawn in gray (neutral — each represents
    a different-sized river) with a bold colored median.  No percentile bands.
    X-axis is clipped at the 98th percentile of discharge so the majority of
    curves are visible without outliers dominating the scale.
    """
    # Shared axis limits from all curves combined
    all_q = np.concatenate([c["q"] for c in orig + calib])
    all_h = np.concatenate([c["h"] for c in orig + calib])
    x_max = float(np.nanpercentile(all_q[all_q > 0], 98)) * 1.05
    y_max = float(np.nanpercentile(all_h, 99)) * 1.05

    h_grid = np.linspace(0.05, y_max, 600)
    med_o  = _median_on_grid(orig,  h_grid, normalized=False)
    med_c  = _median_on_grid(calib, h_grid, normalized=False)

    fig, axes = plt.subplots(1, 2, figsize=(16, 8),
                             sharey=True, constrained_layout=True)

    panels = [
        (axes[0], orig,  med_o,  C_ORIG,  "Original SRCs  (pre-calibration)"),
        (axes[1], calib, med_c,  C_CALIB, "Calibrated SRCs  (post-calibration)"),
    ]

    for ax, curves, med, color, title in panels:
        # Individual gray spaghetti
        for c in curves:
            mask = (c["q"] > 0) & (c["q"] <= x_max * 1.05)
            ax.plot(c["q"][mask], c["h"][mask],
                    color="#777", lw=0.6, alpha=0.28, zorder=2)
        # Median
        valid = (med > 0) & (med <= x_max)
        ax.plot(med[valid], h_grid[valid],
                color=color, lw=3.0, zorder=6, label="Median")

        ax.set_xlim(0, x_max)
        ax.set_ylim(0, y_max)
        ax.set_xlabel("Discharge  (m³/s)", labelpad=8)
        ax.set_title(f"{title}\n(n = {len(curves)} reaches)", pad=8)
        ax.legend(loc="upper left", fontsize=10)
        ax.xaxis.set_major_formatter(
            plt.matplotlib.ticker.FuncFormatter(lambda x, _: f"{x:,.0f}")
        )

    axes[0].set_ylabel("HAND Stage  (m above thalweg)", labelpad=8)

    fig.suptitle("Synthetic Rating Curve Ensemble  —  Original vs Calibrated",
                 fontsize=13, fontweight="bold")

    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out_path.name)


# ══════════════════════════════════════════════════════════════════
# SECTION 4 — Plot 2: Absolute, log x-scale
# ══════════════════════════════════════════════════════════════════

def plot_absolute(orig: list, calib: list, out_path: Path) -> None:
    """
    Original and calibrated SRCs on the same axes.

    Log x-scale lets small creeks and large rivers coexist without the large
    rivers dominating the entire visible area.  No percentile bands — they
    are not interpretable when river sizes span 3-4 orders of magnitude.
    The median lines summarise the typical shift from calibration.
    """
    fig, ax = plt.subplots(figsize=(13, 8), constrained_layout=True)

    # ── Individual spaghetti ──────────────────────────────────────
    for c in orig:
        mask = c["q"] > 0
        ax.plot(c["q"][mask], c["h"][mask],
                color=C_ORIG,  lw=0.55, alpha=0.18, zorder=2)
    for c in calib:
        mask = c["q"] > 0
        ax.plot(c["q"][mask], c["h"][mask],
                color=C_CALIB, lw=0.55, alpha=0.18, zorder=2)

    # ── Medians ───────────────────────────────────────────────────
    h_max  = float(max(c["h"].max() for c in orig + calib))
    h_grid = np.linspace(0.05, h_max, 600)
    med_o  = _median_on_grid(orig,  h_grid, normalized=False)
    med_c  = _median_on_grid(calib, h_grid, normalized=False)

    ax.plot(med_o[med_o > 0],  h_grid[med_o > 0],
            color=C_ORIG,  lw=3.0, zorder=6, label="Original  — median")
    ax.plot(med_c[med_c > 0],  h_grid[med_c > 0],
            color=C_CALIB, lw=3.0, ls="--", zorder=6, label="Calibrated — median")

    # ── Axes ──────────────────────────────────────────────────────
    q_pos = np.concatenate([c["q"][c["q"] > 0] for c in orig + calib])
    ax.set_xscale("log")
    ax.set_xlim(max(float(np.nanpercentile(q_pos, 0.5)), 0.1), None)
    ax.set_ylim(0, h_max)
    ax.set_xlabel("Discharge  (m³/s)  —  log scale", labelpad=8)
    ax.set_ylabel("HAND Stage  (m above thalweg)", labelpad=8)
    ax.set_title(
        "SRC Ensemble  —  Original vs Calibrated\n"
        f"n = {len(orig)} gauged HydroIDs  |  log x-scale so all river sizes are visible",
        pad=10,
    )

    handles = [
        Line2D([0], [0], color=C_ORIG,  lw=3.0,            label=f"Original — median  (n = {len(orig)})"),
        Line2D([0], [0], color=C_CALIB, lw=3.0, ls="--",   label=f"Calibrated — median  (n = {len(calib)})"),
        Line2D([0], [0], color="#888",  lw=0.8, alpha=0.5,  label="Individual reaches"),
    ]
    ax.legend(handles=handles, loc="upper left")
    ax.text(0.98, 0.02,
            "Median shift LEFT → calibration raises stage for same discharge  |  "
            "RIGHT → calibration lowers stage",
            transform=ax.transAxes, ha="right", va="bottom",
            fontsize=8.5, color="#666", style="italic")

    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out_path.name)


# ══════════════════════════════════════════════════════════════════
# SECTION 5 — Plot 3: Normalized by Q_2yr
# ══════════════════════════════════════════════════════════════════

def plot_normalized(orig: list, calib: list, usgs_rc: list,
                    out_path: Path) -> None:
    """
    Discharge normalized by each gauge's NWM 2-yr recurrence flow.

    Q / Q_2yr = 1.0  →  2-yr flood (common flood)
    Q / Q_2yr = 2    →  roughly 5-yr flood
    Q / Q_2yr = 5    →  roughly 25-yr flood

    Removing river-size effect means the SHAPE of the SRC and the calibration
    SHIFT are directly comparable across all reaches.

    Key property: by construction, calibrated SRCs cross Q/Q_2yr = 1.0 at
    exactly the HAND stage where the USGS RC hits the 2-yr NWM flow.
    """
    fig, ax = plt.subplots(figsize=(13, 8), constrained_layout=True)

    # Reasonable x-axis limit: 98th %ile of normalized Q across all curves
    all_qn = np.concatenate([c["q"] / c["Q_2yr"]
                              for c in orig + calib if c["Q_2yr"] > 0])
    x_max = max(float(np.nanpercentile(all_qn[all_qn > 0], 98)) * 1.1, 6.0)

    def _clip(q_norm, h):
        mask = (q_norm > 0) & (q_norm <= x_max * 1.05) & (h > 0)
        return q_norm[mask], h[mask]

    # ── USGS RC (plotted first, behind SRC curves) ────────────────
    for c in usgs_rc:
        qn = c["q"] / c["Q_2yr"]
        qn_c, h_c = _clip(qn, c["h"])
        if len(h_c) < 2:
            continue
        ax.plot(qn_c, h_c, color=C_USGS, lw=0.8, alpha=0.15, zorder=1)

    # ── Individual SRC curves ─────────────────────────────────────
    for c in orig:
        qn = c["q"] / c["Q_2yr"]
        qn_c, h_c = _clip(qn, c["h"])
        ax.plot(qn_c, h_c, color=C_ORIG,  lw=0.55, alpha=0.18, zorder=2)
    for c in calib:
        qn = c["q"] / c["Q_2yr"]
        qn_c, h_c = _clip(qn, c["h"])
        ax.plot(qn_c, h_c, color=C_CALIB, lw=0.55, alpha=0.18, zorder=2)

    # ── Medians ───────────────────────────────────────────────────
    h_max  = min(float(max(c["h"].max() for c in orig + calib)), 20.0)
    h_grid = np.linspace(0.05, h_max, 600)
    med_o  = _median_on_grid(orig,  h_grid, normalized=True)
    med_c  = _median_on_grid(calib, h_grid, normalized=True)

    valid_o = (med_o  > 0) & (med_o  <= x_max)
    valid_c = (med_c  > 0) & (med_c  <= x_max)
    ax.plot(med_o[valid_o],  h_grid[valid_o],
            color=C_ORIG,  lw=3.0, zorder=6, label="Original — median")
    ax.plot(med_c[valid_c],  h_grid[valid_c],
            color=C_CALIB, lw=3.0, ls="--", zorder=6, label="Calibrated — median")

    # ── Reference line at Q/Q_2yr = 1 ────────────────────────────
    ax.axvline(1.0, color="#555", ls=":", lw=1.8, alpha=0.8, zorder=5)
    ax.text(1.03, h_max * 0.97, "Q = Q₂ yr",
            ha="left", va="top", fontsize=9.5, color="#555", style="italic")

    # ── Axes ──────────────────────────────────────────────────────
    ax.set_xlim(0, x_max)
    ax.set_ylim(0, h_max)
    ax.set_xlabel("Normalized Discharge  Q / Q₂ᵧᵣ  (dimensionless)", labelpad=8)
    ax.set_ylabel("HAND Stage  (m above thalweg)", labelpad=8)
    ax.set_title(
        "SRC Ensemble  —  Normalized by 2-Year Recurrence Flow\n"
        "River-size effect removed  ·  shape and calibration shift are directly comparable",
        pad=10,
    )

    handles = [
        Line2D([0], [0], color=C_ORIG,  lw=3.0,           label=f"Original SRC — median  (n = {len(orig)})"),
        Line2D([0], [0], color=C_CALIB, lw=3.0, ls="--",  label=f"Calibrated SRC — median  (n = {len(calib)})"),
        Line2D([0], [0], color=C_USGS,  lw=1.0, alpha=0.5, label=f"USGS observed RC  (n = {len(usgs_rc)})"),
        Line2D([0], [0], color="#888",  lw=0.8, alpha=0.5, label="Individual reaches"),
    ]
    ax.legend(handles=handles, loc="upper left")
    ax.text(0.98, 0.02,
            "Calibrated SRCs cross Q/Q₂ᵧᵣ = 1.0 at the 2-yr HAND stage by construction",
            transform=ax.transAxes, ha="right", va="bottom",
            fontsize=8.5, color="#666", style="italic")

    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("Saved: %s", out_path.name)


# ══════════════════════════════════════════════════════════════════
# SECTION 6 — Main
# ══════════════════════════════════════════════════════════════════

def main():
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)

    df   = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    log.info("Study area: %d HUCs", len(hucs))

    log.info("Loading shared tables …")
    rc, recur = _load_shared()

    log.info("Collecting SRC curves …")
    orig, calib, usgs_rc = collect_curves(hucs, rc, recur)

    log.info("Plot 1 — side-by-side panels (original | calibrated) …")
    plot_sidebyside(orig, calib,
                    SUMMARY_DIR / "src_ensemble_sidebyside.png")

    log.info("Plot 2 — absolute ensemble overlaid (log scale) …")
    plot_absolute(orig, calib,
                  SUMMARY_DIR / "src_ensemble_absolute.png")

    log.info("Plot 3 — normalized ensemble (Q / Q_2yr) …")
    plot_normalized(orig, calib, usgs_rc,
                    SUMMARY_DIR / "src_ensemble_normalized.png")

    log.info("Done. Outputs → %s", SUMMARY_DIR)


if __name__ == "__main__":
    main()
