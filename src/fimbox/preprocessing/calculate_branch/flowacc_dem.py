"""
Author: Supath Dhital
Date Updated: May 2026

D8 flow accumulation along headwater stream network.

Inputs
------
flowdir      : flowdir_d8_burned_filled_{id}.tif (WBT D8 pointer)
headwaters   : headwaters_{id}.tif               (rasterised NWM headwater points)

Outputs
-------
out_flowaccum    : flowaccum_d8_burned_filled_{id}.tif
out_stream_pixels: demDerived_streamPixels_{id}.tif  (1=stream, nodata=-9999)
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# WBT D8 pointer encoding: powers-of-2 --> (row_offset, col_offset)
# 64=N  128=NE  1=E  2=SE  4=S  8=SW  16=W  32=NW
_D8_OFFSETS: dict[int, tuple[int, int]] = {
    1: (0, 1),
    2: (1, 1),
    4: (1, 0),
    8: (1, -1),
    16: (0, -1),
    32: (-1, -1),
    64: (-1, 0),
    128: (-1, 1),
}


@dataclass
class FlowAccDEM:
    """
    Headwater-weighted D8 flow accumulation using a topological BFS.

    No external dependencies beyond numpy and rasterio — avoids the
    numba/llvmlite build requirement that pyflwdir carries.

    Parameters
    ----------
    flowdir           : WBT D8 pointer raster (flowdir_d8_burned_filled_{id}.tif)
    headwaters        : rasterised NWM headwater points (1 = headwater)
    out_flowaccum     : output flow accumulation raster
    out_stream_pixels : binary stream pixels (1=stream, nodata=-9999)
    threshold         : minimum accumulated headwater count to mark as stream (default 1)
    """

    flowdir: Path
    headwaters: Path
    out_flowaccum: Path
    out_stream_pixels: Path
    threshold: float = 1.0

    def __post_init__(self):
        for attr in ("flowdir", "headwaters", "out_flowaccum", "out_stream_pixels"):
            setattr(self, attr, Path(getattr(self, attr)))
        self.out_flowaccum.parent.mkdir(parents=True, exist_ok=True)

    def run(self) -> tuple[Path, Path]:
        import rasterio

        log.info("FlowAccDEM: reading D8 flow direction --> %s", self.flowdir.name)
        with rasterio.open(str(self.flowdir)) as src:
            profile = src.profile.copy()
            d8_raw = src.read(1)
            nodata_d8 = src.nodata

        log.info("FlowAccDEM: reading headwaters raster --> %s", self.headwaters.name)
        with rasterio.open(str(self.headwaters)) as src:
            hw_raw = src.read(1).astype(np.float32)
            nodata_hw = src.nodata

        # zero out nodata cells so they don't contribute
        if nodata_hw is not None:
            hw_raw[hw_raw == nodata_hw] = 0.0
        # treat WBT nodata D8 cells as outlets (code = 0 --> no downstream)
        d8 = d8_raw.copy()
        if nodata_d8 is not None:
            d8[d8_raw == nodata_d8] = 0

        log.info(
            "FlowAccDEM: topological BFS on %d × %d grid", d8.shape[0], d8.shape[1]
        )
        accum = _d8_flow_accum(d8, hw_raw)

        stream_pix = np.where(accum >= self.threshold, 1.0, -9999.0).astype(np.float32)
        stream_count = int((stream_pix == 1.0).sum())
        log.info(
            "FlowAccDEM: %d stream cells (threshold=%.1f)", stream_count, self.threshold
        )

        _lzw_profile = dict(
            compress="lzw", tiled=True, blockxsize=512, blockysize=512, BIGTIFF="YES"
        )

        fa_prof = profile.copy()
        fa_prof.update(dtype="float32", nodata=None, **_lzw_profile)
        with rasterio.open(str(self.out_flowaccum), "w", **fa_prof) as dst:
            dst.write(accum.astype(np.float32), 1)
        log.info("FlowAccDEM: flowaccum --> %s", self.out_flowaccum.name)

        sp_prof = profile.copy()
        sp_prof.update(dtype="float32", nodata=-9999.0, **_lzw_profile)
        self.out_stream_pixels.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(str(self.out_stream_pixels), "w", **sp_prof) as dst:
            dst.write(stream_pix, 1)
        log.info(
            "FlowAccDEM: stream pixels --> %s  (nodata=-9999)",
            self.out_stream_pixels.name,
        )

        return self.out_flowaccum, self.out_stream_pixels


def _d8_flow_accum(d8: np.ndarray, hw: np.ndarray) -> np.ndarray:
    """
    Topological BFS along WBT D8 flow directions.

    Propagates headwater weights downstream so each cell accumulates
    the count of headwater points in its contributing area.

    Parameters
    ----------
    d8 : integer raster of WBT D8 codes (0 = outlet / no-flow cell)
    hw : float32 headwater weights (1 at headwater points, 0 elsewhere)

    Returns
    -------
    accum : float32 accumulated headwater count at each cell
    """
    rows, cols = d8.shape
    n = rows * cols
    flat_d8 = d8.ravel()

    # build flat downstream index; self-loop marks outlet / no-flow
    ds = np.arange(n, dtype=np.int32)
    for code, (dr, dc) in _D8_OFFSETS.items():
        mask = flat_d8 == code
        if not mask.any():
            continue
        idxs = np.where(mask)[0]
        r = idxs // cols
        c = idxs % cols
        nr = r + dr
        nc = c + dc
        valid = (nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
        ds[idxs[valid]] = (nr[valid] * cols + nc[valid]).astype(np.int32)

    # in-degree: number of upstream cells draining into each cell
    non_self = ds != np.arange(n, dtype=np.int32)
    in_deg = np.zeros(n, dtype=np.int16)
    np.add.at(in_deg, ds[non_self], 1)

    # BFS from source cells (nothing flows into them)
    accum = hw.ravel().astype(np.float32)
    queue: deque[int] = deque(np.where(in_deg == 0)[0].tolist())
    while queue:
        i = queue.popleft()
        j = int(ds[i])
        if j != i:
            accum[j] += accum[i]
            in_deg[j] -= 1
            if in_deg[j] == 0:
                queue.append(j)

    return accum.reshape(rows, cols)
