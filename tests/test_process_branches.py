"""
Multi-branch AOI orchestrator tests.

Layered like the rest of the test suite:
    1. test_imports                — import-only smoke (always runs)
    2. test_branch_list_parsing    — pure-python parser; no I/O
    3. test_error_classification   — exit-code mapping; no I/O
    4. test_adjust_floodplains_signature - kwarg surface stability
    5. test_process_branches_dry   - skips if the AOI out_dir is missing;
                                     verifies that running with an empty branch
                                     list does not crash and writes the
                                     processing_time txt.

For full integration testing (actual BranchZero + CreateHAND per branch), run
test_branchprocessing.py first to seed branch 0, then re-enable a full-pipeline
test here.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fimbox import (
    AOIProcessingConfig,
    BranchResult,
    CalibrationConfig,
    adjust_floodplains,
    process_branches,
)
from fimbox.preprocessing.calculate_branch.process_branches import (
    _classify_branch_error,
    _read_branch_list,
)

log = logging.getLogger(__name__)

# Reference AOI fixture for the optional dry run. Path is HUC-named because
# that's how the existing fixture lives on disk, but the fimbox API itself
# is AOI-agnostic.
OUT_DIR = Path("/Users/Supath/Downloads/SDML/FIMBOX/out/HUC08060202")
AOI_ID = "08060202"


def test_imports():
    """Surface-level smoke: every public symbol is reachable."""
    assert callable(process_branches)
    assert AOIProcessingConfig is not None
    assert BranchResult is not None
    assert CalibrationConfig is not None
    assert callable(adjust_floodplains)


def test_config_accepts_huc_id():
    """huc_id / huc_dir are equivalent aliases for aoi_id / aoi_dir."""
    import fimbox

    a = AOIProcessingConfig(
        huc_dir=Path("/tmp/a"),
        huc_id="08060202",
        branch_list_path=Path("/tmp/a/branch_list.csv"),
    )
    assert a.aoi_id == "08060202"
    assert a.huc_id == "08060202"  # alias property
    assert a.aoi_dir == Path("/tmp/a")
    assert a.huc_dir == Path("/tmp/a")  # alias property

    # HucProcessingConfig is the same class
    assert fimbox.HucProcessingConfig is AOIProcessingConfig


def test_config_accepts_aoi_id():
    """The new aoi_id / aoi_dir form works the same way."""
    a = AOIProcessingConfig(
        aoi_dir=Path("/tmp/b"),
        aoi_id="MyBasin",
        branch_list_path=Path("/tmp/b/branch_list.csv"),
    )
    assert a.aoi_id == "MyBasin"
    assert a.huc_id == "MyBasin"


def test_config_rejects_conflicting_aliases():
    """Passing both aoi_dir= and huc_dir= with different values is an error."""
    with pytest.raises(TypeError, match="not both"):
        AOIProcessingConfig(
            aoi_dir=Path("/tmp/a"),
            huc_dir=Path("/tmp/b"),
            aoi_id="X",
            branch_list_path=Path("/tmp/x.csv"),
        )

    # Passing both with the SAME value is fine (idempotent)
    cfg = AOIProcessingConfig(
        aoi_dir=Path("/tmp/a"),
        huc_dir=Path("/tmp/a"),
        aoi_id="X",
        huc_id="X",
        branch_list_path=Path("/tmp/x.csv"),
    )
    assert cfg.aoi_id == "X"


def test_branch_list_parsing(tmp_path):
    """branch_list.csv supports 1-col and 2-col rows; branch zero is excluded."""
    bl = tmp_path / "branch_list.csv"
    bl.write_text("08060202,3246000305\n08060202,3246000257\n08060202,0\n")
    out = _read_branch_list(bl, branch_zero_id="0")
    assert out == ["3246000305", "3246000257"]

    # one-column form
    bl.write_text("3246000305\n0\n3246000257\n")
    out = _read_branch_list(bl, branch_zero_id="0")
    assert out == ["3246000305", "3246000257"]

    # missing file
    assert _read_branch_list(tmp_path / "nope.csv", branch_zero_id="0") == []


def test_error_classification():
    """process_branch.sh's 61/64/65 codes are mapped from exception text."""
    assert _classify_branch_error("NoFlowlinesError: nothing here") == "no_flowlines"
    assert _classify_branch_error("NoCrosswalkError: no matches") == "no_crosswalk"
    assert (
        _classify_branch_error("Too many HydroIDs in gw catchments")
        == "too_many_hydroids"
    )
    assert _classify_branch_error("some other random error") == "failed"


def test_adjust_floodplains_signature():
    """adjust_floodplains is importable with the documented argument names.
    We don't run it (needs WhiteboxTools + real rasters) but we validate the
    keyword-argument surface so refactors break loudly."""
    import inspect

    sig = inspect.signature(adjust_floodplains)
    expected = {
        "input_file", "dem_file", "nwm_catchments", "nwm_streams",
        "nwm_levelpaths", "distance_file", "output_file",
        "distance_threshold", "slope_exponent", "z_factor",
        "branch_polygons", "branch_id", "fema_flood_zones_file",
        "fema_flood_zones_layer", "wbt_path",
    }
    assert expected.issubset(sig.parameters.keys()), (
        f"adjust_floodplains is missing kwargs: {expected - set(sig.parameters)}"
    )


@pytest.mark.skipif(not OUT_DIR.is_dir(), reason="AOI fixture not present")
def test_process_branches_dry(tmp_path):
    """Process branches with an empty branch list: confirms wiring + logging
    without invoking BranchZero/CreateHAND. Calibration runs with all toggles
    OFF and skip_unimplemented=True so no stub raises."""
    branch_list = tmp_path / "branch_list.csv"
    branch_list.write_text("")  # zero branches

    cfg = AOIProcessingConfig(
        aoi_dir=OUT_DIR,
        aoi_id=AOI_ID,
        branch_list_path=branch_list,
        n_workers=1,
    )
    calib = CalibrationConfig(skip_unimplemented=True)
    results = process_branches(cfg, calibration=calib)
    assert results == []

    # processing_time_<aoi>.txt should now contain at least one summary line
    ptime = OUT_DIR / f"processing_time_{AOI_ID}.txt"
    assert ptime.exists()
    last = ptime.read_text().strip().splitlines()[-1]
    assert last.startswith(AOI_ID + ",")
    log.info(f"processing_time entry: {last}")
