"""
Author: Supath Dhital
Date Updated: May 2026

Block-wise raster algebra helpers that constrain the REM and the D8 slope
raster to the filtered catchment footprint before the hydraulic table is built.

Two operations are exposed:

1. ``rem_zeroed_masked``
       REM[i] = REM[i] if REM[i] >= 0 and gw_catchments_reaches[i] > 0
              else 0 (or nodata where REM was nodata).
       Produces ``rem_zeroed_masked_{id}.tif``.

2. ``mask_slopes_to_catchments``
       slope[i] = slope[i] if gw_catchments_reaches_filtered[i] > 0
                else nodata.
       Produces ``slopes_d8_dem_meters_masked_{id}.tif``.

Both write float32 LZW-compressed BIGTIFFs aligned to the source raster grid.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

import numpy as np
import rasterio

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def rem_zeroed_masked(
    rem_path: PathLike,
    catchments_path: PathLike,
    out_path: PathLike,
    nodata: float = -9999.0,
) -> Path:
    """
    Zero negative REM values and mask to the catchment footprint.

    Parameters
    ----------
    rem_path        : rem_{id}.tif
    catchments_path : gw_catchments_reaches_{id}.tif
    out_path        : rem_zeroed_masked_{id}.tif
    nodata          : nodata value used when the REM source has none defined.
    """
    rem_path = Path(rem_path)
    catchments_path = Path(catchments_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(str(rem_path)) as rem_ds:
        meta = rem_ds.meta.copy()
        nodata_rem = rem_ds.nodata if rem_ds.nodata is not None else float(nodata)

    meta.update(
        dtype="float32",
        nodata=float(nodata_rem),
        compress="lzw",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        BIGTIFF="YES",
    )

    if out_path.exists():
        out_path.unlink()

    with (
        rasterio.open(str(rem_path)) as rem_ds,
        rasterio.open(str(catchments_path)) as cat_ds,
        rasterio.open(str(out_path), "w", **meta) as out_ds,
    ):
        for _, window in rem_ds.block_windows(1):
            rem_blk = rem_ds.read(1, window=window).astype(np.float32)
            cat_blk = cat_ds.read(1, window=window)

            result = (
                rem_blk
                * (rem_blk >= 0).astype(np.float32)
                * (cat_blk > 0).astype(np.float32)
            )
            result[rem_blk == nodata_rem] = nodata_rem
            out_ds.write(result, window=window, indexes=1)

    log.info("rem_zeroed_masked → %s", out_path.name)
    return out_path


def mask_slopes_to_catchments(
    slopes_path: PathLike,
    catchments_path: PathLike,
    out_path: PathLike,
    nodata: float = -9999.0,
) -> Path:
    """
    Keep slope values only where the filtered catchment raster is non-zero.

    Parameters
    ----------
    slopes_path     : slopes_d8_dem_{id}.tif
    catchments_path : gw_catchments_reaches_filtered_addedAttributes_{id}.tif
    out_path        : slopes_d8_dem_meters_masked_{id}.tif
    nodata          : nodata to set outside the catchment mask.
    """
    slopes_path = Path(slopes_path)
    catchments_path = Path(catchments_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(str(slopes_path)) as slp_ds:
        meta = slp_ds.meta.copy()
        nodata_slp = slp_ds.nodata if slp_ds.nodata is not None else float(nodata)

    meta.update(
        dtype="float32",
        nodata=float(nodata_slp),
        compress="lzw",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        BIGTIFF="YES",
    )

    if out_path.exists():
        out_path.unlink()

    with (
        rasterio.open(str(slopes_path)) as slp_ds,
        rasterio.open(str(catchments_path)) as cat_ds,
        rasterio.open(str(out_path), "w", **meta) as out_ds,
    ):
        for _, window in slp_ds.block_windows(1):
            slp_blk = slp_ds.read(1, window=window).astype(np.float32)
            cat_blk = cat_ds.read(1, window=window)

            result = np.where(cat_blk > 0, slp_blk, np.float32(nodata_slp))
            out_ds.write(result.astype(np.float32), window=window, indexes=1)

    log.info("mask_slopes_to_catchments → %s", out_path.name)
    return out_path
