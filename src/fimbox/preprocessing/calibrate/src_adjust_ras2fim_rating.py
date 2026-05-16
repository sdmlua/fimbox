"""
Calibrate ManningN against RAS2FIM cross-section rating curves. Not yet ported.

Reference: inundation-mapping/src/src_adjust_ras2fim_rating.py (393 lines)
"""

from __future__ import annotations

from typing import Union
from pathlib import Path

from ._stub import not_yet_ported


def src_adjust_ras2fim_rating(
    aoi_dir: Union[str, Path],
    ras_rating_curve_csv: Union[str, Path],
    nwm_recur_file: Union[str, Path],
    n_workers: int = 1,
) -> None:
    not_yet_ported("src_adjust_ras2fim_rating", "src_adjust_ras2fim_rating.py")
