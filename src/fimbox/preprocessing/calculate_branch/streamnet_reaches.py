"""
Author: Supath Dhital
Date Updated May 2026

Stream network delineation.

Inputs
------
flowdir         : flowdir_d8_burned_filled_{id}.tif
dem_thalweg_cond: dem_thalwegCond_{id}.tif
flowaccum       : flowaccum_d8_burned_filled_{id}.tif
stream_pixels   : demDerived_streamPixels_{id}.tif

Outputs:
----------------------------------------------
streamOrder_{id}.tif           – Strahler stream order raster
sn_catchments_reaches_{id}.tif – per-reach catchment raster (reach IDs)
demDerived_reaches_{id}.gpkg   – vectorised stream network (polylines,
                                  each spanning headwater-to-confluence or
                                  confluence-to-confluence / outlet)
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# WBT D8 pointer: power-of-2 code --> (row_offset, col_offset)
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
class StreamNetReaches:
    """
    Delineate stream network reach topology — pure-numpy streamnet.

    Each reach spans headwater-to-confluence or confluence-to-confluence
    (or confluence-to-outlet), matching TauDEM streamnet behaviour.
    Avoids WBT stream_link_identifier which creates one ID per D8 step,
    producing millions of 1-3-pixel reaches on dense stream networks.

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
        for attr in (
            "flowdir",
            "dem_thalweg_cond",
            "flowaccum",
            "stream_pixels",
            "out_dir",
        ):
            setattr(self, attr, Path(getattr(self, attr)))
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict[str, Path]:
        import rasterio
        import geopandas as gpd
        from shapely.geometry import LineString

        bid = self.branch_id
        stream_order_path = self.out_dir / f"streamOrder_{bid}.tif"
        sn_catchments = self.out_dir / f"sn_catchments_reaches_{bid}.tif"
        reaches_gpkg = self.out_dir / f"demDerived_reaches_{bid}.gpkg"

        def _valid(p: Path) -> bool:
            return p.exists() and p.stat().st_size > 0

        if _valid(stream_order_path) and _valid(sn_catchments) and _valid(reaches_gpkg):
            log.info("StreamNetReaches: outputs exist, skipping branch %s", bid)
            return {
                "stream_order": stream_order_path,
                "sn_catchments_reaches": sn_catchments,
                "demDerived_reaches": reaches_gpkg,
            }

        with rasterio.open(str(self.flowdir)) as src:
            d8_raw = src.read(1).astype(np.int32)
            nodata_d8 = src.nodata
            profile = src.profile.copy()
            transform = src.transform
            crs = src.crs

        with rasterio.open(str(self.stream_pixels)) as src:
            stream_mask = src.read(1) == 1

        rows, cols = d8_raw.shape
        n = rows * cols

        d8 = d8_raw.copy()
        if nodata_d8 is not None:
            d8[d8 == int(nodata_d8)] = 0

        # ── Build downstream flat-index (self-loop = outlet / nodata) ──────────
        flat_d8 = d8.ravel()
        ds = np.arange(n, dtype=np.int64)
        r_all = np.arange(n, dtype=np.int64) // cols
        c_all = np.arange(n, dtype=np.int64) % cols

        for d8_val, (dr, dc) in _D8_OFFSETS.items():
            sel = flat_d8 == d8_val
            if not sel.any():
                continue
            r_ds = r_all + dr
            c_ds = c_all + dc
            valid = sel & (r_ds >= 0) & (r_ds < rows) & (c_ds >= 0) & (c_ds < cols)
            ds[valid] = (r_ds * cols + c_ds)[valid]

        # ── Count stream-cell in-degrees (fully vectorised) ─────────────────────
        stream_flat = stream_mask.ravel()
        stream_idx = np.where(stream_flat)[0].astype(np.int64)

        # downstream index of each stream cell
        ds_stream = ds[stream_idx]  # downstream flat-idx
        non_self = ds_stream != stream_idx  # exclude self-loops (outlets)
        # only count when the downstream cell is also a stream cell
        ds_is_stream = stream_flat[ds_stream]
        valid = non_self & ds_is_stream
        in_deg = np.zeros(n, dtype=np.int32)
        np.add.at(in_deg, ds_stream[valid], 1)

        log.info(
            "StreamNet: %d stream cells, %d headwaters, %d junctions",
            stream_idx.size,
            int((in_deg[stream_idx] == 0).sum()),
            int((in_deg[stream_idx] >= 2).sum()),
        )

        # ── Assign reach IDs in topological (upstream-->downstream) order ─────────
        # Rules:
        #   • headwater cell (in_deg == 0) --> start reach R
        #   • single-upstream cell (in_deg == 1) --> continue reach from upstream
        #   • junction cell (in_deg >= 2) --> end all incoming reaches HERE;
        #     the junction cell itself starts a NEW reach going downstream
        #
        # We process cells in topological order using a BFS queue seeded from
        # headwaters, decrementing in-degree as we go.

        reach_of: np.ndarray = np.zeros(n, dtype=np.int32)  # cell --> reach ID
        next_rid = 1

        # queue holds (cell_idx, reach_id_to_continue)
        # reach_id_to_continue == 0 means "start a new reach at this cell"
        pq: deque[tuple[int, int]] = deque()

        # Seed headwaters
        for i in stream_idx:
            if in_deg[i] == 0:
                pq.append((int(i), 0))  # 0 --> allocate new reach

        remaining_in = in_deg.copy()

        while pq:
            i, incoming_rid = pq.popleft()

            if reach_of[i] != 0:
                # Already assigned — skip (can happen when junction receives
                # multiple predecessors and is enqueued twice)
                continue

            # Assign reach ID to this cell
            if incoming_rid == 0:
                rid = next_rid
                next_rid += 1
            else:
                rid = incoming_rid
            reach_of[i] = rid

            # Move to downstream cell
            j = int(ds[i])
            if j == i or not stream_flat[j]:
                # Outlet or leaves stream — reach ends here
                continue

            remaining_in[j] -= 1

            if remaining_in[j] > 0:
                # Other upstream cells haven't been processed yet — wait
                continue

            # All upstream done: decide what reach continues into j
            if in_deg[j] >= 2:
                # j is a junction — it starts a NEW reach
                pq.append((j, 0))
            else:
                # j has exactly one upstream (this cell) — continue same reach
                pq.append((j, rid))

        # ── Vectorise: build one LineString per reach ───────────────────────────
        # Group cells by reach ID
        assigned = np.where(reach_of > 0)[0]
        rids = reach_of[assigned]
        sort_ord = np.argsort(rids, kind="stable")
        assigned_sorted = assigned[sort_ord]
        rids_sorted = rids[sort_ord]
        split_pts = np.flatnonzero(np.diff(rids_sorted)) + 1
        bounds = np.concatenate([[0], split_pts, [len(rids_sorted)]])
        groups = np.split(assigned_sorted, split_pts)
        unique_rids = rids_sorted[bounds[:-1]]

        def cell_xy(idx: int) -> tuple[float, float]:
            r = int(idx) // cols
            c = int(idx) % cols
            x = transform[2] + (c + 0.5) * transform[0]
            y = transform[5] + (r + 0.5) * transform[4]
            return (x, y)

        reach_geoms = []
        reach_ids_out = []

        pixel_size = abs(transform[0])  # metres per pixel

        for k, rid in enumerate(unique_rids):
            cells = groups[k]
            if len(cells) < 2:
                continue
            # Find head: cell NOT pointed-to by any other cell in this reach
            ds_of_cells = ds[cells]
            cells_set_np = set(cells.tolist())
            internal_dst = ds_of_cells[np.isin(ds_of_cells, cells)]
            pointed_to = set(internal_dst.tolist())
            heads = [c for c in cells.tolist() if c not in pointed_to]
            if not heads:
                heads = [int(cells[0])]
            # Trace downstream from head through this reach
            path = []
            cur = heads[0]
            seen: set[int] = set()
            while cur in cells_set_np and cur not in seen:
                path.append(cur)
                seen.add(cur)
                nxt = int(ds[cur])
                if nxt == cur:
                    break
                cur = nxt
            if len(path) < 2:
                continue

            # Extend path by ONE extra cell (the junction / outlet cell that
            # this reach terminates at). Without this the reach end sits at the
            # centre of the last *owned* cell, one pixel short of where the next
            # reach starts — producing a 1-pixel gap at every confluence.
            tail = path[-1]
            nxt = int(ds[tail])
            if nxt != tail:  # not a self-loop outlet
                path.append(nxt)

            # Build coords and simplify to remove the D8 staircase effect.
            # RDP with tolerance = 0.5 pixel collapses diagonal stair-steps into
            # straight diagonals while preserving all true bends.
            coords = [cell_xy(c) for c in path]
            line = LineString(coords).simplify(
                pixel_size * 0.5, preserve_topology=False
            )
            if line.is_empty or line.geom_type != "LineString":
                line = LineString(coords)
            reach_geoms.append(line)
            reach_ids_out.append(int(rid))

        log.info("StreamNet: %d reaches vectorised", len(reach_geoms))

        # ── Write sn_catchments raster ──────────────────────────────────────────
        sn_profile = profile.copy()
        sn_profile.update(
            dtype="int32",
            nodata=0,
            compress="lzw",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="YES",
        )
        with rasterio.open(str(sn_catchments), "w", **sn_profile) as dst:
            dst.write(reach_of.reshape(rows, cols).astype(np.int32), 1)
        log.info("StreamNet: reach ID raster --> %s", sn_catchments.name)

        # ── Strahler stream order via WBT ───────────────────────────────────────
        log.info("StreamNet: Strahler order --> %s", stream_order_path.name)
        try:
            wbt = self._wbt()
            wbt.strahler_stream_order(
                str(self.flowdir),
                str(self.stream_pixels),
                str(stream_order_path),
            )
            _recompress_lzw(stream_order_path)
        except Exception as exc:
            log.warning("StreamNet: WBT Strahler order failed (%s), writing zeros", exc)
            so_profile = profile.copy()
            so_profile.update(
                dtype="int32",
                nodata=0,
                compress="lzw",
                tiled=True,
                blockxsize=512,
                blockysize=512,
                BIGTIFF="YES",
            )
            with rasterio.open(str(stream_order_path), "w", **so_profile) as dst:
                dst.write(np.zeros((rows, cols), dtype=np.int32), 1)

        # ── Write GeoPackage ────────────────────────────────────────────────────
        gdf = gpd.GeoDataFrame(
            {"STRM_VAL": reach_ids_out},
            geometry=reach_geoms,
            crs=crs,
        )
        if reaches_gpkg.exists():
            reaches_gpkg.unlink()
        gdf.to_file(str(reaches_gpkg), driver="GPKG", index=False, engine="fiona")
        log.info(
            "StreamNet: reaches --> %s  (%d features)", reaches_gpkg.name, len(gdf)
        )

        log.info("StreamNetReaches done for branch %s", bid)
        return {
            "stream_order": stream_order_path,
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
        wbt.set_working_dir(str(Path(self.out_dir)))
        return wbt


def _recompress_lzw(path: Path) -> None:
    import rasterio
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
