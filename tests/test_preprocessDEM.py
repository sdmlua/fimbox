# Example Usage:
import logging
from pathlib import Path

import fimbox

log = logging.getLogger(__name__)
boundary = Path("/Users/supath/Downloads/MSResearch/FIMBOX/03020202/wbd.gpkg")


def test_process_dem():
    output_path = fimbox.DEMProcessor(
        boundary=boundary,
        resolution=10,  # desired DEM resolution in meters (1, 3, or 10), for 3DEP fetch and other
        output_dir=Path("./dem_test"),
        # layer: Optional[str] = None,     #if boundary is geopackage with multiple layers
        # dem_file: Optional[str] = None,  #path to local DEM file if available or outside CONUS
        # epsg: Optional[int] = None       #desired output CRS EPSG code for projection, if None auto-detects UTM zone
    )
    log.info(f"3DEP DEM --> {output_path}")
