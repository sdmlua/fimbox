"""
PPT-ready technical slide deck for the FIMbox calibration pipeline.

Slides:
  01 — Branch Processing: What is a Branch?
  02 — Reference Frame Reconciliation: USGS RC vs SRC
  03 — Our Calibration: Manning's n Back-Calculation (s05)
  04 — Main Repo Calibration: Discharge Correction Coefficient (Stage 4)
  05 — Side-by-Side Comparison Table
  06 — The Collision: What Happens When Stage 4 Runs
  07 — Items Needing Attention Before Stage 5

Run:
    .venv\\Scripts\\python.exe scripts/plot_calibration_slides.py
"""
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch

mpl.rcParams["font.family"] = "DejaVu Sans"

SAVE_TO = Path("D:/SI/out/HUC03020102/src_plots/slides")

# ── Palette ───────────────────────────────────────────────────────────────────
C_NAVY   = "#1b4f72"
C_DBLUE  = "#1a5276"
C_ORANGE = "#a04000"
C_GREEN  = "#1a6b3c"
C_TEAL   = "#0e6655"
C_PURPLE = "#6c3483"
C_WARN   = "#922b21"
C_AMBER  = "#7d6608"
C_GRAY   = "#566573"
C_BG     = "#f4f6f7"
C_PLAIN  = "#0b3d5e"   # plain-language box header
C_TECH   = "#3b0f5e"   # technical box header
C_STRIPE = "#d5d8dc"   # table stripe

W, H = 16, 9           # figure size in inches (16:9)


# ── Low-level helpers ─────────────────────────────────────────────────────────

def new_slide():
    fig, ax = plt.subplots(figsize=(W, H))
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    return fig, ax


def header_bar(ax, title, subtitle="", slide_n=""):
    ax.add_patch(FancyBboxPatch(
        (0, H - 1.15), W, 1.15,
        boxstyle="square,pad=0", facecolor=C_NAVY,
        edgecolor="none", zorder=3,
    ))
    ax.text(0.30, H - 0.50, title,
            ha="left", va="center", fontsize=16, fontweight="bold",
            color="white", zorder=4)
    if subtitle:
        ax.text(0.30, H - 0.88, subtitle,
                ha="left", va="center", fontsize=9.5, color="#a9cce3", zorder=4)
    if slide_n:
        ax.text(W - 0.25, H - 0.60, slide_n,
                ha="right", va="center", fontsize=9, color="#aaaaaa", zorder=4)


def content_box(ax, x, y, w, h, box_title, lines, header_color,
                bg="#e8ecef", title_fs=11, body_fs=9.5, line_gap=1.6):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.10", facecolor=bg,
        edgecolor="white", linewidth=1.8, zorder=3,
    ))
    # coloured left accent strip
    ax.add_patch(FancyBboxPatch(
        (x, y), 0.12, h,
        boxstyle="square,pad=0", facecolor=header_color,
        edgecolor="none", zorder=4,
    ))
    ax.text(x + 0.22, y + h - 0.30, box_title,
            ha="left", va="top", fontsize=title_fs, fontweight="bold",
            color=header_color, zorder=5)
    ax.text(x + 0.22, y + h - 0.62, "\n".join(lines),
            ha="left", va="top", fontsize=body_fs,
            color="#1a1a1a", linespacing=line_gap, zorder=5)


def footnote(ax, text, y=0.18):
    ax.text(W / 2, y, text,
            ha="center", va="center", fontsize=8.5, color="#555",
            style="italic", zorder=4)


def divider(ax, y, x0=0.25, x1=None):
    ax.plot([x0, x1 or W - 0.25], [y, y],
            color="#b2bec3", lw=0.8, ls="--", zorder=2)


def save_slide(fig, name):
    SAVE_TO.mkdir(parents=True, exist_ok=True)
    out = SAVE_TO / name
    fig.savefig(out, dpi=180, bbox_inches="tight", facecolor=C_BG)
    plt.close(fig)
    print(f"  Saved: {out.name}")


# ── Slide 01 — Branch Processing ──────────────────────────────────────────────

def slide_01():
    fig, ax = new_slide()
    header_bar(ax,
               "Branch Processing — How a HUC8 is Split Into Workable Units",
               subtitle="Level-path derivation from NWM stream network topology",
               slide_n="01 / 07")

    content_box(ax, 0.25, 1.05, 7.35, 6.55,
                "Plain Language",
                [
                    "A branch = one river corridor.",
                    "Starting where a tributary joins the main river,",
                    "running all the way up to the tributary's headwater.",
                    "",
                    "HUC 03020102 has 17 branches:",
                    "  Branch 0        —  whole-basin trunk (processed first)",
                    "  3351000014–3351000029  —  16 individual stream corridors",
                    "",
                    "Each branch is processed independently:",
                    "  its own DEM clip, HAND raster, and rating curve table.",
                    "",
                    "Why split at all?",
                    "  Memory: a full-basin HAND raster is too large to compute",
                    "  in one pass. Branches let us parallelize and tile.",
                ],
                C_PLAIN, bg="#e8f4fc", body_fs=10.0, line_gap=1.55)

    content_box(ax, 7.90, 1.05, 7.85, 6.55,
                "Technical Detail  (branch_derivation.py)",
                [
                    "Input: NWM streams for the HUC",
                    "  → filter: drop stream orders 1 & 2  (line 137)",
                    "  → result: only order ≥ 3 tributaries remain",
                    "",
                    "_assign_levelpaths()  (lines 704–803):",
                    "  1. Find all network outlets (no downstream reach)",
                    "  2. Walk upstream from each outlet",
                    "  3. At each confluence — pick ONE upstream reach",
                    "     to continue the same level path:",
                    "     → highest stream order wins",
                    "     → tie: largest arbolate sum; then reach ID",
                    "  4. All other upstream tributaries at that junction",
                    "     → start a NEW level path (new branch ID)",
                    "",
                    "Branch IDs are synthetic (e.g. 3351000014 = first 4 chars",
                    "of NWM reach ID + sequential suffix)",
                    "",
                    "_remove_branches_without_catchments()  (line 806):",
                    "  Drops any branch with zero NWM catchment polygons",
                    "  → prevents empty-DEM crashes downstream",
                ],
                C_TECH, bg="#f3eafd", body_fs=9.2, line_gap=1.50)

    ax.add_patch(FancyBboxPatch(
        (0.25, 0.18), 15.50, 0.72,
        boxstyle="round,pad=0.08", facecolor=C_TEAL,
        edgecolor="none", linewidth=0, zorder=3,
    ))
    ax.text(8.0, 0.54,
            "HUC 03020102:  17 branches  =  1 trunk (Branch 0)  +  16 level-path branches "
            "(3351000014 → 3351000029)",
            ha="center", va="center", fontsize=10.5, fontweight="bold",
            color="white", zorder=4)

    save_slide(fig, "slide_01_branch_processing.png")


# ── Slide 02 — Reference Frame Reconciliation ─────────────────────────────────

def slide_02():
    fig, ax = new_slide()
    header_bar(ax,
               "Reference Frame Reconciliation — USGS RC  vs  Synthetic RC (SRC)",
               subtitle="Converting absolute NAVD88 elevation to HAND (height above thalweg)",
               slide_n="02 / 07")

    content_box(ax, 0.25, 4.30, 7.35, 3.40,
                "Plain Language",
                [
                    "USGS gauges report water-surface elevation in feet",
                    "above sea level  (NAVD88 datum).",
                    "",
                    "Our Synthetic Rating Curves use a different reference:",
                    "height above the riverbed  (HAND stage).",
                    "",
                    "To compare them we subtract the riverbed elevation",
                    "(sampled from the DEM at the snapped gauge location).",
                    "",
                    "The SRC has only discrete 1-foot stage steps, so we",
                    'find the "closest row" rather than an exact match.',
                ],
                C_PLAIN, bg="#e8f4fc", body_fs=10.0, line_gap=1.60)

    content_box(ax, 0.25, 1.05, 7.35, 3.00,
                "Technical Detail  (s05_calibrate_n_recurrence.py  lines 95–194)",
                [
                    "Unit conversion (lines 95–100):",
                    "  rc['flow_cms']  = rc['flow'] / 35.3147       # cfs → m³/s",
                    "  rc['elev_m']    = rc['elevation_navd88'] / 3.28084  # ft → m",
                    "",
                    "Reference conversion (lines 154–155):",
                    "  elev_r = np.interp(Q_r, gage_rc['flow_cms'],",
                    "                          gage_rc['elev_m'])  # NAVD88 water surface (m)",
                    "  hand_r = elev_r − dem_adj_elevation_m        # → HAND stage",
                    "",
                    "Nearest-neighbour SRC lookup (line 192):",
                    "  idx = (hdf['Stage'] − hand_r).abs().idxmin()",
                    "  row = hdf.loc[idx]   # → A, R geometry at that stage",
                ],
                C_TECH, bg="#f3eafd", body_fs=9.0, line_gap=1.55)

    # ── Diagram ────────────────────────────────────────────────────────────────
    dx = 9.80
    bx = dx        # left edge of diagram area
    # Ground / thalweg line
    ax.plot([bx, bx + 5.8], [3.00, 3.00], color="#7d6608", lw=2.5, zorder=4)
    ax.text(bx + 5.85, 3.00, "Thalweg / riverbed", va="center", fontsize=9, color="#7d6608")
    # NAVD88 datum
    ax.plot([bx, bx + 5.8], [1.45, 1.45], color="#566573", lw=1.5, ls="--", zorder=4)
    ax.text(bx + 5.85, 1.45, "NAVD88 datum", va="center", fontsize=9, color="#566573")
    # Water surface
    ax.plot([bx, bx + 5.8], [5.50, 5.50], color="#2980b9", lw=2.5, zorder=4)
    ax.text(bx + 5.85, 5.50, "Water surface", va="center", fontsize=9, color="#2980b9")

    # Arrow: NAVD88 → thalweg (dem_adj_elevation_m)
    cx = bx + 1.2
    ax.annotate("", xy=(cx, 3.00), xytext=(cx, 1.45),
                arrowprops=dict(arrowstyle="<->", color=C_ORANGE, lw=1.8), zorder=5)
    ax.text(cx - 0.12, 2.22, "dem_adj_elevation_m\n(thalweg elev, from DEM)",
            ha="right", va="center", fontsize=8.5, color=C_ORANGE)

    # Arrow: thalweg → water surface (HAND stage)
    cx2 = bx + 2.8
    ax.annotate("", xy=(cx2, 5.50), xytext=(cx2, 3.00),
                arrowprops=dict(arrowstyle="<->", color=C_GREEN, lw=2.2), zorder=5)
    ax.text(cx2 + 0.12, 4.25, "HAND stage  =  hand_r\n(what SRC Stage column uses)",
            ha="left", va="center", fontsize=8.5, color=C_GREEN)

    # Arrow: NAVD88 → water surface (elev_m from USGS RC)
    cx3 = bx + 4.4
    ax.annotate("", xy=(cx3, 5.50), xytext=(cx3, 1.45),
                arrowprops=dict(arrowstyle="<->", color=C_NAVY, lw=2.2), zorder=5)
    ax.text(cx3 + 0.12, 3.47, "elev_r  (NAVD88 water surface)\n(what USGS RC reports)",
            ha="left", va="center", fontsize=8.5, color=C_NAVY)

    # Equation box
    ax.add_patch(FancyBboxPatch(
        (bx - 0.10, 5.80), 5.90, 0.75,
        boxstyle="round,pad=0.08", facecolor="#fdfefe",
        edgecolor="#aab7b8", linewidth=1.2, zorder=3,
    ))
    ax.text(bx + 2.85, 6.18,
            "hand_r  =  elev_r  −  dem_adj_elevation_m",
            ha="center", va="center", fontsize=11, fontweight="bold",
            color=C_NAVY, zorder=5,
            fontfamily="monospace")

    ax.add_patch(FancyBboxPatch(
        (0.25, 0.18), 15.50, 0.60,
        boxstyle="round,pad=0.08", facecolor="#eaecee",
        edgecolor="#aab7b8", linewidth=0.8, zorder=2,
    ))
    ax.text(W / 2, 0.48,
            "dem_adj_elevation_m  comes from  usgs_elev_table.csv"
            "  —  DEM elevation sampled at the snapped gauge point during the USGS Crosswalk step (s04)",
            ha="center", va="center", fontsize=8.8, color="#444", style="italic", zorder=3)

    save_slide(fig, "slide_02_reference_frames.png")


# ── Slide 03 — Our Calibration ────────────────────────────────────────────────

def slide_03():
    fig, ax = new_slide()
    header_bar(ax,
               "Our Calibration — Manning's  n  Back-Calculation",
               subtitle="s05_calibrate_n_recurrence.py  |  direct algebraic inversion at 5 NWM recurrence points",
               slide_n="03 / 07")

    content_box(ax, 0.25, 3.80, 7.35, 3.95,
                "Plain Language",
                [
                    "We ask: at each of 5 flood levels (2, 5, 10, 25, 50-year",
                    "recurrence), what roughness value n makes the SRC predict",
                    "the same flow as the USGS gauge?",
                    "",
                    "Result: 6 zones — one per recurrence interval boundary,",
                    "plus everything above the 50-yr level.",
                    "Each zone gets its own constant n value.",
                    "",
                    "Only 2 of 17 branches get this treatment:",
                    "  → those with a USGS gauge AND an observed rating curve.",
                    "",
                    "The same n is applied uniformly to every reach (HydroID)",
                    "on that branch within each zone.",
                ],
                C_PLAIN, bg="#e8f4fc", body_fs=10.0, line_gap=1.60)

    content_box(ax, 0.25, 1.05, 7.35, 2.50,
                "Technical Detail",
                [
                    "Manning's equation (inverse):    n  =  A · R^(2/3) · S^(1/2)  /  Q",
                    "",
                    "At each recurrence flow Q_r (2, 5, 10, 25, 50 yr):",
                    "  1. Interpolate USGS RC → elev_r (NAVD88 water surface)",
                    "  2. Convert: hand_r = elev_r − dem_adj_elevation_m  (HAND stage)",
                    "  3. Nearest-neighbour SRC lookup → get A, R for that stage",
                    "  4. n_zone = A·R^(2/3)·sqrt(SLOPE) / Q_r   clipped to [0.01, 0.30]",
                    "  5. Recompute Q: Discharge = A·R^(2/3)·sqrt(SLOPE) / n_zone",
                    "     → for every HydroID × every Stage row in that zone",
                    "Writes ManningN + Discharge (m3s-1) → src_full_crosswalked_{bid}.csv",
                    "Does NOT touch: default_ManningN, default_SLOPE, hydroTable_{bid}.csv",
                ],
                C_TECH, bg="#f3eafd", body_fs=9.2, line_gap=1.52)

    # ── Zone diagram ──────────────────────────────────────────────────────────
    zx = 8.10
    zy_base = 1.05
    zone_h = 6.30 / 6       # 6 zones stacked in 6.30 inches
    zone_labels = ["Zone 1\n< 2yr", "Zone 2\n2–5yr", "Zone 3\n5–10yr",
                   "Zone 4\n10–25yr", "Zone 5\n25–50yr", "Zone 6\n> 50yr"]
    n_14 = [0.065, 0.049, 0.039, 0.016, 0.011, 0.011]
    n_23 = [0.090, 0.083, 0.044, 0.024, 0.011, 0.011]
    zone_cols = ["#aed6f1", "#a9dfbf", "#fad7a0", "#f9e79f", "#f1948a", "#d2b4de"]

    for i in range(6):
        by = zy_base + i * zone_h
        col = zone_cols[i]
        ax.add_patch(FancyBboxPatch(
            (zx, by), 7.65, zone_h - 0.04,
            boxstyle="square,pad=0", facecolor=col,
            edgecolor="#cccccc", linewidth=0.6, alpha=0.55, zorder=3,
        ))
        # Zone label
        ax.text(zx + 0.12, by + zone_h / 2 - 0.02, zone_labels[i],
                ha="left", va="center", fontsize=8, color="#333",
                fontweight="bold", zorder=4, linespacing=1.3)
        # n values for branch 14
        ax.text(zx + 2.85, by + zone_h / 2 - 0.02, f"n = {n_14[i]:.3f}",
                ha="center", va="center", fontsize=9.5, color=C_NAVY,
                fontweight="bold", zorder=4)
        # n values for branch 23
        ax.text(zx + 5.85, by + zone_h / 2 - 0.02, f"n = {n_23[i]:.3f}",
                ha="center", va="center", fontsize=9.5, color=C_ORANGE,
                fontweight="bold", zorder=4)

    # Column headers
    ax.text(zx + 2.85, zy_base + 6 * zone_h + 0.22,
            "Branch 3351000014\n(Gauge 02083000)",
            ha="center", va="bottom", fontsize=9, color=C_NAVY, fontweight="bold",
            linespacing=1.4, zorder=4)
    ax.text(zx + 5.85, zy_base + 6 * zone_h + 0.22,
            "Branch 3351000023\n(Gauge 02082950)",
            ha="center", va="bottom", fontsize=9, color=C_ORANGE, fontweight="bold",
            linespacing=1.4, zorder=4)

    # Jump annotation for Branch 23
    ax.annotate(
        "8.2× jump\n(non-monotonic SRC risk)",
        xy=(zx + 5.85, zy_base + zone_h + 0.03),
        xytext=(zx + 7.30, zy_base + zone_h * 1.5),
        fontsize=8, color=C_WARN, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=C_WARN, lw=1.2),
        ha="left", va="center", zorder=6,
    )

    save_slide(fig, "slide_03_our_calibration.png")


# ── Slide 04 — Main Repo Calibration ─────────────────────────────────────────

def slide_04():
    fig, ax = new_slide()
    header_bar(ax,
               "Main Repo Calibration — Discharge Correction Coefficient  (Stage 4)",
               subtitle="src_calibrate.py / src_optimization.py  |  empirical scalar correction per HydroID",
               slide_n="04 / 07")

    content_box(ax, 0.25, 3.75, 7.35, 4.00,
                "Plain Language",
                [
                    "The main pipeline's question:",
                    "  Is the SRC's predicted flow too high or too low",
                    "  compared to the gauge?  Apply a single multiplier.",
                    "",
                    "Unlike our approach, this does NOT recompute n.",
                    "It just scales the existing discharge curve up or down",
                    "by one factor per stream segment (HydroID).",
                    "",
                    "The correction is then spread to nearby un-gauged",
                    "segments within 8 km by network tracing.",
                    "",
                    "It runs AFTER several other adjustments:",
                    "  bathymetry → bankfull → channel/overbank subdivision",
                    "  → nonmonotonic fix → USGS rating calibration (← here)",
                    "",
                    "So it calibrates an already-modified curve, not the",
                    "raw Stage 2 SRC that our script worked on.",
                ],
                C_PLAIN, bg="#e8f4fc", body_fs=9.8, line_gap=1.55)

    content_box(ax, 0.25, 1.05, 7.35, 2.45,
                "Technical Detail  (src_optimization.py)",
                [
                    "1.  _build_usgs_database: match USGS RC to each NWM recurrence flow",
                    "      → (hand, Q_obs) pairs at each recurrence interval",
                    "2.  For each pair: find nearest hydroTable row → get Q_src",
                    "      calb_coef = Q_src / Q_obs       (per HydroID, per interval)",
                    "3.  Per HydroID:  calb_coef_final = median(calb_coef over all intervals)",
                    "      → ONE scalar per HydroID, not stage-varying",
                    "4.  Q_new(h) = Q_old(h) / calb_coef_final  for ALL stage rows",
                    "5.  Network propagation: _trace_network  ± 8 km (same stream order)",
                    "      group_manningn_calc: running-mean coef  downstream 8 km",
                    "6.  Writes: hydroTable_{bid}.csv  (discharge_cms, calb_coef_usgs)",
                    "      Gated: src_adjust_usgs=True  AND  src_subdiv_toggle=True",
                ],
                C_TECH, bg="#f3eafd", body_fs=9.2, line_gap=1.52)

    # ── Propagation diagram ───────────────────────────────────────────────────
    px = 8.10
    py_base = 1.40
    # River network sketch
    # Main reach (horizontal) ─────────────────────
    ax.annotate("", xy=(px + 6.80, py_base + 2.40),
                xytext=(px, py_base + 2.40),
                arrowprops=dict(arrowstyle="-|>", color="#2980b9", lw=2.0), zorder=4)
    # Tributary
    ax.annotate("", xy=(px + 3.60, py_base + 2.40),
                xytext=(px + 2.20, py_base + 4.20),
                arrowprops=dict(arrowstyle="-|>", color="#2980b9", lw=1.5), zorder=4)

    # Gauge HydroID (orange fill)
    gx, gy = px + 3.60, py_base + 2.40
    ax.add_patch(plt.Circle((gx, gy), 0.28, color=C_ORANGE, zorder=5))
    ax.text(gx, gy + 0.55, "Gauge\nHydroID", ha="center", va="bottom",
            fontsize=8, color=C_ORANGE, fontweight="bold", linespacing=1.3)

    # Traced reaches (±8 km fill)
    for dx, label in [(px + 1.8, "−8 km"), (px + 5.4, "+8 km")]:
        ax.add_patch(plt.Circle((dx, py_base + 2.40), 0.20, color=C_NAVY,
                                alpha=0.55, zorder=5))
        ax.text(dx, py_base + 1.85, label, ha="center", fontsize=8,
                color=C_NAVY, fontweight="bold")

    # Downstream propagation arrows
    for ddx in [px + 5.4, px + 6.8]:
        ax.add_patch(plt.Circle((ddx, py_base + 2.40), 0.15,
                                color=C_TEAL, alpha=0.55, zorder=5))
    ax.text(px + 6.10, py_base + 1.50,
            "group mean coef\ncarried ≤ 8 km\ndownstream",
            ha="center", va="top", fontsize=8, color=C_TEAL, linespacing=1.4)

    # Legend
    for col, lbl, yx in [
        (C_ORANGE, "Direct gauge HydroID", py_base + 5.10),
        (C_NAVY,   "Traced neighborhood (±8 km, same order)", py_base + 4.65),
        (C_TEAL,   "Downstream group carry (≤ 8 km)", py_base + 4.20),
    ]:
        ax.add_patch(plt.Circle((px + 0.22, yx), 0.12, color=col, zorder=5))
        ax.text(px + 0.45, yx, lbl, va="center", fontsize=9, color="#222")

    ax.text(px + 3.60, py_base + 6.10,
            "One scalar coefficient per HydroID — applied uniformly across the full discharge curve",
            ha="center", fontsize=9.5, fontweight="bold", color=C_NAVY)

    save_slide(fig, "slide_04_main_repo_calibration.png")


# ── Slide 05 — Comparison Table ───────────────────────────────────────────────

def slide_05():
    fig, ax = new_slide()
    header_bar(ax,
               "Calibration Methods — Side-by-Side Comparison",
               subtitle="Both use the same USGS rating curves and NWM recurrence flows — but produce discharge differently",
               slide_n="05 / 07")

    rows = [
        ("Method",
         "Manning's n  back-calculation\n(algebraic inversion)",
         "Discharge correction coefficient\n(empirical scalar)"),
        ("What changes",
         "ManningN per zone → Q fully\nrecomputed from geometry",
         "Q_old × (1 / coef),  n unchanged,\ncurve shape preserved"),
        ("Discharge output",
         "Stage-varying  (different n per zone\n→ different curve shape per zone)",
         "Single multiplier per HydroID\n(uniform shift up or down)"),
        ("Spatial scope",
         "All HydroIDs on the gauged branch\n(uniform within each zone)",
         "Gauge HydroID  ±  8 km network trace\n+ downstream group carry"),
        ("Baseline curve",
         "Raw SRC from Stage 2\n(src_full_crosswalked_{bid}.csv)",
         "Post-subdivision hydroTable\n(bankfull + bathy + subdiv already applied)"),
        ("Output file",
         "src_full_crosswalked_{bid}.csv\n(ManningN + Discharge (m3s-1))",
         "hydroTable_{bid}.csv\n(discharge_cms + calb_coef_usgs)"),
        ("Branches affected",
         "2 of 17  (gauged with RC data only)\n3351000014, 3351000023",
         "Up to all 17 via network propagation\n(any HydroID within 8 km of a gauge)"),
        ("Handles ungauged HydroIDs",
         "No — only directly gauged branches",
         "Yes — network propagation and\nfeature_id-level mean fallback"),
        ("Run order in pipeline",
         "s05 — runs before Stage 4",
         "Stage 4 — runs after s05;\ncalibration_rerun=True resets first"),
    ]

    col_w = [3.40, 5.70, 5.70]
    col_x = [0.30, 3.90, 9.80]
    row_h = 0.61
    y_start = 7.55

    hdr_colors = ["#2c3e50", C_NAVY, C_ORANGE]
    hdr_labels = ["Aspect", "Our Script  (s05)", "Main Repo  (Stage 4 / UsgsRatingCalibrator)"]
    alt_bgs = ["#dce4ec", "#e8f4fc", "#fef5e7"]
    base_bgs = ["#eaeff4", "#f0f7fd", "#fdf3e3"]

    for ci, (lbl, cw, cx) in enumerate(zip(hdr_labels, col_w, col_x)):
        ax.add_patch(FancyBboxPatch(
            (cx, y_start), cw, 0.58,
            boxstyle="square,pad=0", facecolor=hdr_colors[ci],
            edgecolor="none", zorder=3,
        ))
        ax.text(cx + cw / 2, y_start + 0.29, lbl,
                ha="center", va="center", fontsize=10.5, fontweight="bold",
                color="white", zorder=4)

    for ri, row_data in enumerate(rows):
        ry = y_start - (ri + 1) * row_h
        bg_set = alt_bgs if ri % 2 == 0 else base_bgs
        for ci, (cell, cw, cx) in enumerate(zip(row_data, col_w, col_x)):
            ax.add_patch(FancyBboxPatch(
                (cx, ry), cw, row_h - 0.03,
                boxstyle="square,pad=0", facecolor=bg_set[ci],
                edgecolor="#d5d8dc", linewidth=0.6, zorder=3,
            ))
            ax.text(cx + 0.12, ry + row_h / 2, cell,
                    ha="left", va="center", fontsize=8.5,
                    color="#1a1a1a", linespacing=1.35, zorder=4)

    footnote(ax,
             "Both methods draw on the same source data (usgs_rating_curves.parquet + "
             "nwm3_17C_recurrence_flows_cfs.parquet) — they differ in HOW they apply the information.",
             y=0.25)
    save_slide(fig, "slide_05_comparison_table.png")


# ── Slide 06 — The Collision ──────────────────────────────────────────────────

def slide_06():
    fig, ax = new_slide()
    header_bar(ax,
               "Critical Issue — Calibration Collision When Stage 4 Runs",
               subtitle="calibration_rerun=True  in s07_stage4_single_huc.py  triggers HydroTableReset, "
                        "which erases s05 edits",
               slide_n="06 / 07")

    content_box(ax, 0.25, 4.05, 7.35, 3.65,
                "Plain Language",
                [
                    "Stage 4 is currently configured to RESET everything",
                    "back to defaults before it runs its own calibration.",
                    "",
                    "The reset reads 'default_ManningN' and 'default_SLOPE'",
                    "— the original, untouched values from Stage 2.",
                    "",
                    "Our s05 script only wrote to 'ManningN' and",
                    "'Discharge (m3s-1)' — it never updated the default_ columns.",
                    "",
                    "So the reset has no way to know we calibrated those values.",
                    "It just overwrites them silently.",
                    "",
                    "Result: if Stage 4 runs, our n-calibration is gone.",
                    "Whoever runs last wins.",
                ],
                C_PLAIN, bg="#fef9e7", body_fs=10.0, line_gap=1.55)

    content_box(ax, 0.25, 1.05, 7.35, 2.75,
                "Technical Detail  (reset.py  lines 77–169)",
                [
                    "HydroTableReset._reset_one() reads:",
                    "  src_base_{bid}.csv  +  full['default_SLOPE']",
                    "                      +  full['default_ManningN']",
                    "Then recomputes:",
                    "  Q = A·R^(2/3)·sqrt(default_SLOPE) / default_ManningN",
                    "Then overwrites in src_full_crosswalked_{bid}.csv:",
                    "  ManningN  ← default_ManningN",
                    "  Discharge (m3s-1)  ← recomputed Q",
                    "",
                    "s05 writes ONLY:  ManningN,  Discharge (m3s-1)",
                    "s05 NEVER writes: default_ManningN,  default_SLOPE",
                    "→ reset has no memory that s05 ran; it reverts blindly",
                ],
                C_TECH, bg="#fce4e4", body_fs=9.2, line_gap=1.52)

    # ── Flow diagram ──────────────────────────────────────────────────────────
    fx = 8.10
    steps = [
        (fx + 3.7, 7.40, "s05 runs", C_TEAL,
         "Writes calibrated n\nto src_full_crosswalked_*.csv"),
        (fx + 3.7, 5.75, "Stage 4 starts\n(calibration_rerun=True)", C_WARN,
         "s07_stage4_single_huc.py  line 46"),
        (fx + 3.7, 4.05, "HydroTableReset fires\n(pipeline.py line 145)", C_WARN,
         "Reads default_ManningN / default_SLOPE\n→ rewrites ManningN + Discharge\n→ s05 work ERASED"),
        (fx + 3.7, 2.20, "Stage 4 calibration runs", C_NAVY,
         "UsgsRatingCalibrator applies\nits own coef to the reset baseline"),
    ]
    for sx, sy, title, col, note in steps:
        ax.add_patch(FancyBboxPatch(
            (sx - 2.60, sy - 0.38), 5.20, 0.82,
            boxstyle="round,pad=0.10", facecolor=col,
            edgecolor="white", linewidth=1.5, alpha=0.88, zorder=4,
        ))
        ax.text(sx, sy + 0.04, title,
                ha="center", va="center", fontsize=9.5, fontweight="bold",
                color="white", zorder=5, linespacing=1.25)
        ax.text(sx + 2.75, sy, note,
                ha="left", va="center", fontsize=8, color="#333",
                linespacing=1.35, zorder=5)

    # Arrows between steps
    for i in range(len(steps) - 1):
        _, y1, _, _, _ = steps[i]
        _, y2, _, _, _ = steps[i + 1]
        ax.annotate("", xy=(steps[i+1][0], y2 + 0.44),
                    xytext=(steps[i][0], y1 - 0.38),
                    arrowprops=dict(arrowstyle="-|>", color="#555", lw=1.5), zorder=3)

    # Three resolution options — side-by-side boxes (full width, bottom strip)
    opt_data = [
        (C_WARN,   "Option A  (not recommended)",
                   "Set calibration_rerun=False",
                   "Skip the reset so Stage 4 stacks on top of s05.\nMethods compound in an uncontrolled way."),
        (C_AMBER,  "Option B  (surgical)",
                   "Disable src_adjust_usgs for gauged branches",
                   "Main repo applies bankfull/subdiv/bathy only.\ns05 n-calibration is the final word on gauged branches."),
        (C_GREEN,  "Option C  (safest)",
                   "Copy s05 n into default_ManningN before Stage 4",
                   "Makes s05 the new baseline so the reset preserves\nour work and Stage 4 calibrates on top of it."),
    ]
    bw = (W - 0.50) / 3         # box width per option
    for oi, (col, tag, title, body) in enumerate(opt_data):
        ox = 0.25 + oi * (bw + 0.02)
        ax.add_patch(FancyBboxPatch(
            (ox, 0.18), bw, 0.95,
            boxstyle="round,pad=0.08", facecolor=col,
            edgecolor="white", linewidth=1.2, alpha=0.90, zorder=3,
        ))
        ax.text(ox + 0.15, 0.99, tag,
                ha="left", va="top", fontsize=8.2, color="white",
                fontweight="bold", zorder=4)
        ax.text(ox + 0.15, 0.76, title,
                ha="left", va="top", fontsize=9.5, color="white",
                fontweight="bold", zorder=4)
        ax.text(ox + 0.15, 0.55, body,
                ha="left", va="top", fontsize=8.0, color="#f0f0f0",
                linespacing=1.40, zorder=4)

    save_slide(fig, "slide_06_collision.png")


# ── Slide 07 — Items Needing Attention ────────────────────────────────────────

def slide_07():
    fig, ax = new_slide()
    header_bar(ax,
               "Items Needing Attention Before Stage 5  (FIM Generation)",
               subtitle="HUC 03020102  |  Checklist of open issues, risks, and design decisions",
               slide_n="07 / 07")

    items = [
        (C_WARN, "CRITICAL — Calibration Collision",
         "Stage 4 (s07) is hardcoded with calibration_rerun=True.\n"
         "HydroTableReset will silently overwrite s05's ManningN / Discharge back to defaults.\n"
         "Decision needed before Stage 4 runs on this HUC (or any of the 24 HUCs)."),

        (C_ORANGE, "SRC Discontinuity — Branch 3351000023",
         "n jumps 8.2× at the 2-yr zone boundary (0.011 → 0.090).\n"
         "This creates a non-monotonic section in the calibrated SRC: discharge decreases with rising stage.\n"
         "Risk: Stage 3–5 inundation mapping may invert the stage–discharge relationship at that level."),

        (C_AMBER, "Gauged Branches With No Rating Curve Data",
         "Branches 3351000015 (Gauge 02083410) and 3351000026 (Gauge 02082835)\n"
         "have USGS gauges crosswalked but no rating curve rows in usgs_rating_curves.parquet.\n"
         "Neither s05 nor Stage 4's UsgsRatingCalibrator can calibrate them → default n=0.06 applies."),

        (C_GRAY, "15 Ungauged Branches — Default n Only",
         "Branches 3351000015–3351000029 (excluding 14 & 23) run on global default n = 0.06 (channel)\n"
         "and n = 0.12 (overbank) from mannings_global_optz.parquet.\n"
         "ML-based n regionalization is future work — not blocking Stage 5, but limits FIM accuracy."),

        (C_PURPLE, "Scale — 24 HUCs Not Yet Tested",
         "The full pipeline (including Stage 4) has only been validated on HUC 03020102.\n"
         "Before running s07_stage4_all_hucs.py across all 24, confirm collision handling\n"
         "and that all HUCs have Stage 2 outputs (branches/ + hydroTable + src_full_crosswalked)."),
    ]

    item_h = 1.28
    y0 = 7.60
    for i, (col, title, body) in enumerate(items):
        iy = y0 - i * (item_h + 0.04)
        ax.add_patch(FancyBboxPatch(
            (0.25, iy - item_h), 15.50, item_h,
            boxstyle="round,pad=0.08", facecolor="#ffffff",
            edgecolor=col, linewidth=2.0, zorder=3,
        ))
        # left accent
        ax.add_patch(FancyBboxPatch(
            (0.25, iy - item_h), 0.18, item_h,
            boxstyle="square,pad=0", facecolor=col,
            edgecolor="none", zorder=4,
        ))
        ax.text(0.55, iy - 0.30, title,
                ha="left", va="top", fontsize=10.5, fontweight="bold",
                color=col, zorder=5)
        ax.text(0.55, iy - 0.62, body,
                ha="left", va="top", fontsize=9.0, color="#222",
                linespacing=1.50, zorder=5)

    save_slide(fig, "slide_07_items_attention.png")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Saving slides to: {SAVE_TO}")
    slide_01()
    slide_02()
    slide_03()
    slide_04()
    slide_05()
    slide_06()
    slide_07()
    print(f"\nDone — {SAVE_TO}")
