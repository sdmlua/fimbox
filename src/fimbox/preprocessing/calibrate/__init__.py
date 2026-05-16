"""
Synthetic Rating Curve (SRC) calibration subpackage.

fimbox port of inundation-mapping's ``calibrate_rating_curves.sh`` chain.

Public surface
--------------
CalibrationConfig, run_calibration   - the pipeline orchestrator
aggregate_branches                    - merge per-branch outputs to AOI-level
reset_hydro_and_src                   - reset hydroTable + SRC to baseline
manual_calibration                    - apply user-supplied ManningN coefs
CalibrationNotImplemented            - raised by not-yet-ported steps

Individual step modules (some are stubs with NotImplementedError until
validated against a real AOI end-to-end):
    thalweg_notches_adjustment
    longitudinal_flow_adjustment
    bathymetric_adjustment
    identify_src_bankfull
    subdiv_chan_obank_src
    nonmonotonic_src_adjustment
    src_adjust_usgs_rating_trace
    src_adjust_ras2fim_rating
    src_adjust_spatial_obs
"""

from __future__ import annotations

from .pipeline import CalibrationConfig, run_calibration
from .aggregate_branches_to_aoi import aggregate_branches
from .reset_htable_src import reset_hydro_and_src, reset_branch
from .src_manual_calibration import manual_calibration
from ._stub import CalibrationNotImplemented

# Individual step entry points (importable for direct invocation / testing)
from .thalweg_notches_adjustment import thalweg_notches_adjustment
from .longitudinal_flow_adjustment import longitudinal_flow_adjustment
from .bathymetric_adjustment import bathymetric_adjustment
from .identify_src_bankfull import identify_src_bankfull
from .subdiv_chan_obank_src import subdiv_chan_obank_src
from .nonmonotonic_src_adjustment import nonmonotonic_src_adjustment
from .src_adjust_usgs_rating_trace import src_adjust_usgs_rating_trace
from .src_adjust_ras2fim_rating import src_adjust_ras2fim_rating
from .src_adjust_spatial_obs import src_adjust_spatial_obs

__all__ = [
    # pipeline
    "CalibrationConfig",
    "run_calibration",
    "CalibrationNotImplemented",
    # aggregator + utilities
    "aggregate_branches",
    "reset_hydro_and_src",
    "reset_branch",
    "manual_calibration",
    # individual steps
    "thalweg_notches_adjustment",
    "longitudinal_flow_adjustment",
    "bathymetric_adjustment",
    "identify_src_bankfull",
    "subdiv_chan_obank_src",
    "nonmonotonic_src_adjustment",
    "src_adjust_usgs_rating_trace",
    "src_adjust_ras2fim_rating",
    "src_adjust_spatial_obs",
]
