"""
Author: Supath Dhital
Date Updated: June 2026

Streamflow retrieval, plotting, and statistics.

Sources
-------
NWMRetrospective   NWM v3.0 retrospective hourly streamflow
NWMForecast        NWM operational short/medium/long-range forecast (netCDF)
USGSData           USGS gage observations
GeoglowsData       GEOGLOWS v2 retrospective (S3 zarr)

Orchestration
-------------
StreamflowPipeline retrieve/select streamflow -> FIM-ready CSVs in discharge-inputs/

Analysis
--------
plot_nwm / plot_usgs / plot_comparison   500-DPI figures under watershed-data/plots/
calculate_statistics                     KGE / NSE / PBias (NWM vs USGS)
"""

from __future__ import annotations

from .geoglows import GeoglowsData
from .nwm_forecast import NWMForecast, getNWMforecast
from .nwm_retrospective import NWMRetrospective, getNWMretrospective
from .pipeline import StreamflowPipeline
from .plotting import plot_comparison, plot_nwm, plot_usgs
from .statistics import StreamflowMetrics, calculate_statistics, compute_metrics
from .usgs import USGSData, get_usgs_fid_pairs

__all__ = [
    # classes
    "NWMRetrospective",
    "NWMForecast",
    "USGSData",
    "GeoglowsData",
    "StreamflowPipeline",
    # function wrappers (FIMserv-style)
    "getNWMretrospective",
    "getNWMforecast",
    "get_usgs_fid_pairs",
    # plotting + statistics
    "plot_nwm",
    "plot_usgs",
    "plot_comparison",
    "calculate_statistics",
    "compute_metrics",
    "StreamflowMetrics",
]
