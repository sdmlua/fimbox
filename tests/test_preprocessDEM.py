# Example Usage:
import logging
from pathlib import Path

import fimbox

log = logging.getLogger(__name__)

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]

boundary = PKG_ROOT / "docs" / "test_boundary" / "test_smallB.shp"
OUT_DIR = REPO_ROOT / "out"


def test_process_dem():
    output_path = fimbox.DEMProcessor(
        boundary=boundary,
        resolution=10,  # 3DEP resolution in m: 1, 3, 10 (default), 30, 60
        output_dir=OUT_DIR / "dem_test",
        # layer=None,            # if boundary is a geopackage with multiple layers
        # dem_file=None,         # local DEM to condition instead of fetching
        # epsg=None,             # output CRS EPSG; None auto-detects the UTM zone
        # fallback_to_10m=False, # if resolution unavailable, use 10m not raise
        # use_dask=True,         # dask chunking for the reproject/heal stage
        # chunksize=None,        # dask chunk edge in px; None -> auto from CPU count
    ).result_path
    log.info(f"3DEP DEM --> {output_path}")
