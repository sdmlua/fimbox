"""
Author: Supath Dhital
Date Updated: May 2026

Flood inundation map (FIM) generation subpackage.

Consumes the per-branch HAND + hydroTable outputs produced by the
preprocessing pipeline and turns a discharge forecast (or stage value)
into an inundation extent + depth raster.

Public surface
--------------
FimGenerator         end-to-end orchestrator: forecast -> per-branch -> mosaic
Inundator            per-branch inundation worker
BranchMosaic         combine per-branch rasters into AOI-level outputs
extract_feature_ids  scan an AOI's hydroTables and emit a forecast template CSV
NoForecastMatch      raised when a branch shares no feature_ids with forecast
NWMFimPipeline       default pipeline: NWM streamflow -> discharge-inputs -> FIM
"""

from __future__ import annotations

from .inundator import InundationResult, Inundator, NoForecastMatch
from .mosaic import BranchMosaic, MosaicResult
from .pipeline import (
    FimGenerationResult,
    FimGenerator,
    NWMFimPipeline,
    extract_feature_ids,
)

__all__ = [
    "FimGenerator",
    "FimGenerationResult",
    "Inundator",
    "InundationResult",
    "BranchMosaic",
    "MosaicResult",
    "NoForecastMatch",
    "extract_feature_ids",
    "NWMFimPipeline",
]
