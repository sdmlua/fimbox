"""
Author: Supath Dhital
Date Updated: August 2026

Tests for the synthetic rating curve (SRC) calibration pipeline.

Two layers:

  COMBINED ......... test_calibrate_full_pipeline runs the whole thing in a
      single run_calibration() call against the live AOI, with EVERY optional
      CalibrationConfig parameter spelled out so the full surface is visible
      in one place.

  STEP BY STEP ..... one test per stage (reset, aggregate, slope, thalweg,
      longitudinal, bathymetry, bankfull, subdiv, nonmonotonic, usgs, ras2fim,
      spatial, manual, log scan), each with that class's full parameter list
      spelled out. Marked ``step`` and deselected from a normal run; select
      them with ``pytest -m step`` (or one at a time by node id) to debug a
      single stage.

Order matters. run_calibration() executes the stages in the fixed sequence
written into Calibrator.run() — the order of the fields in CalibrationConfig,
and the order they are passed here, have no effect. Turning everything on
therefore gives:

    geometry/roughness   slope -> thalweg -> longitudinal -> bathymetry ->
                         bankfull -> subdiv -> nonmonotonic
                         each rewrites discharge_cms from the SRC geometry,
                         and the result is what the calibrators snapshot as
                         precalb_discharge_cms.

    observations         usgs -> ras2fim -> spatial
                         each recomputes discharge from precalb (they do not
                         compound), and each later source keeps the earlier
                         coefficient only where it has none of its own —
                         so per HydroID the winner is spatial > ras2fim > usgs.

    manual               applied last, on top of whatever the above produced:
                         discharge = pre_manual_calb_discharge_cms / coef.

It will point into the working version of the AOI and skip when it is absent.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fimbox import CalibrationConfig, datasets, run_calibration
from fimbox._dask import _resolve_n_workers

# One class per calibration stage, for the step-by-step tests at the bottom of
# this file. Kept imported while those are commented out, so uncommenting any of
# them is a one-line edit — hence the noqa.
from fimbox.preprocessing.calibrate_ratingcurve import (  # noqa: F401
    BathymetricAdjustment,
    BranchAggregator,
    HydroTableReset,
    LogScanner,
    LongitudinalFlowFilter,
    ManualCalibrator,
    Ras2fimCalibrator,
    SlopeAdjustment,
    SpatialObsCalibrator,
    SrcBankfull,
    SrcNonmonotonic,
    SrcSubdiv,
    ThalwegNotchesAdjustment,
    UsgsRatingCalibrator,
)

# Live AOI. Edit this to point at your data; tests skip when the AOI is absent.
AOI_DIR = Path(__file__).resolve().parents[2] / "out" / "test_smallB"
# AOI_DIR = Path(__file__).resolve().parents[2] / "out" / "nwm_11239459and2more"

# No input files are listed here on purpose. Every calibration step resolves its
# own input through ``fimbox.datasets`` -- fetched from the public SDML bucket on
# first use and cached -- so the tests below exercise the same default path a
# user gets. ``dataset()`` is only for the tests that pass a path explicitly to
# prove the override seam works.


def dataset(key: str):
    # Lazily, so collecting this file never triggers a download.
    return datasets.fetch(key)


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


# COMBINED — the whole calibration pipeline in one call, matching the step-by-step
# sequence: reset -> aggregate_pre -> slope -> thalweg -> longitudinal -> bathymetry ->
# bankfull -> subdiv -> nonmonotonic -> usgs -> ras2fim -> spatial -> manual ->
# aggregate_post -> log_scan.
# File-dependent steps (bathy, usgs, ras2fim, spatial, manual) self-skip when
# their input file is absent, matching the step-by-step skip decorators.
# @_skip_no_aoi
# def test_calibrate_defaults():
#     """The zero-config path: every step on, every input fetched for this AOI."""
#     run_calibration(AOI_DIR)

#     sample_ht = next(_BRANCHES.glob("*/hydroTable_*.csv"))
#     cols = pd.read_csv(sample_ht, nrows=1).columns
#     assert "subdiv_discharge_cms" in cols
#     assert "channel_n" in cols


@_skip_no_aoi
def test_calibrate_full_pipeline():
    """The same pipeline with every CalibrationConfig parameter spelled out.

    Inputs are passed explicitly here to exercise the override seam; leaving any
    of them at None resolves the published dataset instead (see the test above)."""
    cfg = CalibrationConfig(
        # reset — revert hydroTables to uncalibrated baseline before re-applying.
        # Set True when re-calibrating an AOI that was already calibrated.
        calibration_rerun=True,
        # aggregate_pre — assemble usgs/ras2fim elev tables before adjustments
        aggregate_pre=True,
        # slope — re-derive discharge on a different slope than the DEM's rise/run.
        # "hfab" reads the SLOPE_HFAB column already in the SRC, so slope_table is
        # ignored; pass slope_source="table" to use the table instead.
        slope_adjustment=True,
        slope_source="hfab",
        slope_table=dataset("reach_slope"),
        # thalweg — remove thalweg-notch artifact rows, refill stage ladder
        thalweg_notches_adjustment=True,
        # longitudinal — smooth hydraulic geometry along reach chains
        longitudinal_filter=True,
        # bathymetry — add missing in-channel area below the DEM
        bathymetry_adjust=True,
        bathy_file_ehydro=dataset("channel_bathymetry"),
        bathy_file_aibased=dataset("channel_geometry_predicted"),
        ai_toggle=1,
        ai_strm_order=4,
        # bankfull — identify bankfull stage in every branch SRC
        src_bankfull_toggle=True,
        bankfull_flows_file=dataset("bankfull_flows"),
        include_branch_zero=True,
        # subdiv — channel/overbank subdivision (needs bankfull on)
        src_subdiv_toggle=True,
        vmann_input_file=dataset("channel_roughness"),
        default_channel_n=0.06,  # used when feature_id missing from vmann table
        default_overbank_n=0.12,
        # nonmonotonic — force monotonic in-channel rating curves
        nonmonotonic_src_adjustment=True,
        nonmonotonic_stream_order_min=4,
        # usgs — calibrate SRCs against gage rating curves at recurrence flows
        src_adjust_usgs=True,
        usgs_rating_curve_csv=dataset("gage_rating_curves"),
        usgs_acceptable_gages=dataset("gage_quality_filter"),
        nwm_recur_file=dataset("recurrence_flows"),
        # ras2fim — same, from hydraulic-model cross-section rating curves.
        # Needs ras_elev_table.csv at the AOI root, which the gage crosswalk only
        # writes where cross-sections land in the AOI — usually nowhere, so this
        # step logs a skip on most areas.
        src_adjust_ras2fim=True,
        ras_rating_curve_csv=dataset("xsec_rating_curves"),
        # spatial — calibrate SRCs against flood-edge observation points.
        # Left at None so the published national point set is fetched and clipped
        # to this AOI.
        src_adjust_spatial=True,
        calib_points_file=None,
        # manual — apply a per-feature_id coefficient table
        manual_calb_toggle=True,
        man_calb_file=dataset("manual_coefficients"),
        # aggregate_post — publish htable + bridge + road to AOI root
        aggregate_post=True,
        # log scan — collect error/warning lines into per-AOI summary files
        scan_logs=True,
        # execution
        job_branch_limit=JOB_BRANCH_LIMIT,
        offline=False,  # True = never hit S3; run only on paths given above
        skip_unimplemented=True,  # warn instead of raising on stubs
    )
    run_calibration(AOI_DIR, cfg)

    # Subdivision rewrites the per-branch hydroTable with subdiv columns.
    sample_ht = next(_BRANCHES.glob("*/hydroTable_*.csv"))
    cols = pd.read_csv(sample_ht, nrows=1).columns
    assert "subdiv_discharge_cms" in cols
    assert "channel_n" in cols


# @_skip_no_aoi
# def test_calibrate_step_unplugged():
#     """Passing False for a step removes it without disturbing the rest."""
#     run_calibration(
#         AOI_DIR,
#         CalibrationConfig(
#             calibration_rerun=False,
#             src_adjust_usgs=True,
#             src_adjust_ras2fim=True,
#             src_adjust_spatial=True,
#             manual_calb_toggle=True,
#             job_branch_limit=JOB_BRANCH_LIMIT,
#         ),
#     )


def test_dataset_registry_resolves():
    """Every dataset key maps to a published object. No downloads — registry only."""
    assert datasets.available()
    for key in datasets.available():
        assert f"national/{key}.parquet" in datasets.REGISTRY

    # An unknown key warns and returns None rather than raising.
    assert datasets.fetch("not_a_dataset") is None


# STEP BY STEP — each stage on its own, every parameter spelled out. Deselected
# from a normal run (see the `step` marker in pyproject.toml); run them with
# `pytest -m step`, or one at a time to debug a single stage. Order below is the
# order Calibrator.run() uses; running one out of order is fine, but the
# observation-driven calibrators read the channel_n / overbank_n columns only
# subdivision writes, so run bankfull + subdiv first for a meaningful result.
# _step = pytest.mark.step


# @_step
# @_skip_no_aoi
# def test_step_reset():
#     """Reset per-branch hydroTable + src_full_crosswalked to baseline.
#     Needed only for reruns; a no-op on a fresh AOI. Runs before aggregation."""
#     HydroTableReset(aoi_dir=AOI_DIR).run()


# @_step
# @_skip_no_aoi
# def test_step_aggregate_pre():
#     """Pre-calibration aggregation: the usgs / ras2fim elev tables the gage and
#     cross-section calibrators crosswalk their observations through."""
#     BranchAggregator(
#         aoi_dir=AOI_DIR,
#         usgs_elev=True,  # usgs_elev_table.csv
#         ras_elev=True,  # ras_elev_table.csv
#         htable=False,  # post-calibration outputs — see test_step_aggregate_post
#         src_cross=False,
#         bridge=False,
#         road=False,
#         limit_branches=None,  # e.g. ["0", "1234567"] to roll up a subset
#         default_crs=5070,  # fallback CRS for layers that carry none
#     ).run()


# @_step
# @_skip_no_aoi
# def test_step_slope():
#     """Re-derive discharge on a slope other than the DEM's rise/run.
#     slope_source="hfab" reads the SLOPE_HFAB column already in the SRC and
#     ignores slope_table; "table" reads the reach-slope dataset. Reaches the
#     chosen source does not cover keep their DEM slope."""
#     results = SlopeAdjustment(
#         aoi_dir=AOI_DIR,
#         slope_source="table",
#         slope_table=dataset("reach_slope"),
#         n_workers=JOB_BRANCH_LIMIT,
#         include_branch_zero=True,
#     ).run()
#     assert results


# @_step
# @_skip_no_aoi
# def test_step_thalweg_notches():
#     """Remove thalweg-notch artifact rows and refill the stage ladder."""
#     results = ThalwegNotchesAdjustment(
#         aoi_dir=AOI_DIR,
#         n_workers=JOB_BRANCH_LIMIT,  # branch-parallel
#         stage_interval_m=0.3048,  # SRC stage step
#         n_stages=84,  # full ladder length
#         extrap_rows=3,  # trailing rows fit for extrapolation
#     ).run()
#     assert results


# @_step
# @_skip_no_aoi
# def test_step_longitudinal():
#     """Smooth hydraulic geometry along reach chains, recompute discharge."""
#     results = LongitudinalFlowFilter(
#         aoi_dir=AOI_DIR,
#         n_workers=JOB_BRANCH_LIMIT,
#         n_stages=84,
#     ).run()
#     assert results


# @_step
# @_skip_no_aoi
# def test_step_bathymetry():
#     """Add missing in-channel area below the DEM, then recompute discharge.
#     Surveyed (eHydro) channels are the base source; predicted geometry is layered
#     on only when ai_toggle=1, and only at or above ai_strm_order."""
#     results = BathymetricAdjustment(
#         aoi_dir=AOI_DIR,
#         bathy_file_ehydro=dataset("channel_bathymetry"),
#         bathy_file_aibased=dataset("channel_geometry_predicted"),
#         ai_toggle=1,  # 0 = surveyed only
#         ai_strm_order=4,
#         n_workers=JOB_BRANCH_LIMIT,
#     ).run()
#     assert results


# @_step
# @_skip_no_aoi
# def test_step_bankfull():
#     """Identify bankfull stage in every branch SRC. Writes the Stage_bankfull
#     column subdivision depends on."""
#     results = SrcBankfull(
#         aoi_dir=AOI_DIR,
#         bankfull_flows_file=dataset("bankfull_flows"),
#         n_workers=JOB_BRANCH_LIMIT,
#         include_branch_zero=True,
#     ).run()
#     assert results  # dict of branch_id -> status string


# @_step
# @_skip_no_aoi
# def test_step_subdiv():
#     """Channel/overbank subdivision. Depends on bankfull having run, so run
#     it first within this test to keep the step self-contained."""
#     SrcBankfull(
#         aoi_dir=AOI_DIR,
#         bankfull_flows_file=dataset("bankfull_flows"),
#         n_workers=JOB_BRANCH_LIMIT,
#     ).run()
#     results = SrcSubdiv(
#         aoi_dir=AOI_DIR,
#         vmann_table=dataset("channel_roughness"),
#         n_workers=JOB_BRANCH_LIMIT,
#         include_branch_zero=True,
#         default_channel_n=0.06,  # used when feature_id missing from vmann table
#         default_overbank_n=0.12,
#     ).run()
#     assert results


# @_step
# @_skip_no_aoi
# def test_step_nonmonotonic():
#     """Force monotonic in-channel rating curves, on reaches at or above
#     stream_order_min."""
#     results = SrcNonmonotonic(
#         aoi_dir=AOI_DIR,
#         stream_order_min=4,
#         include_branch_zero=True,
#         n_workers=JOB_BRANCH_LIMIT,
#     ).run()
#     assert results


# @_step
# @_skip_no_aoi
# def test_step_usgs():
#     """Calibrate SRCs against USGS rating curves at NWM recurrence flows.
#     Needs usgs_elev_table.csv at the AOI root (test_step_aggregate_pre writes
#     it); self-skips when inputs are absent."""
#     results = UsgsRatingCalibrator(
#         aoi_dir=AOI_DIR,
#         usgs_rating_curve_csv=dataset("gage_rating_curves"),
#         nwm_recur_file=dataset("recurrence_flows"),
#         usgs_acceptable_gages=dataset("gage_quality_filter"),  # optional filter
#         n_workers=JOB_BRANCH_LIMIT,
#         debug_outputs=False,  # True writes per-branch calc/merge CSVs
#     ).run()
#     assert results is not None


# @_step
# @_skip_no_aoi
# def test_step_ras2fim():
#     """Calibrate SRCs against hydraulic-model cross-section rating curves.
#     Merges over the USGS pass rather than clobbering it — a HydroID the
#     cross-sections do not cover keeps its gage coefficient. Needs
#     ras_elev_table.csv at the AOI root, which most AOIs do not have."""
#     results = Ras2fimCalibrator(
#         aoi_dir=AOI_DIR,
#         ras_rating_curve_csv=dataset("xsec_rating_curves"),
#         nwm_recur_file=dataset("recurrence_flows"),
#         n_workers=JOB_BRANCH_LIMIT,
#         debug_outputs=False,
#     ).run()
#     assert results is not None


# @_step
# @_skip_no_aoi
# def test_step_spatial():
#     """Calibrate SRCs against flood-edge observation points. Samples HAND/HydroID
#     rasters at each point and merges over the earlier rating-curve passes;
#     self-skips when the points file is absent."""
#     results = SpatialObsCalibrator(
#         aoi_dir=AOI_DIR,
#         calib_points_file=dataset("flood_edge_points"),
#         n_workers=JOB_BRANCH_LIMIT,
#         down_dist_thresh=8.0,  # km a group coefficient is carried downstream
#         debug_outputs=False,
#     ).run()
#     assert results is not None


# @_step
# @_skip_no_aoi
# def test_step_manual():
#     """Apply a per-feature_id coefficient table to each branch hydroTable, on top
#     of whatever the observation calibrators produced. The table needs aoi_id (or
#     legacy HUC8), feature_id, calb_coef_manual; a no-op when the AOI has no
#     matching entry."""
#     ManualCalibrator(
#         aoi_dir=AOI_DIR,
#         calibration_file=dataset("manual_coefficients"),
#     ).run()


# @_step
# @_skip_no_aoi
# def test_step_aggregate_post():
#     """Post-calibration aggregation: the AOI-level hydroTable plus the bridge and
#     road layers downstream FIM generation reads."""
#     BranchAggregator(
#         aoi_dir=AOI_DIR,
#         htable=True,
#         bridge=True,
#         road=True,
#         src_cross=False,  # also roll up src_full_crosswalked
#         usgs_elev=False,
#         ras_elev=False,
#         limit_branches=None,
#         default_crs=5070,
#     ).run()


# @_step
# @_skip_no_aoi
# def test_step_log_scan():
#     """Scan logs/ for error / warning lines into per-AOI summary files."""
#     out = LogScanner(
#         aoi_dir=AOI_DIR,
#         calibration_rerun=False,  # True scans only logs/src_calibrations
#         logs_subdir="logs",
#         rerun_subdir="src_calibrations",
#         error_term="error",
#         warning_term="warning",
#     ).run()
#     assert set(out) == {"errors", "warnings"}
