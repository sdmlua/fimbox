"""
Download 10m 3DEP DEM per HUC8, save each tile to disk, then merge.
This avoids the in-memory OOM that happens when merging all tiles at once
for a large (24 HUC) study area.

Run ONCE before test_getallinputdata.py, then pass the output path via dem= in the test.
"""
from pathlib import Path

import pandas as pd
import geopandas as gpd
import py3dep
from rasterio.merge import merge as rio_merge

# ── EDIT THESE ────────────────────────────────────────────────────
EXCEL_PATH = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
HUC_CODES = []        # leave empty to use ALL rows; or e.g. [3020102, 3020103]
EPSG = 5070           # CONUS Albers — matches the rest of the fimbox pipeline
RESOLUTION = 10       # metres
# ──────────────────────────────────────────────────────────────────

TEMP_DIR = Path("E:/SI/out/study_area/watershed-data/dem_tiles")
OUT_DEM  = Path("E:/SI/out/study_area/watershed-data/3dep_dem_10m.tif")

from fimbox.preprocessing.download_data.utils import HUC8Finder


def _download_huc(huc8: str, finder: HUC8Finder) -> Path | None:
    tile_path = TEMP_DIR / f"dem_{huc8}.tif"
    if tile_path.exists():
        print(f"  {huc8}: tile already on disk — skipping download")
        return tile_path

    print(f"  {huc8}: fetching boundary …")
    try:
        gdf = finder.from_huc8(huc8)
    except ValueError:
        print(f"  {huc8}: WARNING — not found in service, skipping")
        return None

    geom = gdf.to_crs(epsg=4326).union_all()

    print(f"  {huc8}: downloading 3DEP 10m …")
    dem = py3dep.get_dem(geom, resolution=RESOLUTION, crs=4326)

    print(f"  {huc8}: reprojecting to EPSG:{EPSG} …")
    dem = dem.rio.reproject(f"EPSG:{EPSG}")
    dem = dem.where(dem > -90000, -999999)
    dem.rio.write_nodata(-999999, inplace=True)
    dem.rio.to_raster(
        str(tile_path),
        driver="GTiff", compress="lzw", tiled=True,
        blockxsize=256, blockysize=256,
    )
    print(f"  {huc8}: saved → {tile_path}")
    return tile_path


def main():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DEM.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(EXCEL_PATH)
    print(f"Loaded {len(df)} rows from Excel")

    if HUC_CODES:
        df = df[df[HUC_CODE_COL].isin(HUC_CODES)]
        print(f"Filtered to {len(df)} rows")

    finder = HUC8Finder()
    tile_paths = []
    for code in df[HUC_CODE_COL]:
        huc8 = str(int(code)).zfill(8)
        path = _download_huc(huc8, finder)
        if path is not None:
            tile_paths.append(path)

    if not tile_paths:
        raise RuntimeError("No tiles downloaded — nothing to merge.")

    print(f"\nMerging {len(tile_paths)} tiles to {OUT_DEM} …")
    rio_merge(
        [str(p) for p in tile_paths],
        method="first",
        dst_path=str(OUT_DEM),
    )

    print(f"\nDone. Merged DEM → {OUT_DEM}")
    print("\nNext step: in tests/test_getallinputdata.py add:")
    print(f'    dem=Path("{OUT_DEM}"),')
    print("to the getAllInputData() call.")


if __name__ == "__main__":
    main()
