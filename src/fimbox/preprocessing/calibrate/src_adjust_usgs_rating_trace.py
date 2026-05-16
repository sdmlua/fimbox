"""
Calibrate ManningN against USGS rating-curve traces at NWM recurrence flows.
Not yet ported.

Reference: inundation-mapping/src/src_adjust_usgs_rating_trace.py (595 lines)
"""

from __future__ import annotations

from typing import Union
from pathlib import Path

from ._stub import not_yet_ported


def src_adjust_usgs_rating_trace(
    aoi_dir: Union[str, Path],
    usgs_rating_curve_csv: Union[str, Path],
    usgs_acceptable_gages: Union[str, Path],
    nwm_recur_file: Union[str, Path],
    n_workers: int = 1,
) -> None:
    not_yet_ported("src_adjust_usgs_rating_trace", "src_adjust_usgs_rating_trace.py")
