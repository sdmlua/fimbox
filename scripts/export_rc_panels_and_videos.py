"""
Consolidate per-gauge RC panels into one export folder and render them as
slideshow videos — three variants of the SAME calibrated curve, differing
only in which n definition is annotated in the title:

    slice/          annotated with slice n (applied, incremental scheme)
    wholesection/    annotated with whole-section n (effective, back-computed)
    both/            annotated with both definitions stacked

Outputs (one consolidated location):
    E:/SI/out/calibration_analysis/rc_panels_export/
        slice/{NNN}_{huc8}_{loc}.png
        wholesection/{NNN}_{huc8}_{loc}.png
        both/{NNN}_{huc8}_{loc}.png
        video_slice_n.mp4
        video_wholesection_n.mp4
        video_both_n.mp4

Reuses the data-assembly logic from plot_v2_diagnostics.py (same anchors,
same baseline/calibrated hydroTable curves) — only the panel's annotation
and output path differ. Panels are numbered in a fixed (huc8, location_id)
sort order so the three videos show gauges in the same sequence.

Run AFTER plot_v2_diagnostics.py (needs ncalib_diagnostics.csv per HUC):
    .venv\\Scripts\\python.exe scripts/export_rc_panels_and_videos.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib as mpl
mpl.use("Agg")   # headless backend — TkAgg default is ~10-20x slower for batch savefig

import imageio.v2 as imageio
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import plot_v2_diagnostics as v2  # reuse load_shared / load_diagnostics / gauge_records

mpl.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10.5,
    "axes.labelsize": 12, "axes.titlesize": 10.5, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.20, "grid.linestyle": ":",
    "legend.fontsize": 9.5, "legend.framealpha": 0.92, "figure.dpi": 140,
})

C_ORIG, C_CALIB, C_OBS = "#2471a3", "#c0392b", "#111111"
C_WSEC = "#7f8c8d"   # whole-section curve — same gray as the scheme evaluation
RY = v2.RECURRENCE_YEARS

EXPORT_DIR = Path("E:/SI/out/calibration_analysis/rc_panels_export")
VARIANTS = ("slice", "wholesection", "both")
FPS = 10
HOLD_SECONDS = 1.5


def n_line(d: pd.Series, col_prefix: str) -> str:
    return "   ".join(f"n({yr}) = {d[f'{col_prefix}_{yr}yr']:.3f}"
                      for yr in RY if pd.notna(d.get(f"{col_prefix}_{yr}yr")))


def draw_panel(g: dict, anchors: list, s_o: pd.DataFrame, s_c: pd.DataFrame,
               grc: pd.DataFrame, variant: str) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(9.5, 6.6), constrained_layout=True)

    h_max = max(a[1] for a in anchors) * 1.6
    q_max = max(a[2] for a in anchors) * 1.8

    for i, (yr, h_r, _) in enumerate(anchors):
        ax.axhspan(0 if i == 0 else anchors[i - 1][1], h_r,
                   color="#187E86", alpha=0.045 if i % 2 == 0 else 0.09)
    if not grc.empty:
        ax.plot(grc["flow_cms"], grc["hand"], color=C_OBS, lw=2.0,
                label="USGS observed RC", zorder=5)
    ax.plot(s_o["Discharge (m3s-1)"], s_o["Stage"], color=C_ORIG, lw=1.8,
            label="Baseline SRC (n = 0.06)", zorder=4)

    # The two calibrated curves are genuinely different objects: the slice
    # (incremental) curve is what FIM consumes; the whole-section curve is an
    # independently calibrated diagnostic (discharge_cms_wholesection column).
    has_wsec = ("discharge_cms_wholesection" in s_c.columns
                and s_c["discharge_cms_wholesection"].notna().any())
    if variant in ("slice", "both"):
        ax.plot(s_c["Discharge (m3s-1)"], s_c["Stage"], color=C_CALIB, lw=2.6,
                ls="--", label="Calibrated SRC — slice n (applied)", zorder=6)
    if variant in ("wholesection", "both") and has_wsec:
        ax.plot(s_c["discharge_cms_wholesection"], s_c["Stage"], color=C_WSEC,
                lw=2.2, ls="-.", label="Calibrated SRC — whole-section n",
                zorder=5)
    elif variant == "wholesection" and not has_wsec:
        # single-anchor / all-pinned gauges: the two schemes coincide
        ax.plot(s_c["Discharge (m3s-1)"], s_c["Stage"], color=C_WSEC, lw=2.2,
                ls="-.", label="Calibrated SRC — whole-section n (= slice)",
                zorder=5)
    for yr, h_r, Q_r in anchors:
        ax.scatter([Q_r], [h_r], s=52, color=C_CALIB, edgecolors="white",
                   linewidths=1.4, zorder=8)
        ax.annotate(f"{yr}-yr", (Q_r, h_r), textcoords="offset points",
                    xytext=(7, -3), fontsize=8.5, color=C_CALIB, fontweight="bold")

    d = g["diag"]
    mode_txt = {"wedge_continuity": f"continuity wedge  δ = {d['delta_m']:.2f} m",
                "wedge_topwidth":   f"TopWidth wedge  δ = {d['delta_m']:.2f} m",
                "q0_offset":        f"baseflow offset  Q₀ = {d['q0_cms']:.1f} m³/s"}.get(
                    d["mode"], d["mode"])

    if variant == "slice":
        n_txt = "Slice n (applied):  " + n_line(d, "n")
    elif variant == "wholesection":
        n_txt = "Whole-section n (effective):  " + n_line(d, "n_eff")
    else:  # both
        n_txt = ("Slice n:          " + n_line(d, "n") + "\n" +
                 "Whole-section n:  " + n_line(d, "n_eff"))

    ax.set_title(f"Gauge {g['location_id']}  ·  HydroID {g['hydroid']}  ·  "
                 f"HUC {g['huc8']}\n{mode_txt}\n{n_txt}", fontsize=9.5)
    ax.set_xlim(0, q_max)
    ax.set_ylim(0, h_max)
    ax.set_xlabel("Discharge  (m³/s)")
    ax.set_ylabel("HAND stage  (m)")
    ax.legend(loc="lower right")
    return fig


def main():
    for v in VARIANTS:
        (EXPORT_DIR / v).mkdir(parents=True, exist_ok=True)

    hucs = [str(int(c)).zfill(8)
            for c in pd.read_excel(v2.EXCEL_PATH)[v2.HUC_CODE_COL]]
    rc, recur = v2.load_shared()
    diag = v2.load_diagnostics(hucs)
    records = sorted(v2.gauge_records(diag, rc, recur),
                     key=lambda r: (r[0]["huc8"], r[0]["location_id"]))
    log.info("Rendering %d gauges × %d variants", len(records), len(VARIANTS))

    paths = {v: [] for v in VARIANTS}
    for i, (g, anchors, s_o, s_c, grc) in enumerate(records, 1):
        fname = f"{i:03d}_{g['huc8']}_{g['location_id']}.png"
        for variant in VARIANTS:
            fig = draw_panel(g, anchors, s_o, s_c, grc, variant)
            out = EXPORT_DIR / variant / fname
            fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
            plt.close(fig)
            paths[variant].append(out)
        if i % 20 == 0:
            log.info("  %d/%d gauges rendered", i, len(records))
    log.info("Rendered %d gauges → %s", len(records), EXPORT_DIR)

    labels = {"slice": "Slice n (applied, incremental scheme)",
              "wholesection": "Whole-section n (effective)",
              "both": "Both n definitions"}
    for variant in VARIANTS:
        make_video(paths[variant], EXPORT_DIR / f"video_{variant}_n.mp4",
                  labels[variant])


def make_video(png_paths: list[Path], out_path: Path, label: str) -> None:
    if not png_paths:
        log.warning("No frames for %s — skipping video", out_path.name)
        return
    frames = [imageio.imread(p) for p in png_paths]
    h, w = frames[0].shape[:2]
    # matplotlib bbox_inches='tight' can vary output pixel size slightly per
    # panel — pad/crop every frame to a common canvas so the video encoder
    # gets a constant frame size.
    canvas_h = max(f.shape[0] for f in frames)
    canvas_w = max(f.shape[1] for f in frames)
    canvas_h += canvas_h % 2   # even dimensions for the video codec
    canvas_w += canvas_w % 2

    hold_frames = max(1, round(FPS * HOLD_SECONDS))
    writer = imageio.get_writer(str(out_path), fps=FPS, codec="libx264",
                                quality=8, macro_block_size=None)
    for f in frames:
        padded = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
        fh, fw = f.shape[:2]
        rgb = f[..., :3] if f.shape[-1] == 4 else f
        padded[:fh, :fw] = rgb
        for _ in range(hold_frames):
            writer.append_data(padded)
    writer.close()
    log.info("Saved video (%s, %d gauges, %.0fs): %s",
             label, len(png_paths), len(png_paths) * HOLD_SECONDS, out_path)


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


if __name__ == "__main__":
    main()
