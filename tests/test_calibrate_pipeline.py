"""
Author: Supath Dhital
Date Updated: June 2026

Tests for the synthetic rating curve (SRC) calibration pipeline.

Two layers:

  COMBINED ......... test_calibrate_full_pipeline runs the whole thing in a
      single run_calibration() call against the live AOI, with EVERY optional
      CalibrationConfig parameter spelled out so the full surface is visible
      in one place.

  STEP BY STEP ..... one test per stage (aggregate, bankfull, subdiv,
      nonmonotonic, log scan) so any single step can be run / debugged alone.

It will point into the working version of the AOI and skip when it is absent.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fimbox import CalibrationConfig, run_calibration
from fimbox._dask import _resolve_n_workers
from fimbox.preprocessing.calibrate_ratingcurve import (
    BranchAggregator,
    HydroTableReset,
    LongitudinalFlowFilter,
    LogScanner,
    SrcBankfull,
    SrcNonmonotonic,
    SrcSubdiv,
    ThalwegNotchesAdjustment,
)

# Live AOI + input files. Edit these to point at your data; tests skip when the AOI is absent.
AOI_DIR = Path(".././out/test_smallB")
DATA = Path("/Users/Supath/Downloads/SDML/FIMBOX/FIM_HF/Data/rating_curve")

BANKFULL_FLOWS_FILE = DATA / "bankfull_flows" / "nwm_high_water_threshold_cms.csv"
VMANN_INPUT_FILE = DATA / "variable_roughness" / "mannings_global_06_014.csv"

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
_skip_no_vmann = pytest.mark.skipif(
    not VMANN_INPUT_FILE.is_file(), reason=f"missing {VMANN_INPUT_FILE}"
)


# # COMBINED — the whole pipeline in one call, Tune the parameters as needed.
# @_skip_no_aoi
# @_skip_no_bankfull
# @_skip_no_vmann
# def test_calibrate_full_pipeline():
#     """One run_calibration() call driving the full default pipeline plus the
#     two big-lift SRC routines. Every CalibrationConfig field is listed (the
#     commented ones are the not-yet-ported / file-dependent steps) so this
#     test doubles as the parameter reference."""
#     cfg = CalibrationConfig(
#         # --- mode ---
#         # True runs the reset step first (reverts hydroTables to the
#         # uncalibrated baseline) before re-applying calibration. Set True
#         # when re-calibrating an AOI that was already calibrated.
#         calibration_rerun=True,

#         thalweg_notches_adjustment=True,   # no extra input
#         longitudinal_filter=True,          # no extra input
#         src_bankfull_toggle=True,          # needs bankfull_flows_file
#         src_subdiv_toggle=True,            # needs vmann_input_file & bankfull on
#         nonmonotonic_src_adjustment=True,  # no extra input
#         # bathymetry_adjust=True,          # needs bathy_file_* below
#         # src_adjust_usgs=True,            # stub; needs usgs_* + nwm_recur
#         # src_adjust_ras2fim=True,         # stub; needs ras_rating_curve_csv
#         # src_adjust_spatial=True,         # stub
#         # manual_calb_toggle=True,         # needs man_calb_file

#         # --- input files -----
#         bankfull_flows_file=BANKFULL_FLOWS_FILE,
#         vmann_input_file=VMANN_INPUT_FILE,
#         # bathy_file_ehydro=...,           # for bathymetry_adjust (eHydro .gpkg)
#         # bathy_file_aibased=...,          # AI bathymetry .parquet
#         # ai_toggle=0,                     # 1 = also apply AI bathymetry
#         # ai_strm_order=4,                 # min stream order for AI bathymetry
#         # usgs_rating_curve_csv=...,       # for src_adjust_usgs
#         # usgs_acceptable_gages=...,
#         # nwm_recur_file=...,              # for usgs + ras2fim
#         # ras_rating_curve_csv=...,        # for src_adjust_ras2fim
#         # man_calb_file=...,               # for manual_calb_toggle

#         # --- tunables ----
#         default_channel_n=0.06,
#         default_overbank_n=0.12,
#         nonmonotonic_stream_order_min=1,
#         include_branch_zero=True,
#         # --- aggregation control -----
#         aggregate_pre=True,                # assemble elev tables before adjust
#         aggregate_post=True,               # publish htable/bridge/road after

#         # --- log scan ------
#         scan_logs=True,

#         # --- execution -----
#         job_branch_limit=JOB_BRANCH_LIMIT,
#         skip_unimplemented=True,           # warn instead of raising on stubs
#     )
#     run_calibration(AOI_DIR, cfg)

#     # Subdivision rewrites the per-branch hydroTable with subdiv columns.
#     sample_ht = next(_BRANCHES.glob("*/hydroTable_*.csv"))
#     cols = pd.read_csv(sample_ht, nrows=1).columns
#     assert "subdiv_discharge_cms" in cols
#     assert "channel_n" in cols


# STEP BY STEP — each stage on its own.
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

@_skip_no_aoi
def test_step_longitudinal():
    """Smooth hydraulic geometry along reach chains, recompute discharge."""
    results = LongitudinalFlowFilter(
        aoi_dir=AOI_DIR, n_workers=JOB_BRANCH_LIMIT, n_stages=84
    ).run()
    assert results


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
# @_skip_no_vmann
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
#         default_channel_n=0.06,   # used when feature_id missing from vmann table
#         default_overbank_n=0.12,
#     ).run()
#     assert results


# @_skip_no_aoi
# def test_step_nonmonotonic():
#     """Force monotonic in-channel rating curves."""
#     results = SrcNonmonotonic(
#         aoi_dir=AOI_DIR, stream_order_min=1, include_branch_zero=True
#     ).run()
#     assert results


# @_skip_no_aoi
# def test_step_aggregate_post():
#     """Post-calibration aggregation: htable + bridge + road -> AOI root."""
#     BranchAggregator(aoi_dir=AOI_DIR, htable=True, bridge=True, road=True).run()


# @_skip_no_aoi
# def test_step_log_scan():
#     """Scan logs/ for error / warning lines into per-AOI summary files."""
#     out = LogScanner(aoi_dir=AOI_DIR, calibration_rerun=False).run()
#     assert set(out) == {"errors", "warnings"}
