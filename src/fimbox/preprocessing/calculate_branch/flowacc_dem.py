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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from numba import njit

from ._d8 import downstream_index

log = logging.getLogger(__name__)


@dataclass
class FlowAccDEM:
    """
    Headwater-weighted D8 flow accumulation using a topological BFS.

    Hand-rolled on numpy + a compiled traversal rather than pulling in pyflwdir.

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
    # Rasterized level-path network (flows_grid_boolean_{id}.tif). Used as a
    # fallback seed when the single headwater point fails to produce a network
    # (e.g. it rasterized into a nodata pocket away from the flow grid).
    stream_raster: Optional[Path] = None

    def __post_init__(self):
        for attr in ("flowdir", "headwaters", "out_flowaccum", "out_stream_pixels"):
            setattr(self, attr, Path(getattr(self, attr)))
        if self.stream_raster is not None:
            self.stream_raster = Path(self.stream_raster)
        self.out_flowaccum.parent.mkdir(parents=True, exist_ok=True)

    def run(self) -> tuple[Path, Path]:
        import rasterio

        log.info("FlowAccDEM: reading D8 flow direction --> %s", self.flowdir.name)
        with rasterio.open(str(self.flowdir)) as src:
            profile = src.profile.copy()
            d8_raw = src.read(1)
            nodata_d8 = src.nodata
            d8_transform = src.transform
            d8_crs = src.crs

        log.info("FlowAccDEM: reading headwaters raster --> %s", self.headwaters.name)
        with rasterio.open(str(self.headwaters)) as src:
            hw_raw = src.read(1).astype(np.float32)
            nodata_hw = src.nodata
            hw_transform = src.transform
            hw_crs = src.crs

        if nodata_hw is not None:
            hw_raw[hw_raw == nodata_hw] = 0.0
        # WBT nodata D8 cells -> outlets (no downstream)
        d8 = d8_raw.copy()
        if nodata_d8 is not None:
            d8[d8_raw == nodata_d8] = 0

        # Headwaters and flowdir are clipped separately, so their grids can drift
        # by a row/col; align the seeds onto the flowdir grid before accumulating.
        if hw_raw.shape != d8.shape or hw_transform != d8_transform:
            from rasterio.warp import Resampling, reproject

            aligned = np.zeros(d8.shape, dtype=np.float32)
            reproject(
                source=hw_raw,
                destination=aligned,
                src_transform=hw_transform,
                src_crs=hw_crs,
                dst_transform=d8_transform,
                dst_crs=d8_crs,
                resampling=Resampling.max,
            )
            log.info("FlowAccDEM: aligned headwaters %s -> %s", hw_raw.shape, d8.shape)
            hw_raw = aligned

        # Snap seeds off non-flowing cells onto the nearest flowing cell.
        hw_raw = _snap_seeds_to_network(hw_raw, d8)

        log.info(
            "FlowAccDEM: topological BFS on %d × %d grid", d8.shape[0], d8.shape[1]
        )
        accum = _d8_flow_accum(d8, hw_raw)

        accum, stream_count = self._stream_count(accum)
        log.info(
            "FlowAccDEM: %d stream cells (threshold=%.1f)", stream_count, self.threshold
        )

        # The lone headwater point can rasterize into a nodata pocket far from
        # the flow grid, yielding ~0 cells. Re-seed from the full rasterized
        # level-path network so the real channel is always captured.
        if stream_count <= 1 and self.stream_raster and self.stream_raster.is_file():
            with rasterio.open(str(self.stream_raster)) as src:
                sr = src.read(1)
                sr_nodata = src.nodata
            seeds = (sr > 0) if sr_nodata is None else ((sr > 0) & (sr != sr_nodata))
            if seeds.shape == d8.shape and seeds.any():
                accum = _d8_flow_accum(d8, seeds.astype(np.float32))
                accum, stream_count = self._stream_count(accum)
                log.info(
                    "FlowAccDEM: re-seeded from level-path network "
                    "(%d seed cells) -> %d stream cells",
                    int(seeds.sum()),
                    stream_count,
                )

        stream_pix = np.where(accum >= self.threshold, 1.0, -9999.0).astype(np.float32)

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

    def _stream_count(self, accum: np.ndarray) -> tuple[np.ndarray, int]:
        # Cells at/above the threshold are stream cells.
        return accum, int((accum >= self.threshold).sum())


def _snap_seeds_to_network(
    hw: np.ndarray, d8: np.ndarray, radius: int = 3
) -> np.ndarray:
    """Move headwater seeds that landed on a non-flowing cell (D8 code 0) onto
    the nearest flowing cell within ``radius``, so the weight enters the D8
    network. Seeds already on a flowing cell are left untouched."""
    seed_rc = np.argwhere(hw > 0)
    if seed_rc.size == 0:
        return hw
    rows, cols = d8.shape
    out = hw.copy()
    for r, c in seed_rc:
        if d8[r, c] != 0:
            continue  # already on a flowing cell
        best = None
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and d8[nr, nc] != 0:
                    dist = dr * dr + dc * dc
                    if best is None or dist < best[0]:
                        best = (dist, nr, nc)
        if best is not None:
            out[r, c] = 0.0
            out[best[1], best[2]] += hw[r, c]
    return out


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

    accum = hw.ravel().astype(np.float32).copy()
    _accumulate_downstream(downstream_index(d8), accum)
    return accum.reshape(rows, cols)


@njit(cache=True)
def _accumulate_downstream(ds: np.ndarray, accum: np.ndarray) -> None:
    """Push each cell's running total into its downstream neighbour, Kahn order.

    In-degree and the source list are counted here rather than with bincount so
    nothing wider than the grid itself gets allocated — that temporary is over a
    gigabyte on a HUC8 and grows with the area.

    A cell is released only once every upstream contributor has been added in, so
    each is touched exactly once, and each joins the queue exactly when its
    in-degree hits zero — so the preallocated queue can never overflow.
    """
    n = ds.size

    in_deg = np.zeros(n, dtype=np.int16)
    for i in range(n):
        j = ds[i]
        if j != i:
            in_deg[j] += 1

    queue = np.empty(n, dtype=np.int32)
    tail = 0
    for i in range(n):
        if in_deg[i] == 0:
            queue[tail] = i
            tail += 1

    head = 0
    while head < tail:
        i = queue[head]
        head += 1
        j = ds[i]
        if j != i:
            accum[j] += accum[i]
            in_deg[j] -= 1
            if in_deg[j] == 0:
                queue[tail] = j
                tail += 1
