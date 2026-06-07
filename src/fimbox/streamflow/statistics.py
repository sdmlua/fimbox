"""
Author: Supath Dhital
Date Created: June 2026

Compare NWM retrospective vs USGS observed streamflow for a (feature_id, site)
pair and report KGE, NSE, and percentage bias. Optionally renders a metrics
bar chart into ``<AOI>/streamflow/plots/``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd

from . import _common as C
from .plotting import _nwm_series, _usgs_series, _plots_dir

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


@dataclass
class StreamflowMetrics:
    feature_id: int
    usgs_site: str
    n_obs: int
    kge: float
    nse: float
    pbias_pct: float

    def as_dict(self) -> dict:
        return {
            "feature_id": self.feature_id,
            "usgs_site": self.usgs_site,
            "n_obs": self.n_obs,
            "KGE": self.kge,
            "NSE": self.nse,
            "PBias (%)": self.pbias_pct,
        }


def compute_metrics(nwm: np.ndarray, usgs: np.ndarray) -> dict:
    """KGE, NSE, and percent-bias between paired NWM and USGS arrays."""
    nwm = np.asarray(nwm, dtype=float)
    usgs = np.asarray(usgs, dtype=float)
    r = np.corrcoef(nwm, usgs)[0, 1]
    beta = np.mean(nwm) / np.mean(usgs)
    gamma = np.std(nwm) / np.std(usgs)
    kge = 1 - np.sqrt((r - 1) ** 2 + (beta - 1) ** 2 + (gamma - 1) ** 2)
    nse = 1 - np.sum((nwm - usgs) ** 2) / np.sum((usgs - np.mean(usgs)) ** 2)
    pbias = np.sum(np.abs(nwm - usgs)) / np.sum(usgs) * 100
    return {"KGE": float(kge), "NSE": float(nse), "PBias (%)": float(pbias)}


def calculate_statistics(
    aoi_dir: PathLike,
    feature_id: int,
    usgs_site: str,
    start_date: str,
    end_date: str,
    *,
    plot: bool = True,
) -> StreamflowMetrics:
    """Merge NWM and USGS series on time, compute metrics, and (optionally)
    save a bar chart. Both series must already be archived under
    ``<AOI>/streamflow/`` (run the retrieval classes first)."""
    nwm = _nwm_series(aoi_dir, feature_id, start_date, end_date)
    usgs = _usgs_series(aoi_dir, usgs_site, start_date, end_date)
    if nwm is None or usgs is None:
        raise FileNotFoundError(
            "Both NWM and USGS series must be fetched first "
            f"(feature_id={feature_id}, usgs_site={usgs_site})."
        )

    nwm["Date"] = pd.to_datetime(nwm["Date"])
    usgs["Date"] = pd.to_datetime(usgs["Date"])
    merged = pd.merge(
        nwm.rename(columns={"Discharge": "nwm"}),
        usgs.rename(columns={"Discharge": "usgs"}),
        on="Date",
    ).dropna()
    if merged.empty:
        raise ValueError("NWM and USGS series do not overlap in time.")

    m = compute_metrics(merged["nwm"].values, merged["usgs"].values)
    metrics = StreamflowMetrics(
        feature_id=int(feature_id),
        usgs_site=str(usgs_site),
        n_obs=len(merged),
        kge=m["KGE"],
        nse=m["NSE"],
        pbias_pct=m["PBias (%)"],
    )
    log.info(
        "Metrics feature_id=%s usgs=%s | KGE=%.3f NSE=%.3f PBias=%.1f%% (n=%d)",
        feature_id, usgs_site, metrics.kge, metrics.nse, metrics.pbias_pct, metrics.n_obs,
    )

    if plot:
        _plot_metrics(aoi_dir, m, usgs_site)
    return metrics


def _plot_metrics(aoi_dir: PathLike, m: dict, usgs_site: str) -> Path:
    plt = C.require("matplotlib").pyplot
    names = ["KGE", "NSE", "PBias (%)"]
    fig, ax1 = plt.subplots(figsize=(6, 4))
    ax1.bar(names[:2], [m["KGE"], m["NSE"]], color=["tab:blue", "tab:purple"],
            edgecolor="black", width=0.7, zorder=3)
    ax1.set_ylabel("Metric value", fontsize=13)
    ax1.set_ylim(0, 1.1)
    ax1.grid(axis="y", linestyle="--", alpha=0.6, zorder=0)
    ax2 = ax1.twinx()
    ax2.bar([names[2]], [m["PBias (%)"]], color="tab:orange", edgecolor="black",
            width=0.7, zorder=3)
    ax2.set_ylabel("Percentage bias (%)", color="tab:orange", fontsize=13)
    plt.title(f"NWM vs USGS metrics ({usgs_site})", fontsize=14)
    plt.tight_layout()
    out = _plots_dir(aoi_dir) / f"metrics_{usgs_site}.png"
    plt.savefig(out, dpi=500, bbox_inches="tight")
    plt.close()
    log.info("Metrics plot --> %s", out)
    return out
