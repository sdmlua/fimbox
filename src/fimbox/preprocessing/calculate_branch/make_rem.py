"""
Author: Supath Dhital
Date Updated: May 2026

REM / HAND (Height Above Nearest Drainage) computation.

Algorithm (two-pass, block-wise — identical to FIM):
  Pass 1  For every pixel that is on the thalweg, record the minimum
          DEM elevation within each pixel-catchment ID.
  Pass 2  REM[i] = DEM[i] - catchment_min[catchment_id[i]]

Inputs
------
dem_thalweg_cond     : dem_thalwegCond_{id}.tif
gw_catchments_pixels : gw_catchments_pixels_{id}.tif
stream_pixels        : demDerived_streamPixels_{id}.tif   (1 = thalweg)

Outputs
-------
rem_{id}.tif  
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio

log = logging.getLogger(__name__)


@dataclass
class MakeREM:
    """
    Compute the REM from a thalweg-conditioned DEM and pixel catchments.

    Parameters
    ----------
    dem_thalweg_cond     : dem_thalwegCond_{id}.tif
    gw_catchments_pixels : gw_catchments_pixels_{id}.tif
    stream_pixels        : demDerived_streamPixels_{id}.tif
    out_rem              : rem_{id}.tif
    """

    dem_thalweg_cond: Path
    gw_catchments_pixels: Path
    stream_pixels: Path
    out_rem: Path

    def __post_init__(self):
        for attr in ("dem_thalweg_cond", "gw_catchments_pixels", "stream_pixels", "out_rem"):
            setattr(self, attr, Path(getattr(self, attr)))
        self.out_rem.parent.mkdir(parents=True, exist_ok=True)

    def run(self) -> Path:
        if self.out_rem.exists() and self.out_rem.stat().st_size > 0:
            log.info("MakeREM: output exists, skipping → %s", self.out_rem.name)
            return self.out_rem

        log.info("MakeREM: building catchment-minimum dict (pass 1)")
        catchment_min = _build_catchment_min(
            self.dem_thalweg_cond,
            self.gw_catchments_pixels,
            self.stream_pixels,
        )
        log.info("MakeREM: %d catchment minima found", len(catchment_min))

        log.info("MakeREM: computing REM (pass 2) → %s", self.out_rem.name)
        _write_rem(
            self.dem_thalweg_cond,
            self.gw_catchments_pixels,
            catchment_min,
            self.out_rem,
        )

        log.info("MakeREM: done → %s", self.out_rem.name)
        return self.out_rem


# Internal helpers
def _build_catchment_min(
    dem_path: Path,
    catchments_path: Path,
    thalweg_path: Path,
) -> dict[int, float]:
    """
    Pass 1: scan every raster block and record the minimum thalweg
    elevation per catchment ID.  Matches make_catchment_min_dict() in FIM.
    """
    catchment_min: dict[int, float] = {}

    with (
        rasterio.open(str(dem_path))        as dem_ds,
        rasterio.open(str(catchments_path)) as cat_ds,
        rasterio.open(str(thalweg_path))    as thal_ds,
    ):
        nodata = dem_ds.nodata

        for _, window in dem_ds.block_windows(1):
            dem_blk   = dem_ds.read(1,  window=window).ravel().astype(np.float32)
            cat_blk   = cat_ds.read(1,  window=window).ravel().astype(np.int32)
            thal_blk  = thal_ds.read(1, window=window).ravel()

            # Only thalweg pixels contribute reference elevation (FIM rule).
            thal_mask = thal_blk == 1
            if not thal_mask.any():
                continue

            dem_t  = dem_blk[thal_mask]
            cat_t  = cat_blk[thal_mask]

            # Skip nodata elevations
            if nodata is not None:
                valid = dem_t != nodata
                dem_t = dem_t[valid]
                cat_t = cat_t[valid]
            if cat_t.size == 0:
                continue

            # Per-catchment minimum — iterate unique IDs in this block.
            # Blocks are small (512×512) so the loop over unique IDs is fast.
            for cid in np.unique(cat_t):
                if cid == 0:
                    continue
                block_min = float(dem_t[cat_t == cid].min())
                if cid in catchment_min:
                    if block_min < catchment_min[cid]:
                        catchment_min[cid] = block_min
                else:
                    catchment_min[cid] = block_min

    return catchment_min


def _write_rem(
    dem_path: Path,
    catchments_path: Path,
    catchment_min: dict[int, float],
    out_path: Path,
) -> None:
    """
    Pass 2: REM[i] = DEM[i] - catchment_min[catchment_id[i]].
    Matches calculate_rem() in FIM.  Written block-wise, float32, LZW.
    """
    with rasterio.open(str(dem_path)) as dem_ds:
        meta = dem_ds.meta.copy()
        nodata = dem_ds.nodata if dem_ds.nodata is not None else -9999.0

    meta.update(
        dtype="float32",
        nodata=float(nodata),
        compress="lzw",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        BIGTIFF="YES",
    )

    # Build lookup array for fast vectorised subtraction.
    # IDs can be large integers; use a dict-based numpy fancy-index approach.
    if catchment_min:
        max_id = max(catchment_min.keys())
    else:
        max_id = 0

    # Lookup table: index = catchment ID, value = min elevation (0.0 = missing)
    lut = np.zeros(max_id + 1, dtype=np.float32)
    valid_ids = np.zeros(max_id + 1, dtype=bool)
    for cid, cmin in catchment_min.items():
        if 0 <= cid <= max_id:
            lut[cid]       = cmin
            valid_ids[cid] = True

    if out_path.exists():
        out_path.unlink()

    with (
        rasterio.open(str(dem_path))        as dem_ds,
        rasterio.open(str(catchments_path)) as cat_ds,
        rasterio.open(str(out_path), "w", **meta) as out_ds,
    ):
        for _, window in dem_ds.block_windows(1):
            dem_blk = dem_ds.read(1, window=window).astype(np.float32)
            cat_blk = cat_ds.read(1, window=window).astype(np.int32)

            rem_blk = np.full(dem_blk.shape, nodata, dtype=np.float32)

            # Pixels with a known catchment ID and valid DEM value
            in_range = (cat_blk > 0) & (cat_blk <= max_id)
            has_lut  = in_range & valid_ids[np.where(in_range, cat_blk, 0)]
            dem_ok   = dem_blk != nodata

            compute  = has_lut & dem_ok
            if compute.any():
                rem_blk[compute] = dem_blk[compute] - lut[cat_blk[compute]]

            out_ds.write(rem_blk, window=window, indexes=1)
