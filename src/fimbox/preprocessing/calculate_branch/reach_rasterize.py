from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.features

log = logging.getLogger(__name__)


def _rasterize_boolean_grid(gpkg: Path, out_path: Path, dem_path: Path) -> int:
    """Rasterize vector features to a 1/0 Int32 grid matching the DEM extent.
    Returns the number of rasterized pixels set to 1."""
    gdf = gpd.read_file(str(gpkg), engine="pyogrio")

    with rasterio.open(str(dem_path)) as src:
        transform = src.transform
        nrows, ncols = src.height, src.width
        crs = src.crs

    if gdf.crs is not None and gdf.crs != crs:
        gdf = gdf.to_crs(crs)

    shapes = (
        (geom, 1) for geom in gdf.geometry if geom is not None and not geom.is_empty
    )
    grid = rasterio.features.rasterize(
        shapes=shapes,
        out_shape=(nrows, ncols),
        transform=transform,
        fill=0,
        dtype=np.int32,
    )

    with rasterio.open(
        str(out_path),
        "w",
        driver="GTiff",
        dtype="int32",
        width=ncols,
        height=nrows,
        count=1,
        crs=crs,
        transform=transform,
        compress="lzw",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        BIGTIFF="YES",
    ) as dst:
        dst.write(grid, 1)

    return int((grid == 1).sum())


@dataclass
class StreamBooleanRasterizer:
    # rasterize all NWM streams to 1/0 grid for branch 0
    streams_gpkg: Path
    dem_path: Path
    out_path: Path

    def run(self) -> Path:
        log.info("Rasterizing NWM streams: %s", Path(self.streams_gpkg).name)
        try:
            n = _rasterize_boolean_grid(self.streams_gpkg, self.out_path, self.dem_path)
            log.info(
                "Stream boolean grid written --> %s  (%d stream pixels)",
                self.out_path.name,
                n,
            )
            if n == 0:
                log.warning(
                    "Stream boolean grid has 0 stream pixels — check CRS alignment"
                )
        except Exception:
            log.exception("StreamBooleanRasterizer FAILED: gpkg=%s", self.streams_gpkg)
            raise
        return self.out_path


@dataclass
class LevelPathBooleanRasterizer:
    # rasterize extended level path streams to 1/0 grid for non-zero branches
    levelpaths_gpkg: Path
    dem_path: Path
    out_path: Path

    def run(self) -> Path:
        log.info("Rasterizing level paths: %s", Path(self.levelpaths_gpkg).name)
        try:
            n = _rasterize_boolean_grid(
                self.levelpaths_gpkg, self.out_path, self.dem_path
            )
            log.info(
                "Level path boolean grid written --> %s  (%d pixels)",
                self.out_path.name,
                n,
            )
            if n == 0:
                log.warning(
                    "Level path boolean grid has 0 pixels — check CRS alignment"
                )
        except Exception:
            log.exception(
                "LevelPathBooleanRasterizer FAILED: gpkg=%s", self.levelpaths_gpkg
            )
            raise
        return self.out_path


@dataclass
class HeadwaterRasterizer:
    # rasterize NWM headwater points to 1/0 grid
    headwaters_gpkg: Path
    dem_path: Path
    out_path: Path

    def run(self) -> Path:
        log.info("Rasterizing headwaters: %s", Path(self.headwaters_gpkg).name)
        try:
            n = _rasterize_boolean_grid(
                self.headwaters_gpkg, self.out_path, self.dem_path
            )
            log.info(
                "Headwater boolean grid written --> %s  (%d pixels)",
                self.out_path.name,
                n,
            )
            if n == 0:
                log.warning("Headwater boolean grid has 0 pixels — check CRS alignment")
        except Exception:
            log.exception("HeadwaterRasterizer FAILED: gpkg=%s", self.headwaters_gpkg)
            raise
        return self.out_path
