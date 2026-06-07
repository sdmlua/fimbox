"""
Author: Supath Dhital
Date Updated: June 2026

FIM generation on a real AOI, fed by real NWM streamflow.

Step 1 (test_step_1_extract_feature_ids):
    Scan the AOI's per-branch hydroTables and write <AOI_DIR>/feature_id.csv.

Step 2 (test_step_2_fetch_streamflow):
    Retrieve NWM retrospective streamflow for EVENT_DATETIME and write a
    FIM-ready CSV (feature_id, discharge_cms) into <AOI_DIR>/discharge-inputs/.
    Skips when teehr is not installed; falls back to a synthetic seed so step 3
    can still run offline.

Step 3 (test_step_3_generate_fim):
    Run every CSV in <AOI_DIR>/discharge-inputs/ through the FIM generator into
    <AOI_DIR>/fim-outputs/ (named after each CSV).

Also: test_nwm_fim_pipeline_end_to_end exercises the one-call path
NWMFimPipeline.from_retrospective(date=...) — streamflow retrieval + FIM in one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from fimbox import FimGenerator, extract_feature_ids

# Edit this to point at any AOI output directory (the AOI root, or a
# legacy flat AOI dir). branches/ are resolved from watershed-data/ when
# present, falling back to the AOI root for legacy flat layouts.
AOI_DIR = Path("/Users/Supath/Downloads/SDML/FIMBOX/out/test_smallB")

# Parallel branch workers for generation.
N_WORKERS = 4

# Streamflow instant to drive FIM from (NWM retrospective hourly).
EVENT_DATETIME = "2020-05-20 12:00:00"

# branches/ either sit at the AOI root (legacy flat) or under watershed-data/.
_BRANCHES_DIR = (
    AOI_DIR / "watershed-data" / "branches"
    if (AOI_DIR / "watershed-data" / "branches").is_dir()
    else AOI_DIR / "branches"
)

_HAVE_TEEHR = importlib.util.find_spec("teehr") is not None

_skip_no_branches = pytest.mark.skipif(
    not _BRANCHES_DIR.is_dir(), reason=f"AOI not present: {_BRANCHES_DIR}"
)


@_skip_no_branches
def test_step_1_extract_feature_ids():
    out_csv = extract_feature_ids(AOI_DIR)
    assert out_csv == AOI_DIR / "feature_id.csv"
    assert out_csv.is_file()
    df = pd.read_csv(out_csv)
    assert "feature_id" in df.columns and len(df) > 0
    print(f"\nWrote {len(df)} feature_ids -> {out_csv}")


@_skip_no_branches
def test_step_2_fetch_streamflow():
    """Get real NWM streamflow for EVENT_DATETIME into discharge-inputs/.

    When teehr is unavailable, seed a synthetic CSV instead so step 3 still has
    an input to run on.
    """
    feature_id_csv = AOI_DIR / "feature_id.csv"
    if not feature_id_csv.is_file():
        extract_feature_ids(AOI_DIR)
    discharge_dir = AOI_DIR / "discharge-inputs"
    discharge_dir.mkdir(exist_ok=True)

    if _HAVE_TEEHR:
        from fimbox.streamflow import NWMRetrospective

        out = NWMRetrospective(AOI_DIR, feature_id_csv).at(EVENT_DATETIME)
        print(f"\nFetched NWM streamflow -> {out}")
        df = pd.read_csv(out)
        assert {"feature_id", "discharge_cms"}.issubset(df.columns)
    elif not list(discharge_dir.glob("*.csv")):
        df = pd.read_csv(feature_id_csv).assign(discharge_cms=50.0)
        seed = discharge_dir / "sample_discharge.csv"
        df.to_csv(seed, index=False)
        print(f"\nteehr not installed — seeded synthetic discharge -> {seed}")


@_skip_no_branches
def test_step_3_generate_fim():
    discharge_dir = AOI_DIR / "discharge-inputs"
    csv_paths = sorted(discharge_dir.glob("*.csv")) if discharge_dir.is_dir() else []
    if not csv_paths:
        pytest.skip(f"No discharge CSVs in {discharge_dir} — run step 2 first")

    output_dir = AOI_DIR / "fim-outputs"
    output_dir.mkdir(exist_ok=True)
    print(f"\nGenerating FIM for {len(csv_paths)} CSV(s) -> {output_dir}")

    for csv in csv_paths:
        base = csv.stem
        depth_out = output_dir / f"{base}_depth.tif"
        extent_out = output_dir / f"{base}_inundation.tif"
        print(f"\n=== {csv.name} ===")
        result = FimGenerator(
            aoi_dir=AOI_DIR,
            forecast=csv,
            n_workers=N_WORKERS,
            depth_out=depth_out,
            extent_out=extent_out,
        ).run()
        print(f"  depth={result.depth_path}  extent={result.extent_path}")
        print(f"  branches ok/skipped: {result.n_branches_ok}/{result.n_branches_skipped}")
        assert depth_out.is_file() and extent_out.is_file()
        assert result.n_branches_ok >= 1


@_skip_no_branches
@pytest.mark.skipif(not _HAVE_TEEHR, reason="teehr not installed")
def test_nwm_fim_pipeline_end_to_end():
    """One call: retrieve NWM streamflow for an instant and generate FIM."""
    from fimbox import NWMFimPipeline

    results = NWMFimPipeline(AOI_DIR, n_workers=N_WORKERS).from_retrospective(
        date=EVENT_DATETIME
    )
    assert results, "pipeline produced no FIM results"
    r = results[0]
    print(f"\nEnd-to-end FIM: depth={r.depth_path} extent={r.extent_path}")
    assert r.depth_path is not None and Path(r.depth_path).is_file()
