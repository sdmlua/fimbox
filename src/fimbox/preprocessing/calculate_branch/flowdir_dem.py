from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# WBT d8_pointer direction --> (row_offset, col_offset)
# Diagonal directions have distance factor sqrt(2); cardinal = 1.
_D8_OFFSETS = {
    1: (0, 1, 1.0),  # E
    2: (1, 1, 1.41421356),  # SE
    4: (1, 0, 1.0),  # S
    8: (1, -1, 1.41421356),  # SW
    16: (0, -1, 1.0),  # W
    32: (-1, -1, 1.41421356),  # NW
    64: (-1, 0, 1.0),  # N
    128: (-1, 1, 1.41421356),  # NE
}


@dataclass
class FlowdirDEM:
    """
    D8 flow direction from a pit-filled DEM.

    Parameters
    ----------
    dem      : pit-filled DEM input (dem_burned_filled_{id}.tif)
    out_path : D8 flow pointer output (flowdir_d8_burned_filled_{id}.tif)
    wbt_path : WhiteboxTools executable directory; falls back to WBT_PATH env var
    """

    dem: Path
    out_path: Path
    wbt_path: Optional[str] = None

    def __post_init__(self):
        self.dem = Path(self.dem)
        self.out_path = Path(self.out_path)

    def run(self) -> Path:
        log.info("D8 flow direction start: %s", self.dem.name)
        try:
            # Invoke WBT D8Pointer via the concurrency-safe runner. WBT's own
            # run_tool() does a process-global os.chdir and returns 0 even when
            # it wrote nothing, which under parallel branches left flowdir
            # silently absent ("No such file or directory") for one branch per
            # run. run_wbt_tool calls the binary by absolute path and verifies
            # the output exists before returning. Paths are absolute so the
            # WBT-install cwd doesn't matter.
            from ._wbt_safe import run_wbt_tool

            run_wbt_tool(
                "D8Pointer",
                [
                    f"--dem={self.dem.resolve()}",
                    f"--output={self.out_path.resolve()}",
                ],
                out_path=self.out_path,
                wbt_path=self.wbt_path,
            )
            log.info("D8 flow direction written --> %s", self.out_path.name)
            return self.out_path
        except Exception:
            log.exception("D8 flow direction FAILED: dem=%s", self.dem)
            raise


@dataclass
class D8SlopeDEM:
    """
    Compute D8 slope raster from a DEM and its D8 flow direction pointer.

    For each cell, slope = (z_current − z_downstream) / distance_to_downstream,
    clamped to [slope_min, ∞).  Diagonal directions use distance = res × √2.

    Computes D8 flow direction and slope on the lateral-thalweg-adjusted DEM.

    Parameters
    ----------
    dem       : dem_lateral_thalweg_adj_{id}.tif
    flowdir   : flowdir_d8_burned_filled_{id}.tif  (WBT d8_pointer)
    out_path  : slopes_d8_dem_meters_{id}.tif
    slope_min : minimum slope floor (default 0.0001)
    """

    dem: Path
    flowdir: Path
    out_path: Path
    slope_min: float = 0.0001
    # Upper bound on a physical channel slope. DEM artifacts / edge cells can
    # otherwise yield impossible drops (e.g. 1000s) that collapse discharge
    # downstream once clipped. Matches inundation-mapping's SLOPE_MAX.
    slope_max: float = 0.5

    def __post_init__(self):
        self.dem = Path(self.dem)
        self.flowdir = Path(self.flowdir)
        self.out_path = Path(self.out_path)

    def run(self) -> Path:
        import rasterio

        log.info("D8 slopes start: %s", self.dem.name)

        with rasterio.open(str(self.dem)) as dem_ds:
            elev = dem_ds.read(1).astype(np.float64)
            nodata = dem_ds.nodata
            res = dem_ds.res[0]
            profile = dem_ds.profile.copy()

        with rasterio.open(str(self.flowdir)) as fd_ds:
            d8 = fd_ds.read(1)

        slope = _compute_d8_slope(elev, d8, res, nodata, self.slope_min, self.slope_max)

        profile.update(
            dtype="float32",
            nodata=nodata,
            compress="lzw",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="YES",
        )
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(str(self.out_path), "w", **profile) as dst:
            dst.write(slope.astype(np.float32), 1)

        log.info("D8 slopes written --> %s", self.out_path.name)
        return self.out_path


def _compute_d8_slope(
    elev: np.ndarray,
    d8: np.ndarray,
    res: float,
    nodata: Optional[float],
    slope_min: float,
    slope_max: float = 0.5,
) -> np.ndarray:
    """Vectorised D8 slope computation (one pass per direction)."""
    rows, cols = elev.shape
    n = rows * cols
    slope = np.full(n, slope_min, dtype=np.float64)

    flat_elev = elev.ravel()
    flat_d8 = d8.ravel()
    r_all = np.arange(n) // cols
    c_all = np.arange(n) % cols

    nodata_mask = flat_elev == nodata if nodata is not None else np.zeros(n, dtype=bool)

    for d8_val, (dr, dc, dist_factor) in _D8_OFFSETS.items():
        sel = flat_d8 == d8_val
        if not sel.any():
            continue

        r_ds = r_all + dr
        c_ds = c_all + dc
        in_bounds = sel & (r_ds >= 0) & (r_ds < rows) & (c_ds >= 0) & (c_ds < cols)
        valid = in_bounds & ~nodata_mask

        z_cur = flat_elev[valid]
        ds_idx = r_ds[valid] * cols + c_ds[valid]
        z_ds = flat_elev[ds_idx]

        # exclude cells whose downstream neighbour is nodata
        if nodata is not None:
            valid_ds = z_ds != nodata
            z_cur = z_cur[valid_ds]
            z_ds = z_ds[valid_ds]
            valid_idxs = np.where(valid)[0][valid_ds]
        else:
            valid_idxs = np.where(valid)[0]

        s = (z_cur - z_ds) / (dist_factor * res)
        # Clamp to [slope_min, slope_max]: floor flat/uphill cells, cap
        # impossible drops from DEM artifacts so they can't poison discharge.
        slope[valid_idxs] = np.clip(s, slope_min, slope_max)

    if nodata is not None:
        slope[nodata_mask] = nodata

    return slope.reshape(rows, cols)
