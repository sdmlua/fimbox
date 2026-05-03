"""
Author: Supath Dhital
Date updated : May 2026

This module contains functions to perform AGREE DEM hydrological conditioning (Hellweger 1997). The main class is:
- HydroenforceDEM: Hydroenforce a DEM so drainage is consistent with a supplied stream.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from scipy.ndimage import distance_transform_edt

log = logging.getLogger(__name__)


@dataclass
class HydroenforceDEM:
    """
    AGREE DEM hydrological conditioning (Hellweger 1997).

    Hydroenforces a DEM so drainage is consistent with a supplied stream network.
    Uses scipy distance transforms

    Parameters
    ----------
    rivers_raster       : stream boolean grid (stream_value=stream, 0=background)
    dem                 : input DEM
    output_raster       : conditioned DEM output path
    workspace           : retained for API compatibility (no intermediate files written)
    buffer_dist         : stream buffer distance in metres (default 15)
    smooth_drop         : smooth drop in metres within buffer zone (default 10)
    sharp_drop          : sharp incision at stream cells in metres (default 1000)
    stream_value        : pixel value in rivers_raster identifying stream cells (default 1)
    """

    rivers_raster: Path
    dem: Path
    output_raster: Path
    workspace: Path
    buffer_dist: float = 15.0
    smooth_drop: float = 10.0
    sharp_drop: float = 1000.0
    stream_value: int = 1
    wbt_path: Optional[str] = None
    keep_intermediates: bool = False

    def __post_init__(self):
        self.rivers_raster = Path(self.rivers_raster)
        self.dem = Path(self.dem)
        self.output_raster = Path(self.output_raster)
        self.workspace = Path(self.workspace)

    def run(self) -> Path:
        log.info(
            "AGREE DEM start: buffer=%.0fm smooth=%.0fm sharp=%.0fm  dem=%s",
            self.buffer_dist, self.smooth_drop, self.sharp_drop, self.dem.name,
        )
        try:
            with rasterio.open(str(self.dem)) as src:
                profile = src.profile.copy()
                dem_data = src.read(1).astype(np.float32)
                dem_mask = src.read_masks(1).astype(bool)
                pixel_size = src.res[0]
                nodata_val = src.nodata if src.nodata is not None else -9999.0
                log.debug(
                    "  DEM grid %dx%d  res=%.1fm  valid_cells=%d",
                    src.width, src.height, pixel_size, int(dem_mask.sum()),
                )

            with rasterio.open(str(self.rivers_raster)) as src:
                rivers_raw = src.read(1)

            stream_mask = dem_mask & (rivers_raw == self.stream_value)
            stream_count = int(stream_mask.sum())
            if stream_count == 0:
                raise RuntimeError(
                    "No stream cells found overlapping the DEM extent. "
                    f"Check CRS alignment and that stream_value={self.stream_value} is correct. "
                    f"dem={self.dem}  rivers={self.rivers_raster}"
                )
            log.debug("  stream cells: %d", stream_count)

            log.debug("  computing euclidean distance from streams")
            not_stream = (~stream_mask).astype(np.uint8)
            vectdist, idx_str = distance_transform_edt(
                not_stream, sampling=pixel_size, return_indices=True
            )

            smogrid = np.where(stream_mask, dem_data - self.smooth_drop, 0.0).astype(np.float32)
            vectallo = smogrid[idx_str[0], idx_str[1]]

            log.debug("  computing euclidean distance from buffer edge (%.0fm)", self.buffer_dist)
            final_buffer = self.buffer_dist - pixel_size / 2
            outside_buf = (vectdist > final_buffer) & dem_mask
            bufgrid = np.where(outside_buf, dem_data, 0.0).astype(np.float32)

            not_outside = (~outside_buf).astype(np.uint8)
            bufdist, idx_buf = distance_transform_edt(
                not_outside, sampling=pixel_size, return_indices=True
            )
            bufallo = bufgrid[idx_buf[0], idx_buf[1]]

            log.debug("  assembling AGREE DEM")
            denom = np.where((bufdist + vectdist) == 0, 1e-10, bufdist + vectdist)
            smoelev = vectallo + ((bufallo - vectallo) / denom) * vectdist

            stream_f = stream_mask.astype(np.float32)
            shagrid = (smoelev - self.sharp_drop) * stream_f
            elevgrid = np.where(stream_mask, shagrid, smoelev)
            agree_dem = np.where(dem_mask, elevgrid, nodata_val).astype(np.float32)

            out_profile = profile.copy()
            out_profile.update(dtype="float32", nodata=nodata_val)
            with rasterio.open(str(self.output_raster), "w", **out_profile) as dst:
                dst.write(agree_dem, 1)

            log.info("AGREE DEM written → %s", self.output_raster.name)
            return self.output_raster

        except Exception:
            log.exception("AGREE DEM FAILED: dem=%s rivers=%s", self.dem, self.rivers_raster)
            raise
