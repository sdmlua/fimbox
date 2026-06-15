"""
Author: Supath Dhital
Date Updated: June 2026

FIM generation driven by the CSVs in <AOI_DIR>/discharge-inputs/.

generateFIM(aoi_dir).from_discharge_inputs(...) selects which discharge CSVs to
run and generates an inundation extent raster for each (named after the input
CSV) in <AOI_DIR>/fim-outputs/. Pass depth=True to also write a depth raster.

Selection modes:
  * nothing            -> every CSV in discharge-inputs/
  * csv=<path>         -> that one CSV
  * date="YYYY-MM-DD"  -> CSVs whose filename carries that day/instant stamp
  * start=.., end=..   -> CSVs whose YYYYMMDD token falls in the range

Point AOI_DIR at a working directory that already has branches and at least one
discharge CSV (produced by the streamflow pipeline, e.g. getNWMretrospective).
"""

from __future__ import annotations

from pathlib import Path
import pytest
from fimbox import generateFIM, extract_feature_ids

AOI_DIR = Path("/Users/Supath/Downloads/SDML/FIMBOX/out/test_smallB")
N_WORKERS = 4

# Optional selection filters (edit to match the CSVs you have).
EVENT_DATE = "2020-05-20 12:00:00"
START = "2020-05-19"
END = "2020-05-22"

_BRANCHES_DIR = (
    AOI_DIR / "watershed-data" / "branches"
    if (AOI_DIR / "watershed-data" / "branches").is_dir()
    else AOI_DIR / "branches"
)
_skip_no_branches = pytest.mark.skipif(
    not _BRANCHES_DIR.is_dir(), reason=f"AOI not present: {_BRANCHES_DIR}"
)


# @_skip_no_branches
# def test_extract_feature_ids():
#     out_csv = extract_feature_ids(AOI_DIR)
#     assert out_csv.is_file()
#     print(f"\nfeature_id.csv -> {out_csv}")


# default: generate FIM for every discharge CSV in the AOI
@_skip_no_branches
def test_fim_all_discharge_inputs():
    results = generateFIM(
        AOI_DIR, n_workers=N_WORKERS, depth=True
    ).from_discharge_inputs()
    assert results
    for r in results:
        print(f"  extent={r.extent_path}")
        assert r.extent_path is not None and Path(r.extent_path).is_file()


# # a specific CSV
# @_skip_no_branches
# def test_fim_specific_csv():
#     csvs = sorted((AOI_DIR / "discharge-inputs").glob("*.csv"))
#     if not csvs:
#         pytest.skip("no discharge CSVs to pick from")
#     results = generateFIM(AOI_DIR, n_workers=N_WORKERS).from_discharge_inputs(csv=csvs[0])
#     assert len(results) == 1


# # match by date stamp in the filename
# @_skip_no_branches
# def test_fim_by_date():
#     results = generateFIM(AOI_DIR, n_workers=N_WORKERS).from_discharge_inputs(
#         date=EVENT_DATE
#     )
#     assert results


# # match by date range
# @_skip_no_branches
# def test_fim_by_range():
#     results = generateFIM(AOI_DIR, n_workers=N_WORKERS).from_discharge_inputs(
#         start=START, end=END
#     )
#     assert results


# # also write the depth raster
# @_skip_no_branches
# def test_fim_with_depth():
#     results = generateFIM(
#         AOI_DIR, n_workers=N_WORKERS, depth=True
#     ).from_discharge_inputs(date=EVENT_DATE)
#     assert results
#     r = results[0]
#     assert r.depth_path is not None and Path(r.depth_path).is_file()
