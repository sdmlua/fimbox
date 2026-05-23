"""
Author: Supath Dhital
Date Updated: May 2026

Combine per-branch inundation rasters into AOI-level depth and extent
rasters. Non-zero branches take precedence over branch 0 where they
exist — the same priority rule used by the production NOAA pipeline.

Strategy
--------
1. Lay down branch 0 as the base (it covers the whole AOI but at coarser
   main-stem accuracy).
2. For every non-zero branch, overlay its wet pixels (depth > 0) on top
   of the base. Where the non-zero branch has data, it wins. Where it
   has no data (outside its level path), the branch 0 value remains.
3. Write the result as a single AOI-wide GeoTIFF.

The implementation streams windows, not whole rasters, so very large
AOIs don't blow up memory. All inputs must share the AOI grid (they do
by construction — CreateHAND writes every branch off the same boundary
DEM).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import numpy as np
import rasterio
from rasterio.merge import merge as rasterio_merge

PathLike = Union[str, Path]

log = logging.getLogger(__name__)


@dataclass
class MosaicResult:
    depth_path: Path
    extent_path: Path
    n_branches_merged: int       # zero counted, plus every non-zero that had data
    n_wet_pixels: int
    max_depth_m: float


@dataclass
class BranchMosaic:
    aoi_dir: PathLike

    # Output file paths inside aoi_dir. Default names match production.
    depth_out: Optional[PathLike] = None
    extent_out: Optional[PathLike] = None

    # Limit to a specific branch subset. Default: walk every branch under
    # <aoi_dir>/branches.
    branch_ids: Optional[Sequence[str]] = None
    branch_zero_id: str = "0"

    # When set, read per-branch inundation rasters from this directory
    # (named inundation_{depth,extent}_<bid>.tif) instead of the standard
    # <aoi_dir>/branches/<bid>/ location. Used by FimGenerator to keep
    # intermediates in a tmp dir that gets cleaned up after the mosaic.
    sources_dir: Optional[PathLike] = None

    def __post_init__(self) -> None:
        self.aoi_dir = Path(self.aoi_dir)
        if self.depth_out is None:
            self.depth_out = self.aoi_dir / "inundation_depth.tif"
        if self.extent_out is None:
            self.extent_out = self.aoi_dir / "inundation_extent.tif"
        self.depth_out = Path(self.depth_out)
        self.extent_out = Path(self.extent_out)

    def run(self) -> MosaicResult:
        if self.sources_dir is not None:
            src_root = Path(self.sources_dir)
            if not src_root.is_dir():
                raise NotADirectoryError(f"sources_dir does not exist: {src_root}")
            # Per-branch outputs all live as a flat set of files in src_root.
            bids = self._collect_branches_from_flat(src_root)
        else:
            src_root = self.aoi_dir / "branches"
            if not src_root.is_dir():
                raise NotADirectoryError(f"No branches dir under {self.aoi_dir}")
            bids = self._collect_branches(src_root)

        if not bids:
            raise FileNotFoundError(
                "No branch inundation rasters found — run Inundator first"
            )

        # Branch zero is laid down first; everything else overlays on top.
        # If branch 0 is missing we still proceed.
        ordered = [b for b in (self.branch_zero_id,) if b in bids]
        ordered += [b for b in bids if b != self.branch_zero_id]

        if self.sources_dir is not None:
            depth_files = [src_root / f"inundation_depth_{b}.tif" for b in ordered]
            extent_files = [src_root / f"inundation_extent_{b}.tif" for b in ordered]
        else:
            depth_files = [src_root / b / f"inundation_depth_{b}.tif" for b in ordered]
            extent_files = [src_root / b / f"inundation_extent_{b}.tif" for b in ordered]
        depth_files = [p for p in depth_files if p.is_file()]
        extent_files = [p for p in extent_files if p.is_file()]

        if not depth_files:
            raise FileNotFoundError("No inundation_depth_*.tif rasters found")

        self._mosaic(depth_files, self.depth_out, method="max_positive")
        self._mosaic(extent_files, self.extent_out, method="last_wins")

        n_wet, max_d = self._summarise(self.depth_out)
        log.info(
            f"BranchMosaic: merged {len(depth_files)} branches, "
            f"{n_wet} wet pixels, max depth {max_d:.2f} m"
        )
        return MosaicResult(
            depth_path=self.depth_out,
            extent_path=self.extent_out,
            n_branches_merged=len(depth_files),
            n_wet_pixels=n_wet,
            max_depth_m=max_d,
        )

    def _collect_branches(self, branch_root: Path) -> list[str]:
        if self.branch_ids is not None:
            return list(self.branch_ids)
        return sorted(p.name for p in branch_root.iterdir() if p.is_dir())

    def _collect_branches_from_flat(self, src_root: Path) -> list[str]:
        # When sources_dir is set, every per-branch file lives as a flat
        # inundation_depth_<bid>.tif. Parse the branch id back out of the
        # filename.
        if self.branch_ids is not None:
            return list(self.branch_ids)
        bids = set()
        for p in src_root.glob("inundation_depth_*.tif"):
            stem = p.stem  # inundation_depth_<bid>
            bid = stem[len("inundation_depth_"):]
            if bid:
                bids.add(bid)
        return sorted(bids)

    def _mosaic(
        self, sources: list[Path], out_path: Path, method: str
    ) -> None:
        # method='max_positive': pick the largest positive value per pixel
        #     (used for depth — overlapping flood predictions take the
        #     deepest, dry values lose to wet).
        # method='last_wins': later source overrides earlier (used for
        #     the signed-HydroID extent raster — the non-zero branch's
        #     HydroID wins over branch 0's where both are wet).
        if not sources:
            return
        if out_path.exists():
            out_path.unlink()

        if method == "max_positive":
            self._merge_max(sources, out_path)
        elif method == "last_wins":
            self._merge_last(sources, out_path)
        else:
            raise ValueError(method)

    def _merge_max(self, sources: list[Path], out_path: Path) -> None:
        # rasterio.merge supports a "max" method already, but it doesn't
        # ignore zero/nodata cleanly. Custom callable picks the max where
        # new pixels are positive, else keeps the existing value.
        def _max_positive(old_data, new_data, old_nodata, new_nodata, **_):
            new_arr = new_data
            new_valid = new_arr > 0
            new_valid &= _not_nodata_mask(new_arr, new_nodata)
            np.copyto(
                old_data,
                np.maximum(old_data, new_arr),
                where=new_valid & (new_arr > old_data),
            )

        rasterio_merge(
            [str(p) for p in sources],
            method=_max_positive,
            dst_path=str(out_path),
        )

    def _merge_last(self, sources: list[Path], out_path: Path) -> None:
        # Signed HydroIDs. Any non-zero value from a later source
        # overwrites the earlier mosaic.
        def _last_wins(old_data, new_data, old_nodata, new_nodata, **_):
            new_arr = new_data
            new_valid = new_arr != 0
            new_valid &= _not_nodata_mask(new_arr, new_nodata)
            np.copyto(old_data, new_arr, where=new_valid)

        rasterio_merge(
            [str(p) for p in sources],
            method=_last_wins,
            dst_path=str(out_path),
        )

    @staticmethod
    def _summarise(depth_path: Path) -> tuple[int, float]:  # noqa: D401
        return _summarise_depth(depth_path)


def _not_nodata_mask(arr: np.ndarray, nodata) -> np.ndarray:
    # Build a "this pixel is real data" mask. nodata can come in as a
    # scalar, a per-band sequence, or None. NaN is also treated as nodata.
    mask = ~np.isnan(arr) if np.issubdtype(arr.dtype, np.floating) else np.ones_like(arr, dtype=bool)
    if nodata is None:
        return mask
    # Reduce per-band nodata down to a single scalar — rasterio sometimes
    # hands us a 1-element sequence even for single-band rasters.
    nd_values = np.atleast_1d(nodata).ravel()
    for nd in nd_values:
        if nd is None:
            continue
        try:
            nd_f = float(nd)
        except (TypeError, ValueError):
            continue
        if np.isnan(nd_f):
            continue  # NaN handled by the isnan mask above
        mask &= (arr != nd_f)
    return mask


def _summarise_depth(depth_path: Path) -> tuple[int, float]:
    # Quick block-wise scan for wet pixel count and max depth.
    n_wet = 0
    max_d = 0.0
    with rasterio.open(depth_path) as ds:
        for _, w in ds.block_windows(1):
            arr = ds.read(1, window=w)
            wet = arr > 0
            if wet.any():
                n_wet += int(wet.sum())
                block_max = float(arr[wet].max())
                if block_max > max_d:
                    max_d = block_max
    return n_wet, max_d
