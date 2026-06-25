"""
Author: Supath Dhital
Date Updated: June 2026

Tests for the synthetic rating curve (SRC) calibration pipeline.

Two layers:

  COMBINED ......... test_calibrate_full_pipeline runs the whole thing in a
      single run_calibration() call against the live AOI, with EVERY optional
      CalibrationConfig parameter spelled out so the full surface is visible
      in one place.

  STEP BY STEP ..... one test per stage (thalweg, longitudinal, bathymetry,
      bankfull, subdiv, nonmonotonic, usgs, spatial, log scan) so any single
      step can be run / debugged alone.

It will point into the working version of the AOI and skip when it is absent.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fimbox import CalibrationConfig, run_calibration
from fimbox._dask import _resolve_n_workers
from fimbox.preprocessing.calibrate_ratingcurve import (
    BathymetricAdjustment,
    BranchAggregator,
    HydroTableReset,
    LongitudinalFlowFilter,
    LogScanner,
    ManualCalibrator,
    SpatialObsCalibrator,
    SrcBankfull,
    SrcNonmonotonic,
    SrcSubdiv,
    ThalwegNotchesAdjustment,
    UsgsRatingCalibrator,
)

# Live AOI + input files. Edit these to point at your data; tests skip when the AOI is absent.
AOI_DIR = Path(__file__).resolve().parents[2] / "out" / "test_smallB"

# Bundled lookup tables shipped in the repo (all calibration inputs live here).
DATA = Path(__file__).resolve().parents[1] / "data"

# Bankfull recurrence flows (NWM v3)
BANKFULL_FLOWS_FILE = DATA / "nwm3_high_water_threshold_cms.parquet"

# Optimized variable-roughness Manning's n table (per feature_id channel/overbank n)
VMANN_INPUT_FILE = DATA / "mannings_global_optz.parquet"

# USGS rating-curve calibration. Rating curve + NWM recurrence flows (v3) are
# required; the acceptable-gage quality filter refines which gages qualify.
USGS_RATING_CURVE_CSV = DATA / "usgs_rating_curves.parquet"
NWM_RECUR_FILE = DATA / "nwm3_17C_recurrence_flows_cfs.parquet"
USGS_ACCEPTABLE_GAGES = DATA / "acceptable_sites_for_rating_curves.parquet"

# Bathymetry: eHydro surveyed channels (.gpkg). Supply it in data/, then set:
BATHY_EHYDRO_FILE = DATA / "final_bathymetry_ehydro_ohrfc.gpkg"

# Spatial-observation calibration: per-AOI benchmark points (.parquet). Yet to
# be added — left unset for now.
CALIB_POINTS_FILE = None

# Manual calibration: per-feature_id coefficient CSV. Yet to be added.
MAN_CALB_FILE = None

# Worker count for the branch-parallel routines — auto-sized to the device
JOB_BRANCH_LIMIT = _resolve_n_workers()

_BRANCHES = (
    AOI_DIR / "watershed-data" / "branches"
    if (AOI_DIR / "watershed-data" / "branches").is_dir()
    else AOI_DIR / "branches"
)
_skip_no_aoi = pytest.mark.skipif(
    not _BRANCHES.is_dir(), reason=f"AOI not present: {_BRANCHES}"
)
_skip_no_bankfull = pytest.mark.skipif(
    not BANKFULL_FLOWS_FILE.is_file(), reason=f"missing {BANKFULL_FLOWS_FILE}"
)
_skip_no_bathy = pytest.mark.skipif(
    BATHY_EHYDRO_FILE is None or not Path(BATHY_EHYDRO_FILE).is_file(),
    reason=f"bathy eHydro file not set: {BATHY_EHYDRO_FILE}",
)
_skip_no_usgs = pytest.mark.skipif(
    not USGS_RATING_CURVE_CSV.is_file(), reason=f"missing {USGS_RATING_CURVE_CSV}"
)
_skip_no_manual = pytest.mark.skipif(
    MAN_CALB_FILE is None or not Path(MAN_CALB_FILE).is_file(),
    reason=f"manual calib file not set: {MAN_CALB_FILE}",
)
_skip_no_spatial = pytest.mark.skipif(
    CALIB_POINTS_FILE is None or not Path(CALIB_POINTS_FILE).is_file(),
    reason=f"spatial calib points not set: {CALIB_POINTS_FILE}",
)


# COMBINED — the whole calibration pipeline in one call, matching the step-by-step
# sequence: reset -> aggregate_pre -> thalweg -> longitudinal -> bathymetry ->
# bankfull -> subdiv -> nonmonotonic -> usgs -> spatial -> manual -> aggregate_post -> log_scan.
# File-dependent steps (bathy, usgs, spatial, manual) self-skip when their
# input file is absent, matching the step-by-step skip decorators.
@_skip_no_aoi
@_skip_no_bankfull
def test_calibrate_full_pipeline():
    """One run_calibration() call driving the full default pipeline.
    Every CalibrationConfig parameter is spelled out, grouped by step."""
    cfg = CalibrationConfig(
        # reset — revert hydroTables to uncalibrated baseline before re-applying.
        # Set True when re-calibrating an AOI that was already calibrated.
        calibration_rerun=True,

        # aggregate_pre — assemble usgs/ras2fim elev tables before adjustments
        aggregate_pre=True,

        # thalweg — remove thalweg-notch artifact rows, refill stage ladder
        thalweg_notches_adjustment=True,

        # longitudinal — smooth hydraulic geometry along reach chains
        longitudinal_filter=True,

        # bathymetry — add missing in-channel area below the DEM (needs bathy_file_ehydro)
        bathymetry_adjust=True,
        bathy_file_ehydro=BATHY_EHYDRO_FILE,

        # bankfull — identify bankfull stage in every branch SRC
        src_bankfull_toggle=True,
        bankfull_flows_file=BANKFULL_FLOWS_FILE,
        include_branch_zero=True,

        # subdiv — channel/overbank subdivision (needs vmann + bankfull on)
        src_subdiv_toggle=True,
        vmann_input_file=VMANN_INPUT_FILE,
        default_channel_n=0.06,  # used when feature_id missing from vmann table
        default_overbank_n=0.12,

        # nonmonotonic — force monotonic in-channel rating curves
        nonmonotonic_src_adjustment=True,
        nonmonotonic_stream_order_min=4,

        # usgs — calibrate SRCs against USGS rating curves at NWM recurrence flows
        src_adjust_usgs=True,
        usgs_rating_curve_csv=USGS_RATING_CURVE_CSV,
        usgs_acceptable_gages=USGS_ACCEPTABLE_GAGES,
        nwm_recur_file=NWM_RECUR_FILE,

        # spatial — calibrate SRCs against benchmark inundation points
        src_adjust_spatial=True,
        calib_points_file=CALIB_POINTS_FILE,  # None -> step self-skips

        # manual — apply a per-feature_id coefficient table
        manual_calb_toggle=True,
        man_calb_file=MAN_CALB_FILE,  # None -> step self-skips

        # aggregate_post — publish htable + bridge + road to AOI root
        aggregate_post=True,

        # log scan — collect error/warning lines into per-AOI summary files
        scan_logs=True,

        # execution
        job_branch_limit=JOB_BRANCH_LIMIT,
        skip_unimplemented=True,  # warn instead of raising on stubs
    )
    run_calibration(AOI_DIR, cfg)

    # Subdivision rewrites the per-branch hydroTable with subdiv columns.
    sample_ht = next(_BRANCHES.glob("*/hydroTable_*.csv"))
    cols = pd.read_csv(sample_ht, nrows=1).columns
    assert "subdiv_discharge_cms" in cols
    assert "channel_n" in cols


# # STEP BY STEP — each stage on its own.
# @_skip_no_aoi
# def test_step_reset():
#     """Reset per-branch hydroTable + src_full_crosswalked to baseline.
#     Needed only for reruns; a no-op on a fresh AOI. Runs before aggregation."""
#     HydroTableReset(aoi_dir=AOI_DIR).run()


# @_skip_no_aoi
# def test_step_aggregate_pre():
#     """Pre-calibration aggregation: usgs/ras2fim elev tables if available (not integrated yet) -> AOI root."""
#     BranchAggregator(aoi_dir=AOI_DIR, usgs_elev=True, ras_elev=True).run()


# @_skip_no_aoi
# def test_step_thalweg_notches():
#     """Remove thalweg-notch artifact rows and refill the stage ladder."""
#     results = ThalwegNotchesAdjustment(
#         aoi_dir=AOI_DIR,
#         n_workers=JOB_BRANCH_LIMIT,  # branch-parallel
#         stage_interval_m=0.3048,     # SRC stage step
#         n_stages=84,                 # full ladder length
#         extrap_rows=3,               # trailing rows fit for extrapolation
#     ).run()
#     assert results


# @_skip_no_aoi
# def test_step_longitudinal():
#     """Smooth hydraulic geometry along reach chains, recompute discharge."""
#     results = LongitudinalFlowFilter(
#         aoi_dir=AOI_DIR, n_workers=JOB_BRANCH_LIMIT, n_stages=84
#     ).run()
#     assert results


# @_skip_no_aoi
# @_skip_no_bathy
# def test_step_bathymetry():
#     """Add missing in-channel area below the DEM from eHydro surveys, then
#     recompute discharge."""
#     results = BathymetricAdjustment(
#         aoi_dir=AOI_DIR,
#         bathy_file_ehydro=BATHY_EHYDRO_FILE,
#     ).run()
#     assert results


# @_skip_no_aoi
# @_skip_no_bankfull
# def test_step_bankfull():
#     """Identify bankfull stage in every branch SRC."""
#     results = SrcBankfull(
#         aoi_dir=AOI_DIR,
#         bankfull_flows_file=BANKFULL_FLOWS_FILE,
#         n_workers=JOB_BRANCH_LIMIT,
#         include_branch_zero=True,
#     ).run()
#     assert results  # dict of branch_id -> status string


# @_skip_no_aoi
# @_skip_no_bankfull
# def test_step_subdiv():
#     """Channel/overbank subdivision. Depends on bankfull having run, so run
#     it first within this test to keep the step self-contained."""
#     SrcBankfull(
#         aoi_dir=AOI_DIR, bankfull_flows_file=BANKFULL_FLOWS_FILE, n_workers=1
#     ).run()
#     results = SrcSubdiv(
#         aoi_dir=AOI_DIR,
#         vmann_table=VMANN_INPUT_FILE,
#         n_workers=JOB_BRANCH_LIMIT,
#         default_channel_n=0.06,  # used when feature_id missing from vmann table
#         default_overbank_n=0.12,
#     ).run()
#     assert results


# @_skip_no_aoi
# def test_step_nonmonotonic():
#     """Force monotonic in-channel rating curves."""
#     results = SrcNonmonotonic(
#         aoi_dir=AOI_DIR, stream_order_min=4, include_branch_zero=True
#     ).run()
#     assert results


# @_skip_no_aoi
# @_skip_no_usgs
# def test_step_usgs():
#     """Calibrate SRCs against USGS rating curves at NWM recurrence flows.
#     Needs usgs_elev_table.csv at the AOI root; self-skips when inputs are absent."""
#     results = UsgsRatingCalibrator(
#         aoi_dir=AOI_DIR,
#         usgs_rating_curve_csv=USGS_RATING_CURVE_CSV,
#         usgs_acceptable_gages=USGS_ACCEPTABLE_GAGES,
#         nwm_recur_file=NWM_RECUR_FILE,
#         n_workers=JOB_BRANCH_LIMIT,
#     ).run()
#     assert results is not None


# @_skip_no_aoi
# @_skip_no_spatial
# def test_step_spatial():
#     """Calibrate SRCs against benchmark inundation points. Samples HAND/HydroID
#     rasters at each point; self-skips when the points file is absent."""
#     results = SpatialObsCalibrator(
#         aoi_dir=AOI_DIR,
#         calib_points_file=CALIB_POINTS_FILE,
#         n_workers=JOB_BRANCH_LIMIT,
#     ).run()
#     assert results is not None


# @_skip_no_aoi
# @_skip_no_manual
# def test_step_manual():
#     """Apply a per-feature_id coefficient table to each branch hydroTable.
#     Needs MAN_CALB_FILE (aoi_id, feature_id, calb_coef_manual); no-op when the
#     AOI has no matching entry."""
#     ManualCalibrator(aoi_dir=AOI_DIR, calibration_file=MAN_CALB_FILE).run()


# @_skip_no_aoi
# def test_step_aggregate_post():
#     """Post-calibration aggregation: htable + bridge + road -> AOI root."""
#     BranchAggregator(aoi_dir=AOI_DIR, htable=True, bridge=True, road=True).run()


# @_skip_no_aoi
# def test_step_log_scan():
#     """Scan logs/ for error / warning lines into per-AOI summary files."""
#     out = LogScanner(aoi_dir=AOI_DIR, calibration_rerun=False).run()
#     assert set(out) == {"errors", "warnings"}
