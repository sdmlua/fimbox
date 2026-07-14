"""
Author: Supath Dhital
Date Created: July 2026

FIM evaluation tests: query benchmark FIMs from the FIMbench database
(queryBenchmarkFIM) and evaluate candidate FIMs against them with FIMeval
(evaluateFIM).

Point AOI_DIR at a working directory that already has flood maps in
fim-outputs/ (produced by test_fimgeneration). fimbench and fimeval install
together with fimbox.
"""

from __future__ import annotations

from pathlib import Path

AOI_DIR = Path(__file__).resolve().parents[2] / "out" / "test_smallB"

# Boundary-extraction method: "smallest_extent" | "convex_hull" | "AOI"
METHOD_NAME = "smallest_extent"

# Optional query filters (edit to match your AOI / event).
EVENT_DATE = None  # e.g. "2017-08-30"
START, END = "2016-04-01", "2026-01-01"
TIER = None  # e.g. "HWM", "tier1"


# COMBINED — the whole evaluation pipeline in one go: query the FIMbench
# catalog with the AOI's newest flood extent, download the matched benchmark
# assets into <aoi>/benchmark-data/, then run FIMeval (EvaluateFIM +
# contingency maps + metric plots) on the staged case.
def test_fimevaluation_combined():
    from fimbox import evaluateFIM, queryBenchmarkFIM

    query = queryBenchmarkFIM(
        AOI_DIR,  # footprint = newest extent raster in <aoi>/fim-outputs/
        # raster_path="my_fim.tif",     # explicit candidate raster instead
        # boundary_path="my_aoi.gpkg",  # or an AOI boundary vector
        # huc8="03020201",              # narrow by basin
        tier=TIER,  # narrow by benchmark tier; None -> all tiers
        event_date=EVENT_DATE,  # exact event date; None -> ignore
        start_date=START,  # date-range start
        end_date=END,  # date-range end
        area=True,  # add overlap % / km^2 per match
        download=True,  # fetch GeoTIFF + GeoPackage
        # out_dir="downloads/",         # default <aoi>/benchmark-data/
    )
    print(query)  # pretty match summary

    result = evaluateFIM(
        AOI_DIR,
        # candidate=None,               # default: every extent .tif in fim-outputs/
        # benchmark=None,               # default: newest .tif in benchmark-data/
        # case_name=None,               # default: first candidate's stem
        method_name=METHOD_NAME,
        # aoi_boundary="my_aoi.gpkg",   # required when method_name="AOI"
        # pwb_dir="my_pwb.gpkg",        # own permanent-water-bodies vector
        # target_crs="EPSG:32633",      # outside CONUS (default EPSG:5070)
        # target_resolution=10,         # m, when resolutions differ
        contingency_map=True,
        plot_metrics=True,
        # building_footprint=True,      # building-level agreement (GEE auth)
    )
    assert result.case_dir.is_dir()
    assert result.output_dir.is_dir()
    assert "benchmark" in result.benchmark.name.lower()
    assert result.metrics_files, "FIMeval produced no metrics CSV"
    print(result.metrics)


# # STEP BY STEP — each stage on its own.

# def test_step_query_catalog_only():
#     """Catalog-only discovery: what benchmarks exist in a date range
#     (no AOI, no download). Returns plain dicts straight from the catalog."""
#     from fimbox import queryBenchmarkFIM

#     response = queryBenchmarkFIM(start_date=START, end_date=END)
#     print(response)
#     for record in response.records:
#         print(record)


# def test_step_query_by_filename():
#     """Direct download by exact catalog filename."""
#     from fimbox import queryBenchmarkFIM

#     response = queryBenchmarkFIM(
#         file_name="HWM_10_0m_20160928_20161009_780051W352232N_BM.tif",
#         download=True,
#         out_dir=AOI_DIR / "benchmark-data",
#     )
#     assert response.downloads


# def test_step_evaluate_own_benchmark():
#     """Evaluate against a benchmark raster you already have on disk
#     (skips the FIMbench query entirely)."""
#     from fimbox import evaluateFIM

#     result = evaluateFIM(
#         AOI_DIR,
#         benchmark="path/to/my_benchmark.tif",
#         case_name="own_benchmark",
#         method_name="convex_hull",
#     )
#     assert result.metrics_files


# def test_step_building_footprint():
#     """Building-level agreement analysis. Uses the Microsoft global building
#     footprints via Google Earth Engine by default (pops a GEE auth prompt),
#     or pass building_footprint_file= to use your own vector."""
#     from fimbox import evaluateFIM

#     result = evaluateFIM(
#         AOI_DIR,
#         method_name=METHOD_NAME,
#         building_footprint=True,
#         # building_footprint_file="path/to/footprints.gpkg",
#     )
#     assert result.output_dir.is_dir()
