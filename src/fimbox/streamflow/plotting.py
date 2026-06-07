"""
Author: Supath Dhital
Date Created: June 2026

Streamflow plots (500 DPI) saved to ``<AOI>/watershed-data/plots/``:
  * NWM retrospective time series for one or more feature_ids
  * USGS time series for one or more sites
  * NWM-vs-USGS comparison overlay

All read the parquet archives written by the retrieval classes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence, Union

import pandas as pd

from . import _common as C

log = logging.getLogger(__name__)

PathLike = Union[str, Path]
_NWM_PREFIX = "nwm30-"
_USGS_PREFIX = "usgs-"


def _plots_dir(aoi_dir: PathLike) -> Path:
    return C.plots_dir(aoi_dir)


def _nwm_series(
    aoi_dir: PathLike, feature_id: int, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    parquet = (
        C.streamflow_dir(aoi_dir, "nwm30_retrospective")
        / f"{start_date.replace('-', '')}_{end_date.replace('-', '')}.parquet"
    )
    if not parquet.exists():
        return None
    df = pd.read_parquet(parquet)
    rows = df[df["location_id"] == f"{_NWM_PREFIX}{feature_id}"]
    if rows.empty:
        return None
    return rows[["value_time", "value"]].rename(
        columns={"value_time": "Date", "value": "Discharge"}
    )


def _usgs_series(
    aoi_dir: PathLike, site: str, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    parquet = C.streamflow_dir(aoi_dir, "usgs") / f"{start_date}_{end_date}.parquet"
    if not parquet.exists():
        return None
    df = pd.read_parquet(parquet)
    rows = df[df["location_id"] == f"{_USGS_PREFIX}{site}"]
    if rows.empty:
        return None
    return rows[["value_time", "value"]].rename(
        columns={"value_time": "Date", "value": "Discharge"}
    )


def plot_nwm(
    aoi_dir: PathLike,
    feature_ids: Sequence[int],
    start_date: str,
    end_date: str,
) -> Optional[Path]:
    """Overlay NWM retrospective series for the given feature_ids."""
    plt = C.require("matplotlib").pyplot
    plt.figure(figsize=(10, 5))
    plotted = []
    for fid in feature_ids:
        s = _nwm_series(aoi_dir, fid, start_date, end_date)
        if s is None or s.empty:
            log.warning("No NWM data for feature_id %s", fid)
            continue
        plt.plot(s["Date"], s["Discharge"], linewidth=2, label=f"NWM {fid}")
        plotted.append(fid)
    if not plotted:
        plt.close()
        return None
    return _finish(plt, "NWM hourly streamflow", _plots_dir(aoi_dir) / f"NWM_{plotted[0]}.png")


def plot_usgs(
    aoi_dir: PathLike,
    sites: Sequence[str],
    start_date: str,
    end_date: str,
) -> Optional[Path]:
    """Overlay USGS series for the given sites."""
    plt = C.require("matplotlib").pyplot
    plt.figure(figsize=(10, 5))
    plotted = []
    for site in sites:
        s = _usgs_series(aoi_dir, site, start_date, end_date)
        if s is None or s.empty:
            log.warning("No USGS data for site %s", site)
            continue
        plt.plot(s["Date"], s["Discharge"], linewidth=2, label=f"USGS {site}")
        plotted.append(site)
    if not plotted:
        plt.close()
        return None
    return _finish(plt, "USGS hourly streamflow", _plots_dir(aoi_dir) / f"USGS_{plotted[0]}.png")


def plot_comparison(
    aoi_dir: PathLike,
    feature_id: int,
    usgs_site: str,
    start_date: str,
    end_date: str,
) -> Optional[Path]:
    """NWM (feature_id) vs USGS (site) overlay for the same window."""
    plt = C.require("matplotlib").pyplot
    nwm = _nwm_series(aoi_dir, feature_id, start_date, end_date)
    usgs = _usgs_series(aoi_dir, usgs_site, start_date, end_date)
    if nwm is None or usgs is None:
        log.warning("Comparison needs both NWM (%s) and USGS (%s) series", feature_id, usgs_site)
        return None
    plt.figure(figsize=(10, 5))
    plt.plot(nwm["Date"], nwm["Discharge"], color="#167693", linewidth=2,
             label=f"NWM {feature_id}")
    plt.plot(usgs["Date"], usgs["Discharge"], color="#BF4037", linestyle="dashed",
             linewidth=2, label=f"USGS {usgs_site}")
    return _finish(
        plt,
        "NWM vs USGS streamflow",
        _plots_dir(aoi_dir) / f"NWMvsUSGS_{usgs_site}.png",
    )


def _finish(plt, title: str, out_path: Path) -> Path:
    plt.xlabel("Date (Hourly)", fontsize=14)
    plt.ylabel("Discharge (m³/s)", fontsize=14)
    plt.title(title, fontsize=16)
    plt.xticks(rotation=45, fontsize=12)
    plt.yticks(fontsize=12)
    plt.grid(True, linestyle="-", linewidth=0.3)
    plt.legend()
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=500, bbox_inches="tight")
    plt.close()
    log.info("Plot --> %s", out_path)
    return out_path
