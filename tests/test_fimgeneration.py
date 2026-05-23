"""
Author: Supath Dhital
Date Updated: May 2026

Two-step FIM generation on a real AOI.

Step 1 (test_step_1_extract_feature_ids):
    Scan the AOI's per-branch hydroTables and write
    <AOI_DIR>/feature_id.csv listing every unique feature_id.

Step 2 (test_step_2_generate_fim):
    Read every CSV in <AOI_DIR>/discharge_inputs/ (each containing
    feature_id + discharge_cms columns) and produce inundation depth +
    extent rasters. Each output lands in <AOI_DIR>/fimbox_output/ named
    after the input CSV:
        <basename>_inundation.tif    extent raster (signed HydroID)
        <basename>_depth.tif         depth raster (meters)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fimbox import FimGenerator, extract_feature_ids

# Edit this to point at any AOI output directory.
AOI_DIR = Path("/Users/Supath/Downloads/SDML/FIMBOX/out/test_smallB")

# Parallel branch workers for step 2.
N_WORKERS = 4


@pytest.mark.skipif(
    not (AOI_DIR / "branches").is_dir(),
    reason=f"AOI not present: {AOI_DIR}/branches",
)
def test_step_1_extract_feature_ids():
    out_csv = extract_feature_ids(AOI_DIR)

    assert out_csv == AOI_DIR / "feature_id.csv"
    assert out_csv.is_file()

    df = pd.read_csv(out_csv)
    assert "feature_id" in df.columns
    assert len(df) > 0
    print(f"\nWrote {len(df)} feature_ids -> {out_csv}")
    discharge_dir = AOI_DIR / "discharge_inputs"
    discharge_dir.mkdir(exist_ok=True)
    if not list(discharge_dir.glob("*.csv")):
        sample = discharge_dir / "sample_discharge.csv"
        df.assign(discharge_cms=50.0).to_csv(sample, index=False)
        print(f"Seeded sample discharge CSV -> {sample}")


@pytest.mark.skipif(
    not (AOI_DIR / "branches").is_dir(),
    reason=f"AOI not present: {AOI_DIR}/branches",
)
def test_step_2_generate_fim():
    discharge_dir = AOI_DIR / "discharge_inputs"
    if not discharge_dir.is_dir():
        pytest.skip(
            f"{discharge_dir} not found — run test_step_1_extract_feature_ids first"
        )

    csv_paths = sorted(discharge_dir.glob("*.csv"))
    if not csv_paths:
        pytest.skip(f"No discharge CSVs in {discharge_dir}")

    output_dir = AOI_DIR / "fimbox_output"
    output_dir.mkdir(exist_ok=True)

    print(f"\nFound {len(csv_paths)} discharge CSV(s) in {discharge_dir}")
    print(f"Writing outputs to {output_dir}")

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

        print(f"  Depth raster : {result.depth_path}")
        print(f"  Extent raster: {result.extent_path}")
        print(
            f"  Branches ok/skipped: {result.n_branches_ok}/"
            f"{result.n_branches_skipped}"
        )
        if result.mosaic is not None:
            print(
                f"  Wet pixels  : {result.mosaic.n_wet_pixels:,}  "
                f"max depth: {result.mosaic.max_depth_m:.2f} m"
            )

        assert depth_out.is_file()
        assert extent_out.is_file()
        assert result.n_branches_ok >= 1
