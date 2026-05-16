"""
Subtract bathymetric depth from SRC channel cross sections using USACE eHydro
+ AI-based bathymetry rasters. Not yet ported.

Reference: inundation-mapping/src/bathymetric_adjustment.py (635 lines)
"""

from __future__ import annotations

from typing import Optional, Union
from pathlib import Path

from ._stub import not_yet_ported


def bathymetric_adjustment(
    aoi_dir: Union[str, Path],
    bathy_file_ehydro: Union[str, Path],
    bathy_file_aibased: Union[str, Path],
    ai_toggle: int = 0,
) -> None:
    not_yet_ported("bathymetric_adjustment", "bathymetric_adjustment.py")
