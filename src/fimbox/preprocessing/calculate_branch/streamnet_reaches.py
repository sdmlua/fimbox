"""
Author: Supath Dhital
Date Updated May 2026

Stream network delineation using WhiteboxTools.

Inputs
------
flowdir         : flowdir_d8_burned_filled_{id}.tif
dem_thalweg_cond: dem_thalwegCond_{id}.tif
flowaccum       : flowaccum_d8_burned_filled_{id}.tif
stream_pixels   : demDerived_streamPixels_{id}.tif

Outputs:
----------------------------------------------
streamOrder_{id}.tif          – Strahler stream order raster
sn_catchments_reaches_{id}.tif – per-reach catchment raster (stream link IDs)
demDerived_reaches_{id}.gpkg  – vectorised stream network (polylines)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class StreamNetReaches:
    """
    Delineate stream network reach topology using WBT.

    Parameters
    ----------
    flowdir          : flowdir_d8_burned_filled_{id}.tif (WBT d8_pointer)
    dem_thalweg_cond : dem_thalwegCond_{id}.tif
    flowaccum        : flowaccum_d8_burned_filled_{id}.tif
    stream_pixels    : demDerived_streamPixels_{id}.tif
    out_dir          : branch output directory
    branch_id        : branch identifier string (e.g. "0")
    """

    flowdir: Path
    dem_thalweg_cond: Path
    flowaccum: Path
    stream_pixels: Path
    out_dir: Path
    branch_id: str
    wbt_path: Optional[str] = None

    def __post_init__(self):
        for attr in ("flowdir", "dem_thalweg_cond", "flowaccum", "stream_pixels", "out_dir"):
            setattr(self, attr, Path(getattr(self, attr)))
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict[str, Path]:
        wbt = self._wbt()
        bid = self.branch_id

        stream_order   = self.out_dir / f"streamOrder_{bid}.tif"
        sn_catchments  = self.out_dir / f"sn_catchments_reaches_{bid}.tif"
        reaches_gpkg   = self.out_dir / f"demDerived_reaches_{bid}.gpkg"

        # skip if all outputs already exist
        if stream_order.exists() and sn_catchments.exists() and reaches_gpkg.exists():
            log.info("StreamNetReaches: outputs exist, skipping branch %s", bid)
            return {
                "stream_order": stream_order,
                "sn_catchments_reaches": sn_catchments,
                "demDerived_reaches": reaches_gpkg,
            }

        # Strahler stream order raster
        log.info("StreamNet: Strahler order --> %s", stream_order.name)
        wbt.strahler_stream_order(
            str(self.flowdir),
            str(self.stream_pixels),
            str(stream_order),
        )

        # Per-reach catchment raster (stream link IDs)
        log.info("StreamNet: stream link IDs --> %s", sn_catchments.name)
        wbt.stream_link_identifier(
            str(self.flowdir),
            str(self.stream_pixels),
            str(sn_catchments),
        )

        # WBT raster_streams_to_vector always writes a Shapefile regardless of
        # the extension given.  Write to a temp .shp, then convert to .gpkg.
        reaches_shp = self.out_dir / f"demDerived_reaches_{bid}.shp"
        log.info("StreamNet: vectorise streams --> %s (via shp)", reaches_gpkg.name)
        wbt.raster_streams_to_vector(
            str(sn_catchments),   # unique reach IDs → one polyline per reach
            str(self.flowdir),
            str(reaches_shp),
        )

        # convert shapefile → GeoPackage (set CRS from flowdir if WBT omitted .prj)
        import geopandas as gpd
        import rasterio as _rio
        if reaches_shp.exists():
            gdf = gpd.read_file(str(reaches_shp))
            if gdf.crs is None:
                with _rio.open(str(self.flowdir)) as _src:
                    gdf = gdf.set_crs(_src.crs)
            gdf.to_file(str(reaches_gpkg), driver="GPKG", engine="fiona")
            for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx"):
                p = reaches_shp.with_suffix(ext)
                if p.exists():
                    p.unlink()
        else:
            log.warning("StreamNet: WBT produced no shapefile for branch %s", bid)

        # Apply LZW compression to raster outputs
        _recompress_lzw(stream_order)
        _recompress_lzw(sn_catchments)

        log.info("StreamNetReaches done for branch %s", bid)
        return {
            "stream_order": stream_order,
            "sn_catchments_reaches": sn_catchments,
            "demDerived_reaches": reaches_gpkg,
        }

    def _wbt(self):
        import whitebox
        wbt = whitebox.WhiteboxTools()
        wbt.set_verbose_mode(False)
        wbt_dir = self.wbt_path or os.environ.get("WBT_PATH")
        if wbt_dir:
            wbt.set_whitebox_dir(wbt_dir)
        return wbt


def _recompress_lzw(path: Path) -> None:
    import rasterio
    import numpy as np
    import shutil

    if not path.exists():
        return
    tmp = path.with_suffix(".tmp.tif")
    try:
        with rasterio.open(str(path)) as src:
            profile = src.profile.copy()
            profile.update(
                compress="lzw", tiled=True,
                blockxsize=512, blockysize=512, BIGTIFF="YES",
            )
            data = src.read(1)
        with rasterio.open(str(tmp), "w", **profile) as dst:
            dst.write(data, 1)
        shutil.move(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            tmp.unlink()
