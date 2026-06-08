"""
Author: Supath Dhital
Date Created: June 2026

Streamflow retrieval / plot / statistics — minimal, call-the-function tests.

Point AOI_DIR at a working directory whose feature_id.csv exists (or pass a
feature_ids list / CSV per call). Edit the dates and USGS site to your basin,
then run the functions you want.
"""

from __future__ import annotations

from pathlib import Path

from fimbox import (
    getNWMretrospective,
    getNWMforecast,
    get_usgs_fid_pairs,
    USGSData,
    plot_nwm,
    plot_usgs,
    plot_comparison,
    calculate_statistics,
)

AOI_DIR = Path(".././out/test_smallB")

START = "2020-05-19"
END = "2020-05-22"
EVENT = "2020-05-20 12:00:00"
USGS_SITE = "07289000"
FEATURE_ID = 25747229


# retrospective — different extraction combinations
# def test_retrospective_event_date():
#     getNWMretrospective(AOI_DIR, date=EVENT)


# def test_retrospective_range_continuous():
#     # start + end, nothing else -> one CSV per hour
#     getNWMretrospective(AOI_DIR, start=START, end=END)


# def test_retrospective_range_sortby():
#     # start + end + sortby -> one aggregated CSV
#     getNWMretrospective(AOI_DIR, start=START, end=END, sortby="maximum")


# def test_retrospective_feature_ids_list():
#     # pass feature_ids directly instead of relying on the AOI's feature_id.csv
#     getNWMretrospective(AOI_DIR, feature_ids=[FEATURE_ID], date=EVENT)


# # forecast — different combinations
# def test_forecast_shortrange():
#     getNWMforecast(AOI_DIR, "shortrange")


# def test_forecast_mediumrange_maxsort():
#     getNWMforecast(AOI_DIR, "mediumrange", sort_by="maximum")


# def test_forecast_specific_cycle():
#     getNWMforecast(AOI_DIR, "shortrange", forecast_date="2024-06-01", hour=12)


# # USGS observations
# def test_usgs_fetch():
#     USGSData(AOI_DIR).fetch([USGS_SITE], START, END)


def test_usgs_feature_id_pairs():
    # which USGS gage falls on which reach (feature_id) within the AOI
    pairs = get_usgs_fid_pairs(AOI_DIR)
    print(pairs)


# plots
def test_plot_feature_id():
    plot_nwm(AOI_DIR, [FEATURE_ID], START, END)


# def test_plot_usgs():
#     plot_usgs(AOI_DIR, [USGS_SITE], START, END)


# def test_plot_usgs_and_feature_id():
#     # time series overlay of USGS and the NWM feature_id together
#     plot_comparison(AOI_DIR, FEATURE_ID, USGS_SITE, START, END)


# # statistics
# def test_statistics_usgs_vs_nwm():
#     calculate_statistics(AOI_DIR, FEATURE_ID, USGS_SITE, START, END)
