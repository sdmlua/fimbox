"""
Author: Supath Dhital
Date Created: June 2026

Streamflow retrieval / plot / statistics tests, plus the default NWM FIM
pipeline. 

Point AOI_DIR at any AOI whose feature_id.csv exists. Edit the dates/sites to
your basin before running the live steps.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from fimbox.streamflow import _common as C

# Edit these for your AOI.
AOI_DIR = Path("/Users/Supath/Downloads/SDML/FIMBOX/out/test_smallB")
FEATURE_ID_CSV = AOI_DIR / "feature_id.csv"

# Retrospective window, a USGS site for comparison.
START = "2020-05-19"
END = "2020-05-22"
EVENT = "2020-05-20 12:00:00"
USGS_SITE = "07289000"


def _have(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


needs_teehr = pytest.mark.skipif(not _have("teehr"), reason="teehr not installed")
needs_mpl = pytest.mark.skipif(not _have("matplotlib"), reason="matplotlib not installed")
needs_s3 = pytest.mark.skipif(
    not (_have("s3fs") and _have("xarray")), reason="s3fs/xarray not installed"
)
needs_fids = pytest.mark.skipif(
    not FEATURE_ID_CSV.exists(), reason=f"no feature_id.csv at {FEATURE_ID_CSV}"
)


# offline: always runs
def test_statistics_math():
    from fimbox.streamflow import compute_metrics

    obs = pd.Series([10.0, 20.0, 30.0, 40.0])
    perfect = compute_metrics(obs.values, obs.values)
    assert perfect["KGE"] == pytest.approx(1.0, abs=1e-6)
    assert perfect["NSE"] == pytest.approx(1.0, abs=1e-6)
    assert perfect["PBias (%)"] == pytest.approx(0.0, abs=1e-6)

    sim = obs * 1.1
    biased = compute_metrics(sim.values, obs.values)
    assert biased["NSE"] < 1.0
    assert biased["PBias (%)"] > 0.0


def test_fim_csv_normalization(tmp_path):
    """The FIM forecast loader accepts both 'discharge' and 'discharge_cms'."""
    from fimbox.fimgeneration.pipeline import _load_forecast

    legacy = tmp_path / "legacy.csv"
    pd.DataFrame({"feature_id": [1, 2], "discharge": [5.0, 6.0]}).to_csv(legacy, index=False)
    df = _load_forecast(legacy)
    assert "discharge_cms" in df.columns
    assert df["discharge_cms"].tolist() == [5.0, 6.0]


def test_archive_select(tmp_path):
    """select_from_archive slices a synthetic parquet without any download."""
    from fimbox.streamflow.nwm_retrospective import NWMRetrospective

    aoi = tmp_path / "MyAOI"
    aoi.mkdir()
    fids = aoi / "feature_id.csv"
    pd.DataFrame({"feature_id": [101, 202]}).to_csv(fids, index=False)

    retro = NWMRetrospective(aoi, fids)
    times = pd.date_range("2020-05-20 00:00:00", periods=3, freq="h")
    rows = []
    for t in times:
        for fid, v in ((101, 10.0), (202, 20.0)):
            rows.append({"location_id": f"nwm30-{fid}", "value_time": t, "value": v})
    parquet = retro.archive_dir / "20200520_20200520.parquet"
    pd.DataFrame(rows).to_parquet(parquet)

    out = retro.select_from_archive(date="2020-05-20 01:00:00")
    assert len(out) == 1 and out[0].exists()
    got = pd.read_csv(out[0])
    assert set(got.columns) == {"feature_id", "discharge_cms"}
    assert sorted(got["feature_id"]) == [101, 202]


# live: NWM retrospective -> FIM-ready CSVs
@needs_teehr
@needs_fids
def test_nwm_retrospective_range():
    from fimbox.streamflow import NWMRetrospective

    csvs = NWMRetrospective(AOI_DIR, FEATURE_ID_CSV).to_fim_inputs(START, END)
    assert csvs, "no FIM-ready CSVs written"
    df = pd.read_csv(csvs[0])
    assert {"feature_id", "discharge_cms"}.issubset(df.columns)


@needs_teehr
@needs_fids
def test_nwm_retrospective_instant():
    from fimbox.streamflow import NWMRetrospective

    out = NWMRetrospective(AOI_DIR, FEATURE_ID_CSV).at(EVENT)
    assert out.is_file()


# live: NWM forecast
@pytest.mark.skipif(not _have("netCDF4"), reason="netCDF4 not installed")
@needs_fids
def test_nwm_forecast_shortrange():
    from fimbox.streamflow import NWMForecast

    out = NWMForecast(AOI_DIR, FEATURE_ID_CSV).to_fim_inputs("shortrange")
    # forecast availability varies; just assert it ran and returned a list.
    assert isinstance(out, list)


# live: USGS + statistics
@needs_teehr
@needs_fids
def test_usgs_fetch():
    from fimbox.streamflow import USGSData

    USGSData(AOI_DIR).fetch([USGS_SITE], START, END)


@needs_mpl
@needs_fids
def test_plot_nwm():
    from fimbox.streamflow import plot_nwm

    fids = C.load_feature_ids(FEATURE_ID_CSV)[:3]
    out = plot_nwm(AOI_DIR, fids, START, END)
    if out is not None:
        assert out.suffix == ".png"
        # plots live under watershed-data/plots
        assert out.parent == C.plots_dir(AOI_DIR)


@needs_teehr
@needs_mpl
@needs_fids
def test_statistics_vs_usgs():
    from fimbox.streamflow import calculate_statistics

    fid = C.load_feature_ids(FEATURE_ID_CSV)[0]
    metrics = calculate_statistics(AOI_DIR, fid, USGS_SITE, START, END, plot=True)
    assert -10 <= metrics.kge <= 1.0


# live: GEOGLOWS (needs a hydrotable mapping LINKNO -> feature_id)
@needs_s3
@needs_fids
def test_geoglows():
    from fimbox.streamflow import GeoglowsData

    hydrotable = AOI_DIR / "geoglows_hydrotable.csv"
    if not hydrotable.exists():
        pytest.skip("no GEOGLOWS hydrotable (LINKNO -> feature_id) present")
    GeoglowsData(AOI_DIR, hydrotable).fetch(EVENT)


# live: end-to-end NWM -> FIM
@needs_teehr
@needs_fids
@pytest.mark.skipif(
    not (AOI_DIR / "watershed-data" / "branches").is_dir(),
    reason="no branches — run branch processing first",
)
def test_nwm_fim_pipeline():
    from fimbox import NWMFimPipeline

    results = NWMFimPipeline(AOI_DIR, FEATURE_ID_CSV, n_workers=1).from_retrospective(
        date=EVENT
    )
    assert results and results[0].depth_path is not None
