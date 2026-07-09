"""
Download 10m 3DEP DEM for the full study area using fimbox.DEMProcessor.

DEMProcessor reads directly from Planetary Computer COGs (cloud-optimised
GeoTIFFs) — only the AOI window is fetched over HTTP, so there is no national
VRT parsing and no per-tile OOM.  It handles reprojection, hole-fill, and clip
internally.  ~8x faster than the py3dep tile-and-merge approach in
s01_download_dem.py.

Run ONCE before the main pipeline.  Pass the output path via dem= in
test_getallinputdata.py.

Usage:
    .venv\\Scripts\\python.exe scripts/s01_download_dem_fast.py
"""
from pathlib import Path

import fimbox

# ── CONFIG ────────────────────────────────────────────────────────
BOUNDARY   = Path("docs/study_boundary/study_area.gpkg")
RESOLUTION = 10          # metres  (1 / 3 / 10 / 30 — 10 m is nationwide)
EPSG       = 5070        # CONUS Albers — matches the rest of the pipeline
OUT_DIR    = Path("E:/SI/out/study_area/watershed-data")
OUT_NAME   = "3dep_dem_10m.tif"
# ─────────────────────────────────────────────────────────────────


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Boundary  : {BOUNDARY}")
    print(f"Resolution: {RESOLUTION} m")
    print(f"Output    : {OUT_DIR / OUT_NAME}")
    print("Downloading …")

    fimbox.DEMProcessor(
        boundary=BOUNDARY,
        output_dir=OUT_DIR,
        resolution=RESOLUTION,
        out_name=OUT_NAME,
        epsg=EPSG,
    )

    out = OUT_DIR / OUT_NAME
    print(f"\nDone → {out}")
    print(f"\nNext step: pass this path as dem= in test_getallinputdata.py:")
    print(f'    dem=Path("{out}"),')


if __name__ == "__main__":
    main()
