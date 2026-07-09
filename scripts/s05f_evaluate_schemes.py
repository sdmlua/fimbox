"""
Application-scheme evaluation: whole-section ("total") vs slice-composite
("incremental") piecewise-constant n — both under method B3.

OFFLINE — reads baseline hydroTable .pre_n_calib backups, writes NOTHING to
the pipeline outputs.  All results go to a separate folder:

    E:/SI/out/calibration_analysis/scheme_evaluation/
        scheme_comparison.csv     summary metrics per scheme
        per_gauge_schemes.csv     per-gauge detail (n per zone, both schemes)
        scheme_comparison.png     pinning / hold-out / smoothness bars
        n_semantics.png           median n per zone — the interpretation shift
        example_curves/*.png      per-gauge overlays: observed RC + baseline +
                                  total-scheme + incremental-scheme curves

The two schemes
---------------
total        n_i = √S·K(h_i)/Q_i applied to the WHOLE section within zone i
             (± transition band + cumulative-max guard).  n_i = effective
             whole-section roughness at the anchor stage.
incremental  n_i = √S·ΔK_i/ΔQ_i applied to the depth SLICE between anchors
             (Lotter/divided-channel composite).  Continuous + monotone by
             construction; n_i = roughness of the flow added in that band.

Run:
    .venv\\Scripts\\python.exe scripts/s05f_evaluate_schemes.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import s05_ncalib_core as core

mpl.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10.5,
    "axes.titlesize": 11.5, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.20, "grid.linestyle": ":",
    "figure.dpi": 150,
})

EVAL_DIR = core.OUT_DIR / "calibration_analysis" / "scheme_evaluation"
RY = core.RECURRENCE_YEARS
ANCHOR_EXCL = 0.35
C_TOT, C_INC, C_OBS, C_BASE = "#7f8c8d", "#187E86", "#111111", "#2471a3"

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")


def curve_total(stages, K, slope, bounds, n_zones, q_offset):
    """Replicates the batch 'total' application: transitions + cummax."""
    n_arr = core.n_profile(stages, bounds, n_zones)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = np.where(n_arr > 0, K * (slope ** 0.5) / n_arr, 0.0) + q_offset
    return np.maximum.accumulate(q), n_arr


def smoothness(stages, q, bounds):
    """(shelf_rows, max_jump_ratio) within the anchor span."""
    ah = np.sort([h for h, _ in bounds.values()])
    m = (stages >= ah.min()) & (stages <= ah.max() + 0.62)
    qs = q[m]
    if len(qs) < 3:
        return 0, 1.0
    dq = np.diff(qs)
    shelves = int((dq <= 1e-9).sum())
    pos = qs[:-1] > 0
    ratio = float((qs[1:][pos] / qs[:-1][pos]).max()) if pos.any() else 1.0
    return shelves, ratio


def main():
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "example_curves").mkdir(exist_ok=True)
    rc, recur = core.load_shared()

    agg = {s: {"n": [], "lo": 0, "hi": 0, "ho": [], "shelf": [], "jump": [],
               "resid": []} for s in ("total", "incremental")}
    per_zone = {s: {yr: [] for yr in RY} for s in ("total", "incremental")}
    gauge_rows, examples = [], []
    n_gauges = 0

    for huc8 in core.study_hucs():
        aoi = core.OUT_DIR / f"HUC{huc8}"
        elev_path = aoi / "usgs_elev_table.csv"
        branches = aoi / "watershed-data" / "branches"
        if not elev_path.exists() or not branches.exists():
            continue
        elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
        elev["location_id"] = elev["location_id"].str.zfill(8)
        rc_ids = set(rc["location_id"])
        non_trunk = elev[elev["levpa_id"].astype(str) != "0"]
        gauged_bids = set(
            non_trunk[non_trunk["location_id"].isin(rc_ids)]["levpa_id"].astype(str))

        for bid in sorted(gauged_bids):
            bk = branches / bid / f"hydroTable_{bid}.pre_n_calib.csv"
            if not bk.exists():
                bk = branches / bid / f"hydroTable_{bid}.csv"
            if not bk.exists():
                continue
            ht = pd.read_csv(bk, low_memory=False)
            ht["HydroID"] = ht["HydroID"].astype(int)
            seen = set()
            for _, gage in elev[elev["levpa_id"].astype(str) == bid].iterrows():
                loc = str(gage["location_id"]).zfill(8)
                if loc not in rc_ids or pd.isna(gage["feature_id"]):
                    continue
                hydroid = int(gage["HydroID"])
                if hydroid in seen:
                    continue
                dem = float(gage["dem_adj_elevation"])
                gage_rc = rc[rc["location_id"] == loc]
                bounds = core.compute_zone_boundaries(
                    loc, int(gage["feature_id"]), dem, rc, recur)
                if not bounds or len(bounds) < 2:
                    continue
                hdf = ht[ht["HydroID"] == hydroid].sort_values("stage")
                if hdf.empty:
                    continue
                slope = float(hdf["SLOPE"].iloc[0])
                if slope <= 0:
                    continue
                pzf = core.fit_pzf(gage_rc, dem)
                if pzf is None:
                    continue
                Q0 = core.baseflow_q0(gage_rc, dem)
                params = core.method_params("B3", hdf, slope, pzf, Q0)
                stages, K = core.build_geometry(hdf, params["delta"], params["w0"])
                qoff = params["q_offset"]

                nz_t, lo_t, hi_t = core.calibrate_n_zones(
                    stages, K, slope, bounds, qoff, 0.0)
                nz_i, lo_i, hi_i = core.calibrate_n_zones_incremental(
                    stages, K, slope, bounds, qoff)
                if not nz_t or not nz_i:
                    continue
                q_t, _ = curve_total(stages, K, slope, bounds, nz_t, qoff)
                q_i, _ = core.discharge_incremental(
                    stages, K, slope, bounds, nz_i, qoff)

                grc = gage_rc.sort_values("flow_cms").drop_duplicates("flow_cms")
                hand = (grc["elev_m"] - dem).to_numpy(float)
                qobs = grc["flow_cms"].to_numpy(float)
                ah = np.sort([h for h, _ in bounds.values()])

                row = {"huc8": huc8, "location_id": loc, "hydroid": hydroid}
                for tag, nz, lo, hi, q in (
                        ("total", nz_t, lo_t, hi_t, q_t),
                        ("incremental", nz_i, lo_i, hi_i, q_i)):
                    a = agg[tag]
                    a["n"] += list(nz.values())
                    a["lo"] += lo
                    a["hi"] += hi
                    for yr, n in nz.items():
                        per_zone[tag][yr].append(n)
                        row[f"n_{yr}yr_{tag[:3]}"] = round(n, 4)
                    sh, jp = smoothness(stages, q, bounds)
                    a["shelf"].append(sh)
                    a["jump"].append(jp)
                    for h_o, q_o in zip(hand, qobs):
                        if not (ah.min() < h_o < ah.max()) or q_o <= 0:
                            continue
                        if np.abs(ah - h_o).min() < ANCHOR_EXCL:
                            continue
                        a["ho"].append(
                            100 * (float(np.interp(h_o, stages, q)) - q_o) / q_o)
                    for yr, (h_r, Q_r) in bounds.items():
                        a["resid"].append(
                            100 * (float(np.interp(h_r, stages, q)) - Q_r) / Q_r)
                gauge_rows.append(row)
                seen.add(hydroid)
                n_gauges += 1
                sh_t, _ = smoothness(stages, q_t, bounds)
                examples.append((sh_t, huc8, loc, hydroid, stages, q_t, q_i,
                                 hdf["discharge_cms"].to_numpy(float),
                                 hand, qobs, bounds))

    # ── summary table ─────────────────────────────────────────────
    rows = []
    for s in ("total", "incremental"):
        a = agg[s]
        nv = np.array(a["n"]); ho = np.array(a["ho"]); rs = np.array(a["resid"])
        rows.append({
            "scheme": s, "anchors": len(nv),
            "pinned_NMAX_pct": round(100 * a["hi"] / max(len(nv), 1), 1),
            "pinned_NMIN_pct": round(100 * a["lo"] / max(len(nv), 1), 1),
            "median_n": round(float(np.median(nv)), 3),
            "n_in_lit_pct": round(100 * ((nv >= 0.02) & (nv <= 0.20)).mean(), 1),
            "holdout_med_err_pct": round(float(np.median(ho)), 1),
            "holdout_med_abs_err_pct": round(float(np.median(np.abs(ho))), 1),
            "anchor_med_abs_resid_pct": round(float(np.median(np.abs(rs))), 2),
            "gauges_with_shelves_pct": round(
                100 * (np.array(a["shelf"]) > 0).mean(), 1),
            "median_shelf_rows": int(np.median(a["shelf"])),
            "p90_max_jump_ratio": round(float(np.percentile(a["jump"], 90)), 2),
        })
    table = pd.DataFrame(rows)
    print("\n" + table.to_string(index=False))
    table.to_csv(EVAL_DIR / "scheme_comparison.csv", index=False)
    pd.DataFrame(gauge_rows).to_csv(EVAL_DIR / "per_gauge_schemes.csv", index=False)

    # ── figure 1: bars ────────────────────────────────────────────
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), constrained_layout=True)
    x = np.arange(2); labels = ["total\n(whole-section)", "incremental\n(slice)"]
    clrs = [C_TOT, C_INC]

    ax = axes[0]
    ax.bar(x - 0.18, table["pinned_NMAX_pct"], 0.34, color=clrs, alpha=0.9, label="at N_MAX")
    ax.bar(x + 0.18, table["pinned_NMIN_pct"], 0.34, color=clrs, alpha=0.45, label="at N_MIN")
    for xi in x:
        ax.text(xi - 0.18, table["pinned_NMAX_pct"][xi], f"{table['pinned_NMAX_pct'][xi]:.0f}",
                ha="center", va="bottom", fontsize=9.5)
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylabel("% of n pinned at a bound"); ax.legend(fontsize=9)
    ax.set_title("Bound saturation")

    ax = axes[1]
    ax.bar(x - 0.18, table["holdout_med_abs_err_pct"], 0.34, color=clrs, alpha=0.9,
           label="hold-out median |err| %")
    ax.bar(x + 0.18, table["anchor_med_abs_resid_pct"], 0.34, color=clrs, alpha=0.45,
           label="anchor median |resid| %")
    for xi in x:
        ax.text(xi - 0.18, table["holdout_med_abs_err_pct"][xi],
                f"{table['holdout_med_abs_err_pct'][xi]:.0f}", ha="center", va="bottom", fontsize=9.5)
    ax.set_xticks(x); ax.set_xticklabels(labels); ax.legend(fontsize=9)
    ax.set_title("Accuracy (lower = better)")

    ax = axes[2]
    ax.bar(x - 0.18, table["gauges_with_shelves_pct"], 0.34, color=clrs, alpha=0.9,
           label="% gauges with flat shelves")
    ax.bar(x + 0.18, (table["p90_max_jump_ratio"] - 1) * 100, 0.34, color=clrs, alpha=0.45,
           label="p90 max row-jump  (ratio−1)×100")
    for xi in x:
        ax.text(xi - 0.18, table["gauges_with_shelves_pct"][xi],
                f"{table['gauges_with_shelves_pct'][xi]:.0f}", ha="center", va="bottom", fontsize=9.5)
    ax.set_xticks(x); ax.set_xticklabels(labels); ax.legend(fontsize=9)
    ax.set_title("Smoothness — shelves & jumps (lower = better)")

    fig.suptitle(f"Application-scheme comparison under method B3 — {n_gauges} gauges",
                 fontweight="bold")
    fig.savefig(EVAL_DIR / "scheme_comparison.png", dpi=150,
                bbox_inches="tight", facecolor="white")
    plt.close(fig)

    # ── figure 2: n semantics per zone ────────────────────────────
    fig, ax = plt.subplots(figsize=(9, 5.4), constrained_layout=True)
    xz = np.arange(len(RY))
    for s, clr, mk in (("total", C_TOT, "o"), ("incremental", C_INC, "s")):
        med = [np.median(per_zone[s][yr]) if per_zone[s][yr] else np.nan for yr in RY]
        ax.plot(xz, med, color=clr, lw=2.6, marker=mk, ms=8,
                label=f"{s} — median n per zone")
    ax.axhline(0.06, color=C_BASE, ls="--", lw=1.3, label="OWP default 0.06")
    ax.set_xticks(xz); ax.set_xticklabels([f"{yr}-yr" for yr in RY])
    ax.set_ylabel("Median calibrated n")
    ax.set_title("What n MEANS differs between schemes\n"
                 "total = effective whole-section n at the anchor;  "
                 "incremental = roughness of the flow added in that depth band")
    ax.legend()
    fig.savefig(EVAL_DIR / "n_semantics.png", dpi=150,
                bbox_inches="tight", facecolor="white")
    plt.close(fig)

    # ── figure 3: example curves (worst-shelf gauges + a smooth one) ──
    examples.sort(key=lambda e: -e[0])
    picks = examples[:3] + [examples[-1]]
    for sh_t, huc8, loc, hydroid, stages, q_t, q_i, q_b, hand, qobs, bounds in picks:
        fig, ax = plt.subplots(figsize=(9.5, 6.2), constrained_layout=True)
        h_max = max(h for h, _ in bounds.values()) * 1.5
        q_max = max(Q for _, Q in bounds.values()) * 1.7
        ok = hand > 0
        ax.plot(qobs[ok], hand[ok], color=C_OBS, lw=2.0, label="USGS observed RC", zorder=5)
        ax.plot(q_b, stages, color=C_BASE, lw=1.5, label="Baseline SRC (n = 0.06)", zorder=3)
        ax.plot(q_t, stages, color=C_TOT, lw=2.2, label="total scheme (shelves possible)", zorder=4)
        ax.plot(q_i, stages, color=C_INC, lw=2.6, ls="--",
                label="incremental scheme (smooth by construction)", zorder=6)
        for yr, (h_r, Q_r) in sorted(bounds.items()):
            ax.scatter([Q_r], [h_r], s=50, color="#c0392b", edgecolors="white",
                       linewidths=1.3, zorder=8)
            ax.annotate(f"{yr}-yr", (Q_r, h_r), textcoords="offset points",
                        xytext=(7, -3), fontsize=8.5, color="#c0392b", fontweight="bold")
        ax.set_xlim(0, q_max); ax.set_ylim(0, h_max)
        ax.set_xlabel("Discharge (m³/s)"); ax.set_ylabel("HAND stage (m)")
        ax.set_title(f"Gauge {loc} · HydroID {hydroid} · HUC {huc8}  "
                     f"({sh_t} shelf rows under total scheme)")
        ax.legend(loc="lower right", fontsize=9)
        fig.savefig(EVAL_DIR / "example_curves" / f"{huc8}_{loc}.png",
                    dpi=140, bbox_inches="tight", facecolor="white")
        plt.close(fig)

    log.info("Done → %s  (%d gauges compared)", EVAL_DIR, n_gauges)


if __name__ == "__main__":
    main()
