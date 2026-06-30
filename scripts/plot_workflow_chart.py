"""
PPT-ready workflow chart for the FIMbox calibration pipeline.

Run:
    .venv\\Scripts\\python.exe scripts/plot_workflow_chart.py
"""
from pathlib import Path
from matplotlib.patches import FancyBboxPatch
import matplotlib.pyplot as plt
import matplotlib as mpl

mpl.rcParams["font.family"] = "DejaVu Sans"

SAVE_TO = Path("D:/SI/out/HUC03020102/src_plots")

# ── Color palette ─────────────────────────────────────────────────
C_PIPE   = "#1b4f72"   # dark navy  — OWP pipeline stages
C_CUSTOM = "#a04000"   # burnt orange — custom scripts (our work)
C_DATA   = "#1a6b3c"   # dark green  — external data sources
C_OUT    = "#0e6655"   # teal        — calibrated outputs
C_FUTURE = "#717d7e"   # gray        — future steps
C_ML     = "#6c3483"   # purple      — ML future step
BG       = "#f0f3f4"   # background


# ── Helpers ───────────────────────────────────────────────────────

def draw_box(ax, cx, cy, w, h, lines, color, tc="white",
             fs=9.5, future=False):
    lx, by = cx - w / 2, cy - h / 2
    fc = "#c8cacb" if future else color
    ls = "--"  if future else "-"
    lw = 1.2   if future else 2.0
    ec = "#aaa" if future else "white"
    tc_use = "#666" if future else tc

    ax.add_patch(FancyBboxPatch(
        (lx, by), w, h,
        boxstyle="round,pad=0.13",
        facecolor=fc, edgecolor=ec,
        linewidth=lw, linestyle=ls, zorder=3,
    ))
    ax.text(cx, cy, "\n".join(lines),
            ha="center", va="center", fontsize=fs,
            color=tc_use, fontweight="bold",
            multialignment="center", linespacing=1.45,
            zorder=4)


def arrow(ax, x1, y1, x2, y2, color="#444", lw=2.0, rad=0.0):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(
                    arrowstyle="-|>", color=color,
                    lw=lw, mutation_scale=14,
                    connectionstyle=f"arc3,rad={rad}",
                ),
                zorder=5, annotation_clip=False)


def legend_entry(ax, cx, cy, w, h, color, label, future=False):
    fc = "#c8cacb" if future else color
    ax.add_patch(FancyBboxPatch(
        (cx - w/2, cy - h/2), w, h,
        boxstyle="round,pad=0.06",
        facecolor=fc, edgecolor="white",
        linewidth=1.0, linestyle="--" if future else "-",
        zorder=3,
    ))
    ax.text(cx + w/2 + 0.15, cy, label,
            ha="left", va="center", fontsize=8.5,
            color="#222", zorder=4)


# ── Figure ────────────────────────────────────────────────────────

fig, ax = plt.subplots(figsize=(17, 11))
ax.set_xlim(0, 17)
ax.set_ylim(0, 11)
ax.axis("off")
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)

MX = 8.7   # main pipeline x-center
BW = 5.6   # main box width

# ── Title ─────────────────────────────────────────────────────────

ax.text(MX, 10.7,
        "FIMbox  Manning's n  Calibration Pipeline",
        ha="center", va="center", fontsize=15,
        fontweight="bold", color="#111")
ax.text(MX, 10.35,
        "NOAA OWP HAND-FIM Testbed   |   NC/SC Coastal Study Area   |   24 HUC8s",
        ha="center", va="center", fontsize=10, color="#555")

# ── Main pipeline boxes ───────────────────────────────────────────

# 1. Study area
draw_box(ax, MX, 9.55, BW, 0.62,
         ["Study Area Definition",
          "24 HUC8s  |  NC / SC Coast (HUC2 = 03)  |  Test HUC8: 03020102"],
         C_PIPE)

# 2. Stage 1
draw_box(ax, MX, 8.60, BW, 0.65,
         ["Stage 1  -  Input Data Acquisition",
          "NWM streams   WBD boundaries   DEM   NHD flowlines"],
         C_PIPE)

# 3. Stage 2
draw_box(ax, MX, 7.38, BW, 0.90,
         ["Stage 2  -  HAND-FIM Preprocessing  (OWP FIMbox)",
          "17 branches  (1 trunk + 16 level-path branches)",
          "HAND rasters   |   Synthetic Rating Curves (SRCs) per HydroID"],
         C_PIPE)

# 4. USGS Crosswalk
draw_box(ax, MX, 5.93, BW, 0.90,
         ["USGS Gage Crosswalk",
          "Download gauges  |  Assign to NWM branches",
          "Snap to thalweg  |  Extract HAND-stage elevation"],
         C_CUSTOM)

# 5. n Calibration
draw_box(ax, MX, 4.48, BW, 0.90,
         ["Manning's  n  Calibration",
          "Back-calculate n at 6 recurrence-interval zones  (2, 5, 10, 25, 50, >50 yr)",
          "Piecewise n applied to directly calibrated branches only"],
         C_CUSTOM)

# 6a. Calibrated branches (left branch)
draw_box(ax, 4.5, 3.00, 4.6, 0.82,
         ["Calibrated Branches  (2 of 17)",
          "Branch 3351000014  |  Gauge 02083000",
          "Branch 3351000023  |  Gauge 02082950"],
         C_OUT)

# 6b. Ungauged (right branch — future)
draw_box(ax, 13.2, 3.00, 4.6, 0.82,
         ["Ungauged Branches  (15 of 17)",
          "ML-based n Regionalization",
          "(future work)"],
         C_ML, future=True)

# 7. Validation plots
draw_box(ax, 4.5, 1.72, 4.6, 0.82,
         ["Calibration Validation Plots",
          "Orig. SRC  vs  Calibrated SRC  vs  USGS RC",
          "Manning's n variation with HAND stage"],
         C_CUSTOM)

# 8. Future pipeline bar
draw_box(ax, 8.7, 0.55, 14.0, 0.60,
         ["Stage 3: Streamflow Mapping          Stage 4: FIM Calibration          Stage 5: FIM Generation  (Flood Inundation Maps)"],
         C_FUTURE, future=True)

# ── External data source boxes ────────────────────────────────────

# USGS gauge network → Crosswalk
draw_box(ax, 15.2, 5.93, 2.9, 0.62,
         ["USGS Gauge Network",
          "ArcGIS Online API"],
         C_DATA, fs=8.8)

# USGS RC + NWM Recurrence → Calibration
draw_box(ax, 15.2, 4.48, 2.9, 0.82,
         ["USGS Rating Curves",
          "usgs_rating_curves.parquet",
          "NWM Recurrence Flows  (2-50 yr)"],
         C_DATA, fs=8.5)

# ── Arrows ────────────────────────────────────────────────────────

PIPE_COL = "#1b4f72"

# Main vertical flow
arrow(ax, MX, 9.24,  MX, 8.93)     # Study → Stage1
arrow(ax, MX, 8.28,  MX, 7.83)     # Stage1 → Stage2
arrow(ax, MX, 6.93,  MX, 6.38)     # Stage2 → Crosswalk
arrow(ax, MX, 5.48,  MX, 4.93)     # Crosswalk → Calibration

# Calibration → split
arrow(ax, 7.0, 4.03,  5.0, 3.42,   color="#0e6655")   # → Calibrated
arrow(ax, 10.4, 4.03, 12.6, 3.42,  color=C_ML)         # → Ungauged

# Calibrated → Validation
arrow(ax, 4.5, 2.59,  4.5, 2.13,   color="#0e6655")

# Validation → Future
arrow(ax, 5.8, 1.31,  7.5, 0.85,   color="#777", lw=1.5)

# Data inputs (horizontal)
arrow(ax, 13.75, 5.93, 11.51, 5.93, color=C_DATA, lw=1.6)   # gauges → crosswalk
arrow(ax, 13.75, 4.48, 11.51, 4.48, color=C_DATA, lw=1.6)   # RC+NWM → calibration

# ── "Future Work" separator line ──────────────────────────────────

ax.plot([1.0, 16.0], [1.18, 1.18],
        color="#aaa", lw=1.2, ls="--", zorder=2)
ax.text(16.1, 1.18, "Future", ha="left", va="center",
        fontsize=8.5, color="#777", fontstyle="italic")

# ── Legend  (upper-left, clear of all boxes) ──────────────────────

items = [
    (C_PIPE,   "OWP FIMbox Pipeline Stage  (completed)",  False),
    (C_CUSTOM, "Custom Script  (this project)",           False),
    (C_DATA,   "External Data Source",                    False),
    (C_OUT,    "Calibrated Output",                       False),
    (C_ML,     "Future  -  ML Regionalization",           True),
    (C_FUTURE, "Future  -  Pipeline Continuation",        True),
]

lx0, ly0, dy = 0.55, 8.60, 0.50

# subtle background panel
n_items = len(items)
panel_h = dy * n_items + 0.15
panel_y = ly0 - panel_h + 0.38
ax.add_patch(FancyBboxPatch(
    (0.18, panel_y - 0.10), 4.55, panel_h + 0.55,
    boxstyle="round,pad=0.08",
    facecolor="white", edgecolor="#ccc",
    linewidth=0.9, alpha=0.82, zorder=2,
))

ax.text(0.40, ly0 + 0.38, "Legend", ha="left", va="center",
        fontsize=9.5, fontweight="bold", color="#333", zorder=5)

for i, (col, lbl, fut) in enumerate(items):
    ly = ly0 - i * dy
    legend_entry(ax, lx0 + 0.22, ly, 0.44, 0.30, col, lbl, future=fut)

fig.tight_layout(pad=0.3)

out = SAVE_TO / "workflow_chart.png"
SAVE_TO.mkdir(parents=True, exist_ok=True)
fig.savefig(out, dpi=200, bbox_inches="tight", facecolor=BG)
plt.close(fig)
print(f"Saved: {out}")
