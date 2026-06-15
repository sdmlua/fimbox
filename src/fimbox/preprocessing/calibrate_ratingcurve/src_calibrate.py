"""
Author: Supath Dhital
Date Updated: May 2026

Per-feature_id Manning's n calibration. Manual coefficients are fully
implemented; USGS rating-curve / ras2fim / spatial-observation
calibration routines are stubs awaiting validation against a real AOI.
"""

from __future__ import annotations

import glob
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ._common import (
    HYDROTABLE_DTYPES,
    PathLike,
    aoi_id_of,
    not_yet_ported,
    resolve_aoi_dir,
)

log = logging.getLogger(__name__)


# Manual calibration
@dataclass
class ManualCalibrator:
    # Apply a per-feature_id coefficient to each per-branch hydroTable.
    #
    # Coefficient semantics:
    #   coef in (0, 1)  -> increases discharge (decreases inundation)
    #   coef = 1        -> no change
    #   coef > 1        -> decreases discharge (increases inundation)
    #
    # The transformation is:
    #     discharge_cms = pre_manual_calb_discharge_cms / calb_coef_manual
    #
    # The pre-calibration hydroTable is saved as
    # ``htable_pre_manual_calib_<bid>.csv`` next to the hydroTable before
    # being overwritten.

    aoi_dir: PathLike
    calibration_file: PathLike

    def run(self) -> None:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        calib_path = Path(self.calibration_file)
        if not calib_path.exists():
            raise FileNotFoundError(f"No calibration file at {calib_path}")

        aoi_id = aoi_id_of(aoi_dir)
        log.info(f"--- ManualCalibrator: {aoi_id} ---")

        calib = pd.read_csv(calib_path, index_col=False)
        if "Unnamed: 0" in calib.columns:
            calib = calib.drop(columns=["Unnamed: 0"])

        # Accept aoi_id (preferred) or HUC8 (legacy) as the AOI key column.
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
            Path(p)
            for p in glob.glob(
                str(branches_dir / "**" / "hydroTable_*.csv"), recursive=True
            )
        )
        for ht_file in htable_files:
            self._apply(ht_file, calib)

    @staticmethod
    def _apply(ht_file: Path, calib: pd.DataFrame) -> None:
        backup = ht_file.with_name(
            ht_file.name.replace("hydroTable_", "htable_pre_manual_calib_")
        )
        shutil.copyfile(ht_file, backup)

        df = pd.read_csv(ht_file, dtype=HYDROTABLE_DTYPES, index_col=False)
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


def manual_calibration(
    aoi_dir: Optional[PathLike] = None,
    calibration_file: Optional[PathLike] = None,
    *,
    huc_dir: Optional[PathLike] = None,
) -> None:
    if calibration_file is None:
        raise TypeError("calibration_file= is required.")
    ManualCalibrator(
        aoi_dir=resolve_aoi_dir(aoi_dir, huc_dir),
        calibration_file=calibration_file,
    ).run()


# SRC calibration against reference rating curves
@dataclass
class UsgsRatingCalibrator:
    # SRC optimisation against USGS rating-curve database (WSE/flow at NWM recurrence intervals). Not yet ported.
    aoi_dir: PathLike
    usgs_rating_curve_csv: PathLike
    usgs_acceptable_gages: PathLike
    nwm_recur_file: PathLike
    n_workers: int = 1

    def run(self) -> None:
        not_yet_ported("UsgsRatingCalibrator")


@dataclass
class Ras2fimCalibrator:
    # SRC optimisation against HEC-RAS cross-section ratings at NWM recurrence intervals. Not yet ported.
    aoi_dir: PathLike
    ras_rating_curve_csv: PathLike
    nwm_recur_file: PathLike
    n_workers: int = 1

    def run(self) -> None:
        not_yet_ported("Ras2fimCalibrator")


@dataclass
class SpatialObsCalibrator:
    # SRC optimisation against benchmark inundation extent parquet files.
    aoi_dir: PathLike
    n_workers: int = 1

    def run(self) -> None:
        not_yet_ported("SpatialObsCalibrator")
