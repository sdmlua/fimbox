"""
Author: Supath Dhital
Date Updated: May 2026

Lateral thalweg adjustment and stream flow conditioning.

Combines three steps:

  1. stream_pixel_zones
     Assigns a unique float ID to every stream pixel, then runs WBT
     euclidean_distance / euclidean_allocation to produce per-cell zone
     proximity and allocation grids used by step 2.

  2. adjust_thalweg_laterally
     For each thalweg cell, replaces its elevation with the lateral zonal
     minimum if that minimum is lower and within the elevation threshold.

  3. flow_condition_streams
     Masks the D8 pointer to stream cells only, then conditions the
     thalweg-adjusted DEM so elevation is monotonically decreasing along
     every flow path.

Inputs
--------------------------
dem             : dem_meters_{id}.tif
stream_pixels   : demDerived_streamPixels_{id}.tif  (1=stream, nodata=-9999)
flowdir         : flowdir_d8_burned_filled_{id}.tif (WBT d8_pointer)

Outputs
------------------------------
stream_pixel_ids : demDerived_streamPixels_ids_{id}.tif        (unique float IDs)
pixel_dist       : demDerived_streamPixels_ids_{id}_dist.tif   (proximity grid)
pixel_allo       : demDerived_streamPixels_ids_{id}_allo.tif   (allocation grid)
thalweg_adj      : dem_lateral_thalweg_adj_{id}.tif
flowdir_streams  : flowdir_d8_burned_filled_flows_{id}.tif     (D8 masked to streams)
thalweg_cond     : dem_thalwegCond_{id}.tif
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio

log = logging.getLogger(__name__)

# WBT D8 direction
_D8_OFFSETS: dict[int, tuple[int, int]] = {
    1: (0, 1),  # E
    2: (1, 1),  # SE
    4: (1, 0),  # S
    8: (1, -1),  # SW
    16: (0, -1),  # W
    32: (-1, -1),  # NW
    64: (-1, 0),  # N
    128: (-1, 1),  # NE
}


@dataclass
class ThalwegAdjustment:
    """
    Lateral thalweg conditioning & flow-direction conditioning for a single branch.

    Parameters
    ----------
    dem                       : dem_meters_{id}.tif
    stream_pixels             : demDerived_streamPixels_{id}.tif
    flowdir                   : flowdir_d8_burned_filled_{id}.tif
    out_thalweg_adj           : dem_lateral_thalweg_adj_{id}.tif
    out_flowdir_streams       : flowdir_d8_burned_filled_flows_{id}.tif
    out_thalweg_cond          : dem_thalwegCond_{id}.tif
    cost_distance_tolerance   : max distance (m) for lateral zone search (default 50)
    lateral_elevation_threshold: max elev difference (m) for lateral replacement (default 3)
    """

    dem: Path
    stream_pixels: Path
    flowdir: Path
    out_thalweg_adj: Path
    out_flowdir_streams: Path
    out_thalweg_cond: Path
    cost_distance_tolerance: float = 50.0
    lateral_elevation_threshold: int = 3
    wbt_path: Optional[str] = None

    def __post_init__(self):
        for attr in (
            "dem",
            "stream_pixels",
            "flowdir",
            "out_thalweg_adj",
            "out_flowdir_streams",
            "out_thalweg_cond",
        ):
            setattr(self, attr, Path(getattr(self, attr)))

    def run(self) -> dict[str, Path]:
        self.out_thalweg_adj.parent.mkdir(parents=True, exist_ok=True)

        log.info("ThalwegAdjustment start: branch dir=%s", self.out_thalweg_adj.parent)

        # unique pixel IDs with WBT proximity / allocation
        pixel_ids, dist_grid, allo_grid = self._stream_pixel_zones()
        log.info("  stream pixel zones --> %s  %s", dist_grid.name, allo_grid.name)

        # lateral thalweg adjustment
        self._adjust_thalweg(allo_grid, dist_grid)
        log.info("  thalweg adjusted   --> %s", self.out_thalweg_adj.name)

        # mask flowdir to stream cells with flow-condition DEM
        self._mask_and_condition()
        log.info("  thalweg conditioned --> %s", self.out_thalweg_cond.name)

        return {
            "stream_pixel_ids": pixel_ids,
            "pixel_dist": dist_grid,
            "pixel_allo": allo_grid,
            "thalweg_adj": self.out_thalweg_adj,
            "flowdir_streams": self.out_flowdir_streams,
            "thalweg_cond": self.out_thalweg_cond,
        }

    def _wbt(self):
        import whitebox

        wbt = whitebox.WhiteboxTools()
        wbt.set_verbose_mode(False)
        wbt_dir = self.wbt_path or os.environ.get("WBT_PATH")
        if wbt_dir:
            wbt.set_whitebox_dir(wbt_dir)
        return wbt

    def _stream_pixel_zones(self) -> tuple[Path, Path, Path]:
        """
        Port of unique_pixel_and_allocation.py.
        Returns (unique_ids_path, dist_path, allo_path).
        """
        wbt = self._wbt()
        out_dir = self.out_thalweg_adj.parent
        base = self.stream_pixels.stem  # demDerived_streamPixels_{id}

        pixel_ids = out_dir / f"{base}_ids.tif"
        dist_grid = out_dir / f"{base}_ids_dist.tif"
        allo_grid = out_dir / f"{base}_ids_allo.tif"

        with rasterio.open(str(self.stream_pixels)) as src:
            data = src.read(1)
            nodata_sp = src.nodata if src.nodata is not None else -9999.0
            profile = src.profile.copy()

        stream_mask = data == 1

        # Unique float64 values per stream pixel; background = 0
        unique_vals = np.arange(data.size, dtype=np.float64).reshape(data.shape)
        pixel_values = np.where(stream_mask, unique_vals, 0.0)

        uid_profile = profile.copy()
        uid_profile.update(
            dtype="float64",
            nodata=0.0,
            compress="lzw",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="YES",
        )
        with rasterio.open(str(pixel_ids), "w", **uid_profile) as dst:
            dst.write(pixel_values, 1)

        # WBT euclidean_distance on stream_pixels
        wbt.euclidean_distance(str(self.stream_pixels), str(dist_grid))
        _recompress_lzw(dist_grid)

        # WBT euclidean_allocation on unique values
        wbt.euclidean_allocation(str(pixel_ids), str(allo_grid))

        # Post-process allocation: fill stream cells with their own ID
        with rasterio.open(str(allo_grid)) as src:
            allo = src.read(1)
            allo_profile = src.profile.copy()

        allo = np.where(allo > 0, allo, pixel_values)
        allo_profile.update(
            compress="lzw", tiled=True, blockxsize=512, blockysize=512, BIGTIFF="YES"
        )
        with rasterio.open(str(allo_grid), "w", **allo_profile) as dst:
            dst.write(allo, 1)

        return pixel_ids, dist_grid, allo_grid

    def _adjust_thalweg(self, allo_grid: Path, dist_grid: Path) -> None:
        """
        Reads elevation, allocation, cost-distance in blocks and writes
        dem_lateral_thalweg_adj with LZW compression.
        """
        with (
            rasterio.open(str(self.dem)) as elev_ds,
            rasterio.open(str(allo_grid)) as allo_ds,
            rasterio.open(str(dist_grid)) as dist_ds,
            rasterio.open(str(self.stream_pixels)) as stream_ds,
        ):
            meta = elev_ds.meta.copy()
            meta.update(
                tiled=True,
                compress="lzw",
                blockxsize=512,
                blockysize=512,
                BIGTIFF="YES",
            )
            nodata = meta.get("nodata")
            tol = self.cost_distance_tolerance
            threshold = float(self.lateral_elevation_threshold)

            # build zone --> min-elevation dict
            zone_min: dict[int, float] = {}
            for _, window in elev_ds.block_windows(1):
                elev_w = elev_ds.read(1, window=window).ravel().astype(np.float32)
                allo_w = allo_ds.read(1, window=window).ravel()
                dist_w = dist_ds.read(1, window=window).ravel()

                valid = (dist_w <= tol) & (elev_w > 0) & np.isfinite(allo_w)
                if nodata is not None:
                    valid &= elev_w != nodata

                zones = allo_w[valid].astype(np.int64)
                elevs = elev_w[valid]
                for z, e in zip(zones, elevs):
                    if z not in zone_min or e < zone_min[z]:
                        zone_min[z] = float(e)

            # apply zone minimum to thalweg cells
            if zone_min:
                max_zone = max(zone_min.keys())
                lut = np.full(max_zone + 2, np.inf, dtype=np.float64)
                for z, m in zone_min.items():
                    if 0 <= z <= max_zone:
                        lut[z] = m
            else:
                lut = np.empty(0, dtype=np.float64)

            with rasterio.open(str(self.out_thalweg_adj), "w", **meta) as out_ds:
                for _, window in elev_ds.block_windows(1):
                    elev_w = elev_ds.read(1, window=window).astype(np.float32)
                    allo_w = allo_ds.read(1, window=window)
                    stream_w = stream_ds.read(1, window=window)

                    result = elev_w.copy()
                    thal_flat = stream_w.ravel() == 1

                    if lut.size > 0 and thal_flat.any():
                        zones = allo_w.ravel()[thal_flat].astype(np.int64)
                        elevs = elev_w.ravel()[thal_flat]
                        valid_z = (zones >= 0) & (zones < lut.size)
                        zone_mins = np.where(
                            valid_z, lut[np.minimum(zones, lut.size - 1)], np.inf
                        )
                        diff = elevs - zone_mins
                        should_update = (zone_mins < elevs) & (diff <= threshold)

                        result_flat = result.ravel()
                        thal_indices = np.where(thal_flat)[0]
                        result_flat[thal_indices[should_update]] = zone_mins[
                            should_update
                        ]
                        result = result_flat.reshape(elev_w.shape)

                    out_ds.write(result.astype(np.float32), window=window, indexes=1)

    # mask flowdir to stream cells with flow-condition DEM
    def _mask_and_condition(self) -> None:
        """
        Mask D8 flow directions to stream cells (A*B) then condition the
        thalweg-adjusted DEM to be monotonically decreasing along flow paths
        (TauDEM flowdircond equivalent).
        """
        with (
            rasterio.open(str(self.flowdir)) as fd_ds,
            rasterio.open(str(self.stream_pixels)) as sp_ds,
            rasterio.open(str(self.out_thalweg_adj)) as adj_ds,
        ):
            d8 = fd_ds.read(1)
            nodata_d8 = fd_ds.nodata
            sp = sp_ds.read(1)
            elev = adj_ds.read(1).astype(np.float64)
            nodata_elev = adj_ds.nodata
            profile = adj_ds.profile.copy()

        # Mask D8 to stream cells only (zero out nodata first, matching gdal_calc A*B)
        if nodata_d8 is not None:
            d8 = np.where(d8 == int(nodata_d8), 0, d8)
        d8_streams = np.where(sp == 1, d8, 0).astype(np.int32)

        fd_profile = rasterio.open(str(self.flowdir)).profile.copy()
        fd_profile.update(
            dtype="int32",
            nodata=0,
            compress="lzw",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="YES",
        )
        with rasterio.open(str(self.out_flowdir_streams), "w", **fd_profile) as dst:
            dst.write(d8_streams.astype(np.int32), 1)

        # Flow condition: ensure elevation is strictly decreasing along flow paths
        elev_cond = _flow_condition(d8_streams, elev, nodata=nodata_elev)

        profile.update(
            dtype="float32",
            compress="lzw",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="YES",
        )
        with rasterio.open(str(self.out_thalweg_cond), "w", **profile) as dst:
            dst.write(elev_cond.astype(np.float32), 1)


def _recompress_lzw(path: Path) -> None:
    """Rewrite a raster in-place with LZW compression."""
    import shutil

    if not path.exists():
        return
    tmp = path.with_suffix(".tmp.tif")
    try:
        with rasterio.open(str(path)) as src:
            profile = src.profile.copy()
            profile.update(
                compress="lzw",
                tiled=True,
                blockxsize=512,
                blockysize=512,
                BIGTIFF="YES",
            )
            data = src.read(1)
        with rasterio.open(str(tmp), "w", **profile) as dst:
            dst.write(data, 1)
        shutil.move(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            tmp.unlink()


def _flow_condition(
    d8_streams: np.ndarray,
    elev: np.ndarray,
    nodata: Optional[float] = None,
    eps: float = 1e-4,
) -> np.ndarray:
    """
    Processes cells sorted by elevation descending (headwaters first),
    which approximates topological order without building a graph.
    """
    rows, cols = elev.shape
    n = rows * cols
    flat_d8 = d8_streams.ravel()
    flat_elev = elev.ravel().copy()

    # Build downstream flat-index lookup
    flat_ds = np.arange(n, dtype=np.int64)  # default: self (outlet)
    r_all = np.arange(n) // cols
    c_all = np.arange(n) % cols

    for d8_val, (dr, dc) in _D8_OFFSETS.items():
        sel = flat_d8 == d8_val
        if not sel.any():
            continue
        r_ds = r_all + dr
        c_ds = c_all + dc
        in_bounds = sel & (r_ds >= 0) & (r_ds < rows) & (c_ds >= 0) & (c_ds < cols)
        flat_ds[in_bounds] = (r_ds * cols + c_ds)[in_bounds]

    # Stream cells only
    stream_idxs = np.where(flat_d8 > 0)[0]
    if stream_idxs.size == 0:
        return elev

    # Sort descending by elevation: highest (headwaters) first
    sort_order = np.argsort(flat_elev[stream_idxs])[::-1]

    for idx in stream_idxs[sort_order]:
        ds_idx = flat_ds[idx]
        if ds_idx == idx:
            continue  # outlet cell
        if flat_elev[idx] <= flat_elev[ds_idx]:
            flat_elev[idx] = flat_elev[ds_idx] + eps

    return flat_elev.reshape(rows, cols)
