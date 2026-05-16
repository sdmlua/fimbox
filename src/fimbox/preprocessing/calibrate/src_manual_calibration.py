"""
Author: Supath Dhital
Date Updated: May 2026

Apply a user-supplied per-feature_id ManningN calibration coefficient to the
per-branch hydroTables. Port of inundation-mapping's ``src_manual_calibration.py``.

Coefficient semantics
---------------------
    coef in (0, 1)   --> increases discharge (decreases inundation)
    coef = 1         --> no change
    coef > 1         --> decreases discharge (increases inundation)

The transformation is:

    discharge_cms = pre_manual_calb_discharge_cms / calb_coef_manual

The pre-calibration hydroTable is saved as ``htable_pre_manual_calib_*.csv``
in the same branch directory before being overwritten.

Inputs
------
aoi_dir
    AOI output directory containing ``branches/<branch_id>/hydroTable_<branch_id>.csv``.
calibration_file
    CSV with columns: ``aoi_id`` (str — falls back to ``HUC8`` for
    backwards compatibility with inundation-mapping manual_calibration_coefficients.csv),
    ``feature_id`` (int), ``calb_coef_manual`` (float > 0).

Outputs (per branch)
--------------------
hydroTable_{id}.csv               - overwritten with calibrated discharge
htable_pre_manual_calib_{id}.csv  - backup of the original

Raises
------
ValueError  - when any coefficient is <= 0
FileNotFoundError  - when the calibration file does not exist
"""

from __future__ import annotations

import glob
import logging
import shutil
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd

from .aggregate_branches_to_aoi import HYDROTABLE_DTYPES

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def manual_calibration(
    aoi_dir: Optional[PathLike] = None,
    calibration_file: Optional[PathLike] = None,
    *,
    huc_dir: Optional[PathLike] = None,
) -> None:
    """Apply manual ManningN calibration coefficients.

    ``aoi_dir`` and ``huc_dir`` are equivalent — pass whichever matches your
    workflow."""
    from ._stub import resolve_aoi_dir

    aoi_dir = Path(resolve_aoi_dir(aoi_dir, huc_dir))
    if calibration_file is None:
        raise TypeError("calibration_file= is required.")
    calibration_file = Path(calibration_file)
    if not calibration_file.exists():
        raise FileNotFoundError(f"No calibration file at {calibration_file}")

    aoi_id = aoi_dir.name
    log.info(f"--- manual_calibration: {aoi_id} ---")

    calib = pd.read_csv(calibration_file, index_col=False)
    if "Unnamed: 0" in calib.columns:
        calib = calib.drop(columns=["Unnamed: 0"])

    # Accept either an "aoi_id" column (preferred) or "HUC8" (legacy
    # inundation-mapping format). Normalise to a single "aoi_id" column.
    if "aoi_id" in calib.columns:
        calib["aoi_id"] = calib["aoi_id"].astype(str)
    elif "HUC8" in calib.columns:
        calib = calib.rename(columns={"HUC8": "aoi_id"})
        calib["aoi_id"] = calib["aoi_id"].astype(str)
    else:
        raise ValueError(
            "Manual calibration CSV must have either an 'aoi_id' or 'HUC8' column."
        )
    calib["feature_id"] = calib["feature_id"].astype(int)
    calib["calb_coef_manual"] = calib["calb_coef_manual"].astype(float)

    min_coef = calib["calb_coef_manual"].min()
    if min_coef <= 0:
        raise ValueError(
            f"Manual calibration coefficients must be > 0. Minimum found: {min_coef}"
        )

    if aoi_id not in calib["aoi_id"].unique():
        log.info(f"No manual calibration entry for AOI {aoi_id} — skipping")
        return

    branches_dir = aoi_dir / "branches"
    htable_files = sorted(
        Path(p) for p in glob.glob(str(branches_dir / "**" / "hydroTable_*.csv"), recursive=True)
    )

    for ht_file in htable_files:
        backup = ht_file.with_name(ht_file.name.replace("hydroTable_", "htable_pre_manual_calib_"))
        shutil.copyfile(ht_file, backup)

        df = pd.read_csv(ht_file, dtype=HYDROTABLE_DTYPES, index_col=False)

        # Strip a few legacy / leftover columns
        for col in ("postcalb_discharge_cms", "calb_coef_manual", "HydroID Int16"):
            if col in df.columns:
                df = df.drop(columns=[col])

        df["pre_manual_calb_discharge_cms"] = df["discharge_cms"]
        df = df.merge(calib, how="left", on="feature_id")
        df = df.drop(columns=["aoi_id", "HUC8"], errors="ignore")

        df["discharge_cms"] = np.where(
            df["calb_coef_manual"].isnull(),
            df["pre_manual_calb_discharge_cms"],
            df["pre_manual_calb_discharge_cms"] / df["calb_coef_manual"],
        )
        df.to_csv(ht_file, index=False)
        log.info(f"Applied manual calibration --> {ht_file.name}")


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(description="Apply manual ManningN calibration.")
    parser.add_argument("-aoi_dir", required=True)
    parser.add_argument("-calb_file", "--calibration_file", required=True)
    args = parser.parse_args()
    manual_calibration(args.aoi_dir, args.calibration_file)
