"""
Starting-point method evaluation — the justification for choosing B3.

Runs all four calibration methods (A, B1, B2, B3 — see s05_ncalib_core.py)
OFFLINE against identical inputs at every PZF-fittable gauge.  Nothing on disk
is modified: baseline geometry is read from the hydroTable .pre_n_calib backup
(or the current file where no backup exists).

Decision criteria
-----------------
All methods fix the curve fit about equally (n absorbs whatever the geometry
correction gets wrong), so curve fit alone cannot pick a winner.  The methods
are scored on whether the calibrated n stays PHYSICAL — which is what the
research hypothesis tests and what the future ML model trains on:

  1. bound saturation      % of n pinned at N_MIN / N_MAX (pinned = the anchor
                           was NOT honored and the label is censored)
  2. literature range      % of n within 0.02–0.20 (Chow 1959 natural channels)
  3. hold-out error        median |error| at observed RC points BETWEEN anchors
                           (never used in calibration — a true split sample)
  4. origin match          median Q_src(origin) / Q_obs(baseflow) — 1.0 is perfect

Outputs → E:/SI/out/calibration_analysis/method_evaluation/
  method_comparison.csv    the full table
  method_comparison.png    the figure
  and a printed verdict.

Run:
    .venv\\Scripts\\python.exe scripts/s05e_evaluate_methods.py
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

EVAL_DIR   = core.OUT_DIR / "calibration_analysis" / "method_evaluation"
METHODS    = ["A", "B1", "B2", "B3"]
M_LABEL    = {"A": "A · wedge (TopWidth w0)", "B1": "B1 · datum shift",
              "B2": "B2 · Q0 offset", "B3": "B3 · wedge (continuity w0)"}
M_CLR      = {"A": "#7f8c8d", "B1": "#b0632e", "B2": "#2471a3", "B3": "#187E86"}
ANCHOR_EXCL = 0.35
LIT_RANGE   = (0.02, 0.20)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")


def _baseline_ht(branch_dir: Path, bid: str) -> pd.DataFrame | None:
    """Baseline hydroTable — prefer the .pre_n_calib backup (pre-calibration)."""
    p = branch_dir / f"hydroTable_{bid}.pre_n_calib.csv"
    if not p.exists():
        p = branch_dir / f"hydroTable_{bid}.csv"
    if not p.exists():
        return None
    ht = pd.read_csv(p, low_memory=False)
    ht["HydroID"] = ht["HydroID"].astype(int)
    return ht


def evaluate_gauge(method: str, hdf: pd.DataFrame, slope: float,
                   bounds: dict, pzf: dict | None, Q0: float,
                   grc_hand: list[tuple[float, float]]) -> dict | None:
    """One method at one gauge: n values, clip counts, holdout errs, origin ratio."""
    params = core.method_params(method, hdf, slope, pzf, Q0)
    stages, K = core.build_geometry(hdf, params["delta"], params["w0"])
    n_zones, clip_lo, clip_hi = core.calibrate_n_zones(
        stages, K, slope, bounds, params["q_offset"], params["shift"])
    if not n_zones:
        return None

    shift = params["shift"]
    stages_eval = stages + shift
    K_eval = np.interp(stages_eval, stages, K)
    n_arr  = core.n_profile(stages_eval, bounds, n_zones, shift=shift)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = np.where(n_arr > 0, K_eval * (slope ** 0.5) / n_arr, 0.0)
    q = np.maximum.accumulate(q + params["q_offset"])

    ah = np.sort([h for h, _ in bounds.values()])
    ho = []
    for h_obs, q_obs in grc_hand:
        if not (ah.min() < h_obs < ah.max()) or q_obs <= 0:
            continue
        if np.abs(ah - h_obs).min() < ANCHOR_EXCL:
            continue
        # evaluate the applied curve at the observed stage (already includes shift)
        ho.append(100 * (float(np.interp(h_obs, stages, q)) - q_obs) / q_obs)

    ratio = float(np.interp(0.0, stages, q)) / Q0 if Q0 > 0 else np.nan
    return {"n_vals": list(n_zones.values()), "clip_lo": clip_lo,
            "clip_hi": clip_hi, "holdout": ho, "ratio": ratio,
            "mode": params["mode"]}


def main():
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    rc, recur = core.load_shared()
    hucs = core.study_hucs()

    agg = {m: {"n": [], "lo": 0, "hi": 0, "ho": [], "ratio": []} for m in METHODS}
    ho_orig_all = []
    n_gauges = 0

    for huc8 in hucs:
        aoi = core.OUT_DIR / f"HUC{huc8}"
        elev_path = aoi / "usgs_elev_table.csv"
        if not elev_path.exists():
            continue
        elev = pd.read_csv(elev_path, dtype={"location_id": str, "feature_id": "Int64"})
        elev["location_id"] = elev["location_id"].str.zfill(8)
        rc_ids = set(rc["location_id"])

        branches = aoi / "watershed-data" / "branches"
        if not branches.exists():
            continue
        non_trunk = elev[elev["levpa_id"].astype(str) != "0"]
        gauged_bids = set(
            non_trunk[non_trunk["location_id"].isin(rc_ids)]["levpa_id"].astype(str))

        for bid in sorted(gauged_bids):
            bdir = branches / bid
            ht = _baseline_ht(bdir, bid)
            if ht is None:
                continue
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
                if not bounds:
                    continue
                hdf = ht[ht["HydroID"] == hydroid].sort_values("stage")
                if hdf.empty:
                    continue
                slope = float(hdf["SLOPE"].iloc[0])
                if slope <= 0:
                    continue
                pzf = core.fit_pzf(gage_rc, dem)
                if pzf is None:
                    continue        # compare methods only where all four apply
                Q0 = core.baseflow_q0(gage_rc, dem)
                grc = gage_rc.sort_values("flow_cms").drop_duplicates("flow_cms")
                grc_hand = list(zip((grc["elev_m"] - dem).astype(float),
                                    grc["flow_cms"].astype(float)))

                # baseline (original SRC) holdout error, once per gauge
                ah = np.sort([h for h, _ in bounds.values()])
                st0 = hdf["stage"].to_numpy(float)
                q0_curve = hdf["discharge_cms"].to_numpy(float)
                for h_obs, q_obs in grc_hand:
                    if (ah.min() < h_obs < ah.max()) and q_obs > 0 \
                            and np.abs(ah - h_obs).min() >= ANCHOR_EXCL:
                        ho_orig_all.append(
                            100 * (float(np.interp(h_obs, st0, q0_curve)) - q_obs) / q_obs)

                ok = False
                for m in METHODS:
                    out = evaluate_gauge(m, hdf, slope, bounds, pzf, Q0, grc_hand)
                    if out is None:
                        continue
                    a = agg[m]
                    a["n"] += out["n_vals"]
                    a["lo"] += out["clip_lo"]
                    a["hi"] += out["clip_hi"]
                    a["ho"] += out["holdout"]
                    if np.isfinite(out["ratio"]):
                        a["ratio"].append(out["ratio"])
                    ok = True
                if ok:
                    seen.add(hydroid)
                    n_gauges += 1

    # ── Table ─────────────────────────────────────────────────────
    rows = []
    for m in METHODS:
        a = agg[m]
        nv = np.array(a["n"]); ho = np.array(a["ho"])
        rows.append({
            "method": m, "label": M_LABEL[m],
            "anchors": len(nv),
            "pinned_at_NMAX_pct": round(100 * a["hi"] / max(len(nv), 1), 1),
            "pinned_at_NMIN_pct": round(100 * a["lo"] / max(len(nv), 1), 1),
            "median_n": round(float(np.median(nv)), 3),
            "n_in_lit_range_pct": round(
                100 * ((nv >= LIT_RANGE[0]) & (nv <= LIT_RANGE[1])).mean(), 1),
            "holdout_median_err_pct": round(float(np.median(ho)), 1),
            "holdout_median_abs_err_pct": round(float(np.median(np.abs(ho))), 1),
            "origin_ratio_median": round(float(np.median(a["ratio"])), 2),
        })
    table = pd.DataFrame(rows)
    ho_o = np.array(ho_orig_all)
    log.info("Gauges compared: %d | holdout points: %d | "
             "ORIGINAL SRC median |err| %.1f%%",
             n_gauges, len(agg["A"]["ho"]), np.median(np.abs(ho_o)))
    print("\n" + table.to_string(index=False))

    table.to_csv(EVAL_DIR / "method_comparison.csv", index=False)

    # ── Figure ────────────────────────────────────────────────────
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), constrained_layout=True)
    x = np.arange(len(METHODS))
    clrs = [M_CLR[m] for m in METHODS]
    short = [m for m in METHODS]

    ax = axes[0]
    ax.bar(x - 0.18, table["pinned_at_NMAX_pct"], 0.34, color=clrs, alpha=0.9,
           label="at N_MAX")
    ax.bar(x + 0.18, table["pinned_at_NMIN_pct"], 0.34, color=clrs, alpha=0.45,
           label="at N_MIN")
    for xi, (h, l) in enumerate(zip(table["pinned_at_NMAX_pct"],
                                    table["pinned_at_NMIN_pct"])):
        ax.text(xi - 0.18, h, f"{h:.0f}", ha="center", va="bottom", fontsize=9)
        ax.text(xi + 0.18, l, f"{l:.0f}", ha="center", va="bottom", fontsize=9)
    ax.set_xticks(x); ax.set_xticklabels(short)
    ax.set_ylabel("% of n pinned at a bound")
    ax.set_title("Bound saturation — lower is better\n(pinned n = censored label)")
    ax.legend(fontsize=9)

    ax = axes[1]
    ax.bar(x, table["n_in_lit_range_pct"], 0.5, color=clrs, alpha=0.9)
    for xi, v in enumerate(table["n_in_lit_range_pct"]):
        ax.text(xi, v, f"{v:.0f}%", ha="center", va="bottom", fontsize=9.5)
    ax.set_xticks(x); ax.set_xticklabels(short)
    ax.set_ylabel("% of n in 0.02–0.20")
    ax.set_title("Physical plausibility of calibrated n\n(Chow 1959 natural-channel range)")

    ax = axes[2]
    ax.bar(x - 0.18, table["holdout_median_abs_err_pct"], 0.34, color=clrs,
           alpha=0.9, label="hold-out median |err| %")
    ax.bar(x + 0.18, (table["origin_ratio_median"] - 1).abs() * 100, 0.34,
           color=clrs, alpha=0.45, label="|origin ratio − 1| ×100")
    for xi, v in enumerate(table["holdout_median_abs_err_pct"]):
        ax.text(xi - 0.18, v, f"{v:.0f}", ha="center", va="bottom", fontsize=9)
    ax.set_xticks(x); ax.set_xticklabels(short)
    ax.set_title("Accuracy — hold-out fit and origin match\n(lower is better for both)")
    ax.legend(fontsize=9)

    fig.suptitle("Starting-point method evaluation — "
                 f"{n_gauges} gauges, identical anchors and machinery",
                 fontweight="bold")
    fig.savefig(EVAL_DIR / "method_comparison.png", dpi=150,
                bbox_inches="tight", facecolor="white")
    plt.close(fig)

    # ── Verdict ───────────────────────────────────────────────────
    best = table.sort_values(
        ["pinned_at_NMAX_pct", "n_in_lit_range_pct"],
        ascending=[True, False]).iloc[0]
    print(f"""
VERDICT
-------
All methods reduce the hold-out error from {np.median(np.abs(ho_o)):.0f}% (original SRC)
to {table['holdout_median_abs_err_pct'].min():.0f}-{table['holdout_median_abs_err_pct'].max():.0f}% — curve fit alone cannot discriminate, because n absorbs
whatever the geometry correction gets wrong.  The discriminator is whether n
stays PHYSICAL (uncensored labels for the hypothesis test and the ML stage):

  chosen method: {best['method']} ({best['label']})
    bound saturation {best['pinned_at_NMAX_pct']}% / {best['pinned_at_NMIN_pct']}%  |  median n {best['median_n']}  |
    {best['n_in_lit_range_pct']}% in literature range  |  origin ratio {best['origin_ratio_median']}

Outputs: {EVAL_DIR}
""")


if __name__ == "__main__":
    main()
