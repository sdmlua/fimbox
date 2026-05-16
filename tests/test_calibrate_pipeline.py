"""
Calibration subpackage tests.

Layered:
    1. test_imports                       - public surface reachable
    2. test_stubs_raise                   - every not-yet-ported step raises
                                            CalibrationNotImplemented
    3. test_pipeline_default_safe         - default CalibrationConfig runs
                                            aggregate_branches only (no
                                            stubs triggered) on a fresh tmp AOI
    4. test_pipeline_unported_skips       - toggling a stubbed step ON with
                                            skip_unimplemented=True warns
                                            instead of raising
    5. test_pipeline_unported_raises      - same but skip_unimplemented=False
                                            propagates the error
    6. test_aggregate_branches_empty      - aggregate_branches on an empty
                                            branches dir is a no-op
    7. test_reset_hydro_and_src_no_files  - reset on a dir with no branch
                                            files exits quietly
    8. test_manual_calibration_validation - rejects negative coefficients,
                                            missing files

Heavy end-to-end calibration (USGS / RAS2FIM / spatial obs) is not exercised
here because those routines are stubs until validated against a real HUC.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from fimbox import (
    CalibrationConfig,
    CalibrationNotImplemented,
    aggregate_branches,
    manual_calibration,
    reset_hydro_and_src,
    run_calibration,
)
from fimbox.preprocessing.calibrate import (
    bathymetric_adjustment,
    identify_src_bankfull,
    longitudinal_flow_adjustment,
    nonmonotonic_src_adjustment,
    src_adjust_ras2fim_rating,
    src_adjust_spatial_obs,
    src_adjust_usgs_rating_trace,
    subdiv_chan_obank_src,
    thalweg_notches_adjustment,
)

log = logging.getLogger(__name__)


def test_imports():
    assert callable(run_calibration)
    assert CalibrationConfig is not None
    assert CalibrationNotImplemented is not None
    assert callable(aggregate_branches)
    assert callable(reset_hydro_and_src)
    assert callable(manual_calibration)


@pytest.mark.parametrize(
    "fn,args",
    [
        (thalweg_notches_adjustment, ("/tmp/x",)),
        (longitudinal_flow_adjustment, ("/tmp/x",)),
        (bathymetric_adjustment, ("/tmp/x", "/tmp/a", "/tmp/b")),
        (identify_src_bankfull, ("/tmp/x", "/tmp/f")),
        (subdiv_chan_obank_src, ("/tmp/x", "/tmp/v")),
        (nonmonotonic_src_adjustment, ("/tmp/x",)),
        (src_adjust_ras2fim_rating, ("/tmp/x", "/tmp/r", "/tmp/n")),
        (src_adjust_spatial_obs, ("/tmp/x",)),
        (src_adjust_usgs_rating_trace, ("/tmp/x", "/tmp/r", "/tmp/g", "/tmp/n")),
    ],
)
def test_stubs_raise(fn, args):
    """Every not-yet-ported step should raise CalibrationNotImplemented when
    invoked, with a clear pointer to the inundation-mapping source."""
    with pytest.raises(CalibrationNotImplemented) as exc:
        fn(*args)
    msg = str(exc.value)
    assert "inundation-mapping/src/" in msg
    assert fn.__name__ in msg or fn.__name__.replace("_", "") in msg.replace("_", "")


def _make_empty_aoi(tmp_path: Path) -> Path:
    aoi = tmp_path / "08060202"
    (aoi / "branches").mkdir(parents=True)
    return aoi


def test_pipeline_default_safe(tmp_path):
    """Default CalibrationConfig has every toggle OFF. The pipeline only runs
    aggregate_branches (which no-ops on empty branches), so no stub is hit."""
    aoi = _make_empty_aoi(tmp_path)
    cfg = CalibrationConfig()  # all toggles off
    run_calibration(aoi, cfg)  # must not raise


def test_pipeline_unported_skips(tmp_path):
    """When skip_unimplemented=True the pipeline warns rather than raising
    on stubbed steps."""
    aoi = _make_empty_aoi(tmp_path)
    cfg = CalibrationConfig(
        thalweg_notches_adjustment=True,
        nonmonotonic_src_adjustment=True,
        skip_unimplemented=True,
    )
    run_calibration(aoi, cfg)  # warns and continues


def test_pipeline_unported_raises(tmp_path):
    """skip_unimplemented=False propagates CalibrationNotImplemented."""
    aoi = _make_empty_aoi(tmp_path)
    cfg = CalibrationConfig(
        thalweg_notches_adjustment=True,
        skip_unimplemented=False,
    )
    with pytest.raises(CalibrationNotImplemented):
        run_calibration(aoi, cfg)


def test_calibrate_accepts_huc_dir_alias(tmp_path):
    """run_calibration / aggregate_branches / reset_hydro_and_src /
    manual_calibration all accept either aoi_dir= or huc_dir= as the input."""
    aoi = _make_empty_aoi(tmp_path)

    # run_calibration with huc_dir=
    run_calibration(huc_dir=aoi, cfg=CalibrationConfig())

    # aggregate_branches with huc_dir=
    aggregate_branches(huc_dir=aoi, usgs_elev=True)

    # reset_hydro_and_src with huc_dir=
    reset_hydro_and_src(huc_dir=aoi)

    # manual_calibration with huc_dir=
    csv = tmp_path / "mc.csv"
    pd.DataFrame(
        {"aoi_id": ["08060202"], "feature_id": [1], "calb_coef_manual": [1.0]}
    ).to_csv(csv, index=False)
    manual_calibration(huc_dir=aoi, calibration_file=csv)


def test_calibrate_rejects_conflicting_aliases(tmp_path):
    """Passing aoi_dir= AND huc_dir= with different paths is rejected."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    with pytest.raises(TypeError, match="not both"):
        aggregate_branches(aoi_dir=a, huc_dir=b, usgs_elev=True)


def test_aggregate_branches_empty(tmp_path):
    """aggregate_branches on an empty branches/ dir is a no-op (no exceptions,
    no output files)."""
    aoi = _make_empty_aoi(tmp_path)
    aggregate_branches(aoi, usgs_elev=True, ras_elev=True, htable=True, src_cross=True)
    # nothing to write
    assert not (aoi / "hydrotable.csv").exists()
    assert not (aoi / "usgs_elev_table.csv").exists()


def test_reset_hydro_and_src_no_files(tmp_path):
    """reset_hydro_and_src on a dir with branches but no SRC files exits
    quietly (matches inundation-mapping behavior for failed branches)."""
    aoi = _make_empty_aoi(tmp_path)
    (aoi / "branches" / "3246000305").mkdir()
    reset_hydro_and_src(aoi)  # must not raise


def test_manual_calibration_validation(tmp_path):
    """manual_calibration rejects bad inputs and accepts either column name."""
    aoi = _make_empty_aoi(tmp_path)

    # missing file
    with pytest.raises(FileNotFoundError):
        manual_calibration(aoi, tmp_path / "missing.csv")

    # negative coefficient (legacy HUC8 column name)
    bad = tmp_path / "bad.csv"
    pd.DataFrame(
        {"HUC8": ["08060202"], "feature_id": [1], "calb_coef_manual": [-1.0]}
    ).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="must be > 0"):
        manual_calibration(aoi, bad)

    # missing both aoi_id and HUC8 columns -> ValueError
    no_aoi_col = tmp_path / "no_aoi.csv"
    pd.DataFrame(
        {"other_col": ["08060202"], "feature_id": [1], "calb_coef_manual": [1.0]}
    ).to_csv(no_aoi_col, index=False)
    with pytest.raises(ValueError, match="aoi_id"):
        manual_calibration(aoi, no_aoi_col)

    # no entry for this AOI (legacy HUC8): should log and skip without error
    other = tmp_path / "other.csv"
    pd.DataFrame(
        {"HUC8": ["09999999"], "feature_id": [1], "calb_coef_manual": [1.0]}
    ).to_csv(other, index=False)
    manual_calibration(aoi, other)

    # no entry for this AOI (new aoi_id column): same skip behavior
    other_new = tmp_path / "other_new.csv"
    pd.DataFrame(
        {"aoi_id": ["09999999"], "feature_id": [1], "calb_coef_manual": [1.0]}
    ).to_csv(other_new, index=False)
    manual_calibration(aoi, other_new)
