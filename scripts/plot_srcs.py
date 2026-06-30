"""
Plot Synthetic Rating Curves (SRCs) for a single HUC8 after Stage 2.

Reads all hydroTable_*.csv files from every branch, then produces two figures:
  1. Overview — all SRCs on one axes, colored by Strahler order
  2. Branch grid — one subplot per branch showing individual reach SRCs

Run:
    .venv\\Scripts\\python.exe scripts/plot_srcs.py
"""
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np

# ── CONFIG ────────────────────────────────────────────────────────
HUC8    = "03020102"
OUT_DIR = Path("D:/SI/out")
SAVE_TO = Path("D:/SI/out") / f"HUC{HUC8}" / "src_plots"
# ─────────────────────────────────────────────────────────────────

WATERSHED = OUT_DIR / f"HUC{HUC8}" / "watershed-data"
BRANCHES  = WATERSHED / "branches"


def load_all_hydrotables() -> pd.DataFrame:
    frames = []
    for branch_dir in sorted(BRANCHES.iterdir()):
        if not branch_dir.is_dir():
            continue
        branch_id = branch_dir.name
        htable = branch_dir / f"hydroTable_{branch_id}.csv"
        if not htable.exists():
            continue
        df = pd.read_csv(htable, dtype={"feature_id": str})
        df["branch_id"] = branch_id
        frames.append(df)
    if not frames:
        raise FileNotFoundError(f"No hydroTable CSVs found under {BRANCHES}")
    return pd.concat(frames, ignore_index=True)


def _order_cmap(orders):
    max_order = max(orders) if orders else 5
    cmap = plt.colormaps["plasma_r"].resampled(max_order)
    return {o: mcolors.to_hex(cmap(o / max_order)) for o in range(1, max_order + 1)}


def plot_overview(df: pd.DataFrame, save_to: Path) -> None:
    """All SRCs on one axes, colored by Strahler stream order."""
    fig, ax = plt.subplots(figsize=(10, 7))

    orders = sorted(df["order_"].dropna().unique().astype(int))
    colors = _order_cmap(orders)

    for order in orders:
        subset = df[df["order_"] == order]
        for _, grp in subset.groupby("HydroID"):
            grp_s = grp.sort_values("stage")
            ax.plot(
                grp_s["discharge_cms"],
                grp_s["stage"],
                color=colors[order],
                alpha=0.25,
                linewidth=0.7,
            )

    # Legend — one proxy per order
    handles = [
        plt.Line2D([0], [0], color=colors[o], linewidth=1.5, label=f"Order {o}")
        for o in orders
    ]
    ax.legend(handles=handles, title="Strahler Order", loc="upper left", fontsize=8)

    ax.set_xlabel("Discharge (m³/s)", fontsize=11)
    ax.set_ylabel("Stage (m)", fontsize=11)
    ax.set_title(f"Synthetic Rating Curves — HUC8 {HUC8}\n"
                 f"({df['HydroID'].nunique()} reaches across "
                 f"{df['branch_id'].nunique()} branches)",
                 fontsize=12)
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out = save_to / "src_overview.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Saved: {out}")


def plot_branch_grid(df: pd.DataFrame, save_to: Path) -> None:
    """One subplot per branch, each reach as a separate line."""
    branches = sorted(df["branch_id"].unique())
    n = len(branches)
    ncols = 4
    nrows = int(np.ceil(n / ncols))

    fig, axes = plt.subplots(nrows, ncols, figsize=(ncols * 4, nrows * 3.5),
                             sharex=False, sharey=False)
    axes_flat = np.array(axes).flatten()

    orders_global = sorted(df["order_"].dropna().unique().astype(int))
    colors = _order_cmap(orders_global)

    for i, branch_id in enumerate(branches):
        ax = axes_flat[i]
        bdf = df[df["branch_id"] == branch_id]

        for order in orders_global:
            subset = bdf[bdf["order_"] == order]
            for _, grp in subset.groupby("HydroID"):
                grp_s = grp.sort_values("stage")
                ax.plot(
                    grp_s["discharge_cms"],
                    grp_s["stage"],
                    color=colors[order],
                    alpha=0.5,
                    linewidth=0.9,
                )

        ax.set_title(f"Branch {branch_id}\n({bdf['HydroID'].nunique()} reaches)",
                     fontsize=7)
        ax.set_xlabel("Q (m³/s)", fontsize=7)
        ax.set_ylabel("Stage (m)", fontsize=7)
        ax.tick_params(labelsize=6)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.3)

    # Hide unused subplots
    for j in range(i + 1, len(axes_flat)):
        axes_flat[j].set_visible(False)

    # Shared legend at the bottom
    handles = [
        plt.Line2D([0], [0], color=colors[o], linewidth=1.5, label=f"Order {o}")
        for o in orders_global
    ]
    fig.legend(handles=handles, title="Strahler Order",
               loc="lower right", ncol=len(orders_global), fontsize=8)

    fig.suptitle(f"SRCs by Branch — HUC8 {HUC8}", fontsize=13, y=1.01)
    fig.tight_layout()

    out = save_to / "src_by_branch.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out}")


def main():
    SAVE_TO.mkdir(parents=True, exist_ok=True)

    print(f"Loading hydroTables from {BRANCHES} ...")
    df = load_all_hydrotables()
    print(f"  {len(df):,} rows | {df['HydroID'].nunique()} reaches | "
          f"{df['branch_id'].nunique()} branches")

    plot_overview(df, SAVE_TO)
    plot_branch_grid(df, SAVE_TO)

    print("\nDone. Plots saved to:", SAVE_TO)


if __name__ == "__main__":
    main()
