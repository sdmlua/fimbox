"""
Author: Supath Dhital
Date Updated: May 2026

Synthetic rating curve calibration subpackage.

Public surface
---------
CalibrationConfig, Calibrator, run_calibration   pipeline orchestrator
BranchAggregator, aggregate_branches             per-branch -> AOI rollup
HydroTableReset, reset_hydro_and_src             baseline reset for reruns
ManualCalibrator, manual_calibration             per-feature_id manual ManningN
SrcBankfull                                       identify bankfull stage
SrcSubdiv                                         channel/overbank subdivision
SrcNonmonotonic                                   force monotonic rating curves
ThalwegNotchesAdjustment                          remove thalweg-notch artifacts
LongitudinalFlowFilter                            smooth geometry along reaches
BathymetricAdjustment                             add eHydro / AI channel depth

Stubs
---------
UsgsRatingCalibrator, Ras2fimCalibrator, SpatialObsCalibrator
"""

from __future__ import annotations

from ._common import CalibrationNotImplemented
from .aggregate import BranchAggregator, aggregate_branches
from .dem_adjust import (
    BathymetricAdjustment,
    LongitudinalFlowFilter,
    ThalwegNotchesAdjustment,
)
from .logscan import LogScanner, scan_logs
from .pipeline import CalibrationConfig, Calibrator, run_calibration
from .reset import HydroTableReset, reset_hydro_and_src
from .src_adjust import SrcBankfull, SrcNonmonotonic, SrcSubdiv
from .src_calibrate import (
    ManualCalibrator,
    Ras2fimCalibrator,
    SpatialObsCalibrator,
    UsgsRatingCalibrator,
    manual_calibration,
)


# Function-style aliases so callers that prefer ``identify_src_bankfull(...)``
# over ``SrcBankfull(...).run()`` get a one-line entry point. Each just
# instantiates the class and calls .run().
def identify_src_bankfull(aoi_dir, bankfull_flows_file, *, n_workers: int = 1):
    return SrcBankfull(
        aoi_dir=aoi_dir, bankfull_flows_file=bankfull_flows_file, n_workers=n_workers
    ).run()


def subdiv_chan_obank_src(aoi_dir, vmann_table, *, n_workers: int = 1):
    return SrcSubdiv(
        aoi_dir=aoi_dir, vmann_table=vmann_table, n_workers=n_workers
    ).run()


def nonmonotonic_src_adjustment(aoi_dir):
    return SrcNonmonotonic(aoi_dir=aoi_dir).run()


def thalweg_notches_adjustment(aoi_dir, *, n_workers: int = 1):
    return ThalwegNotchesAdjustment(aoi_dir=aoi_dir, n_workers=n_workers).run()


def longitudinal_flow_adjustment(aoi_dir, *, n_workers: int = 1):
    return LongitudinalFlowFilter(aoi_dir=aoi_dir, n_workers=n_workers).run()


def bathymetric_adjustment(
    aoi_dir,
    bathy_file_ehydro=None,
    bathy_file_aibased=None,
    *,
    ai_toggle: int = 0,
    ai_strm_order: int = 4,
):
    return BathymetricAdjustment(
        aoi_dir=aoi_dir,
        bathy_file_ehydro=bathy_file_ehydro,
        bathy_file_aibased=bathy_file_aibased,
        ai_toggle=ai_toggle,
        ai_strm_order=ai_strm_order,
    ).run()


def src_adjust_usgs_rating_trace(
    aoi_dir,
    usgs_rating_curve_csv,
    nwm_recur_file,
    usgs_acceptable_gages=None,
    *,
    n_workers: int = 1,
):
    return UsgsRatingCalibrator(
        aoi_dir=aoi_dir,
        usgs_rating_curve_csv=usgs_rating_curve_csv,
        nwm_recur_file=nwm_recur_file,
        usgs_acceptable_gages=usgs_acceptable_gages,
        n_workers=n_workers,
    ).run()


def src_adjust_ras2fim_rating(
    aoi_dir, ras_rating_curve_csv, nwm_recur_file, *, n_workers: int = 1
):
    return Ras2fimCalibrator(
        aoi_dir=aoi_dir,
        ras_rating_curve_csv=ras_rating_curve_csv,
        nwm_recur_file=nwm_recur_file,
        n_workers=n_workers,
    ).run()


def src_adjust_spatial_obs(aoi_dir, calib_points_file=None, *, n_workers: int = 1):
    return SpatialObsCalibrator(
        aoi_dir=aoi_dir, calib_points_file=calib_points_file, n_workers=n_workers
    ).run()


__all__ = [
    # pipeline
    "CalibrationConfig",
    "Calibrator",
    "run_calibration",
    "CalibrationNotImplemented",
    # aggregator + reset + log scan
    "BranchAggregator",
    "aggregate_branches",
    "HydroTableReset",
    "reset_hydro_and_src",
    "LogScanner",
    "scan_logs",
    # SRC adjustments (implemented)
    "SrcBankfull",
    "SrcSubdiv",
    "SrcNonmonotonic",
    # SRC calibration (manual implemented; others stubbed)
    "ManualCalibrator",
    "manual_calibration",
    "UsgsRatingCalibrator",
    "Ras2fimCalibrator",
    "SpatialObsCalibrator",
    # DEM-side stubs
    "ThalwegNotchesAdjustment",
    "LongitudinalFlowFilter",
    "BathymetricAdjustment",
    # function-style aliases
    "identify_src_bankfull",
    "subdiv_chan_obank_src",
    "nonmonotonic_src_adjustment",
    "thalweg_notches_adjustment",
    "longitudinal_flow_adjustment",
    "bathymetric_adjustment",
    "src_adjust_usgs_rating_trace",
    "src_adjust_ras2fim_rating",
    "src_adjust_spatial_obs",
]
