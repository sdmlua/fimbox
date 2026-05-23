"""
Author: Supath Dhital
Date Updated: May 2026

DEM/discharge-side adjustments applied before the SRC subdivision step.
All three routines are stubs awaiting validation against a real AOI; they
raise CalibrationNotImplemented so the pipeline can skip them cleanly.

  ThalwegNotchesAdjustment   smooth artifacts where streams cross DEM cells
  LongitudinalFlowFilter     enforce that discharge is non-decreasing
                              downstream along reach chains
  BathymetricAdjustment      deepen channels using eHydro / AI-bathy depths
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ._common import PathLike, not_yet_ported


@dataclass
class ThalwegNotchesAdjustment:
    aoi_dir: PathLike

    def run(self) -> None:
        not_yet_ported("ThalwegNotchesAdjustment")


@dataclass
class LongitudinalFlowFilter:
    aoi_dir: PathLike

    def run(self) -> None:
        not_yet_ported("LongitudinalFlowFilter")


@dataclass
class BathymetricAdjustment:
    aoi_dir: PathLike
    bathy_file_ehydro: Optional[PathLike] = None
    bathy_file_aibased: Optional[PathLike] = None
    ai_toggle: int = 0

    def run(self) -> None:
        not_yet_ported("BathymetricAdjustment")
