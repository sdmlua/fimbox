"""
Author: Supath Dhital
Date Updated: May 2026

Per-branch flood inundation generator.

Given:
  - a per-branch HAND raster (rem_zeroed_masked_{bid}.tif)
  - a per-branch HydroID catchment raster
    (gw_catchments_reaches_filtered_addedAttributes_{bid}.tif)
  - a per-branch rating curve table (hydroTable_{bid}.csv)
  - a forecast dataframe of (feature_id, discharge_cms)

produce two rasters in the branch directory:
  - inundation_extent_{bid}.tif   uint16 / int16:  +HydroID where wet, -HydroID where dry,
                                                 0/nodata elsewhere
  - inundation_depth_{bid}.tif    float32: water depth in meters where wet, 0 elsewhere
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd
import rasterio

PathLike = Union[str, Path]

log = logging.getLogger(__name__)


class NoForecastMatch(Exception):
    # Raised when a hydroTable shares no feature_ids with the forecast.
    pass


@dataclass
class InundationResult:
    branch_id: str
    extent_path: Path
    depth_path: Path
    n_hydroids_wet: int       # how many HydroIDs ended up with stage > 0
    n_pixels_wet: int         # how many DEM pixels are flooded
    max_depth_m: float        # max water depth across the raster
    skipped: bool = False     # True when the branch had no input data


@dataclass
class Inundator:
    branch_dir: PathLike
    branch_id: str
    forecast: Union[PathLike, pd.DataFrame]
    hand_raster: Optional[PathLike] = None
    catchments_raster: Optional[PathLike] = None
    hydrotable_csv: Optional[PathLike] = None
    out_dir: Optional[PathLike] = None
    extent_out: Optional[PathLike] = None
    depth_out: Optional[PathLike] = None

    # Smallest depth (m) that counts as wet. ~30 mm 
    min_depth_m: float = 0.03

    #When true, written as a millimeter
    int16_mode: bool = False

    # When True, skip lake reaches (LakeID != -999). Set to False to include
    # lakes in the inundation map.
    drop_lakes: bool = True

    def __post_init__(self) -> None:
        self.branch_dir = Path(self.branch_dir)
        bid = self.branch_id
        if self.hand_raster is None:
            self.hand_raster = self.branch_dir / f"rem_zeroed_masked_{bid}.tif"
        if self.catchments_raster is None:
            self.catchments_raster = (
                self.branch_dir
                / f"gw_catchments_reaches_filtered_addedAttributes_{bid}.tif"
            )
        if self.hydrotable_csv is None:
            self.hydrotable_csv = self.branch_dir / f"hydroTable_{bid}.csv"

        # Output directory: explicit out_dir wins; otherwise outputs go alongside the input rasters in branch_dir.
        write_dir = Path(self.out_dir) if self.out_dir is not None else self.branch_dir
        write_dir.mkdir(parents=True, exist_ok=True)
        if self.extent_out is None:
            self.extent_out = write_dir / f"inundation_extent_{bid}.tif"
        if self.depth_out is None:
            self.depth_out = write_dir / f"inundation_depth_{bid}.tif"

        for attr in (
            "hand_raster", "catchments_raster", "hydrotable_csv",
            "extent_out", "depth_out",
        ):
            setattr(self, attr, Path(getattr(self, attr)))

    def run(self) -> InundationResult:
        bid = self.branch_id
        # Quietly skip when a branch never produced HAND outputs (e.g. killed during BranchZero). The caller mosaicker treats this
        # as an absent branch, not a fatal error.
        for p in (self.hand_raster, self.catchments_raster, self.hydrotable_csv):
            if not p.is_file():
                log.warning(
                    f"Inundator: branch {bid} missing {p.name} — skipping"
                )
                return InundationResult(
                    branch_id=bid,
                    extent_path=self.extent_out,
                    depth_path=self.depth_out,
                    n_hydroids_wet=0, n_pixels_wet=0, max_depth_m=0.0,
                    skipped=True,
                )

        forecast = self._load_forecast()
        stage_by_hydroid = self._build_stage_lookup(forecast)
        if not stage_by_hydroid:
            log.warning(
                f"Inundator: branch {bid} has no HydroIDs with matching forecast"
            )
            return InundationResult(
                branch_id=bid,
                extent_path=self.extent_out,
                depth_path=self.depth_out,
                n_hydroids_wet=0, n_pixels_wet=0, max_depth_m=0.0,
                skipped=True,
            )

        return self._write_rasters(stage_by_hydroid)

    def _load_forecast(self) -> pd.DataFrame:
        f = self.forecast
        if isinstance(f, (str, Path)):
            df = pd.read_csv(f)
        elif isinstance(f, pd.DataFrame):
            df = f.copy()
        else:
            raise TypeError(
                "forecast must be a path to a CSV or a pandas DataFrame"
            )

        # Accept the common column-name variants.
        if "discharge_cms" not in df.columns:
            for alias in ("discharge", "flow_cms", "flow"):
                if alias in df.columns:
                    df = df.rename(columns={alias: "discharge_cms"})
                    break
        if "feature_id" not in df.columns or "discharge_cms" not in df.columns:
            raise ValueError(
                "forecast must contain 'feature_id' and 'discharge_cms' columns"
            )

        df["feature_id"] = df["feature_id"].astype(np.int64)
        df["discharge_cms"] = df["discharge_cms"].astype(np.float64)
        df = df.groupby("feature_id", as_index=False)["discharge_cms"].max()
        return df

    def _build_stage_lookup(
        self, forecast: pd.DataFrame
    ) -> dict[int, float]:
        ht_cols_needed = ["feature_id", "HydroID", "stage", "discharge_cms"]
        ht_optional = ["LakeID"]
        header = pd.read_csv(self.hydrotable_csv, nrows=0).columns
        usecols = [c for c in ht_cols_needed + ht_optional if c in header]
        ht = pd.read_csv(
            self.hydrotable_csv,
            usecols=usecols,
            dtype={
                "feature_id": np.int64,
                "HydroID": np.int64,
                "stage": np.float64,
                "discharge_cms": np.float64,
                "LakeID": np.int64,
            },
        )
        if "feature_id" not in ht.columns or "HydroID" not in ht.columns:
            raise ValueError(
                f"hydroTable {self.hydrotable_csv.name} missing feature_id or HydroID"
            )
        if self.drop_lakes and "LakeID" in ht.columns:
            ht = ht[ht["LakeID"] == -999]

        forecast_local = forecast.rename(columns={"discharge_cms": "forecast_q"})
        merged = ht.merge(forecast_local, on="feature_id", how="inner")
        if merged.empty:
            return {}

        # Per-HydroID stage interpolation. xs must be ascending for np.interp.
        stage_by_hydroid: dict[int, float] = {}
        for hid, sub in merged.groupby("HydroID", sort=False):
            sub = sub.sort_values("discharge_cms")
            xs = sub["discharge_cms"].to_numpy()
            ys = sub["stage"].to_numpy()
            q = float(sub["forecast_q"].iloc[0])
            interpolated = float(np.interp(q, xs, ys))
            stage_by_hydroid[int(hid)] = max(0.0, interpolated)
        return stage_by_hydroid

    def _write_rasters(
        self, stage_by_hydroid: dict[int, float]
    ) -> InundationResult:
        bid = self.branch_id

        with (
            rasterio.open(self.hand_raster) as hand_ds,
            rasterio.open(self.catchments_raster) as cat_ds,
        ):
            if (hand_ds.width, hand_ds.height) != (cat_ds.width, cat_ds.height):
                raise ValueError(
                    f"HAND ({hand_ds.width}x{hand_ds.height}) and catchments "
                    f"({cat_ds.width}x{cat_ds.height}) grids differ for branch {bid}"
                )

            depth_meta = hand_ds.meta.copy()
            if self.int16_mode:
                # Depth in millimetres as int16.
                depth_meta.update(
                    dtype="int16", count=1, nodata=0,
                    compress="lzw", tiled=True, blockxsize=512, blockysize=512,
                    BIGTIFF="YES",
                )
            else:
                depth_meta.update(
                    dtype="float32", count=1, nodata=-9999.0,
                    compress="lzw", tiled=True, blockxsize=512, blockysize=512,
                    BIGTIFF="YES",
                )
            ext_meta = hand_ds.meta.copy()
            ext_meta.update(
                dtype="int32", count=1, nodata=0,
                compress="lzw", tiled=True, blockxsize=512, blockysize=512,
                BIGTIFF="YES",
            )

            # Build vectorised lookup arrays: index = HydroID, value = stage.
            max_hid = max(stage_by_hydroid)
            stage_arr = np.zeros(max_hid + 1, dtype=np.float32)
            for hid, st in stage_by_hydroid.items():
                if 0 <= hid <= max_hid:
                    stage_arr[hid] = st

            hand_nodata = hand_ds.nodata
            cat_nodata = cat_ds.nodata

            n_wet_pixels = 0
            n_wet_hydroids: set[int] = set()
            max_depth = 0.0

            if self.extent_out.exists():
                self.extent_out.unlink()
            if self.depth_out.exists():
                self.depth_out.unlink()

            with (
                rasterio.open(self.extent_out, "w", **ext_meta) as ext_ds,
                rasterio.open(self.depth_out, "w", **depth_meta) as depth_ds,
            ):
                for _, window in hand_ds.block_windows(1):
                    hand = hand_ds.read(1, window=window).astype(np.float32)
                    cat = cat_ds.read(1, window=window).astype(np.int32)

                    # Pixels with valid catchment + non-negative HAND can be tested. Everything else is dry.
                    valid = (cat > 0) & (cat <= max_hid) & (hand >= 0)
                    if hand_nodata is not None and not np.isnan(hand_nodata):
                        valid &= (hand != hand_nodata)
                    valid &= ~np.isnan(hand)

                    # Vectorised stage lookup via indexing.
                    safe_cat = np.where(valid, cat, 0)
                    stage = stage_arr[safe_cat]
                    depth = stage - hand
                    wet = valid & (depth > self.min_depth_m)

                    if self.int16_mode:
                        depth_mm = np.round(depth * 1000.0)
                        depth_mm = np.clip(depth_mm, 0, 32767)
                        out_depth = np.where(wet, depth_mm, 0).astype(np.int16)
                    else:
                        out_depth = np.where(wet, depth, 0.0).astype(np.float32)
                    out_ext = np.where(wet, cat, -cat).astype(np.int32)
                    # cells that are entirely outside any catchment stay 0
                    out_ext = np.where(cat <= 0, 0, out_ext)

                    depth_ds.write(out_depth, 1, window=window)
                    ext_ds.write(out_ext, 1, window=window)

                    if wet.any():
                        n_wet_pixels += int(wet.sum())
                        # Convert mm back to m for the summary stat so max_depth_m is comparable between modes.
                        block_max_m = (
                            float(out_depth.max()) / 1000.0
                            if self.int16_mode
                            else float(out_depth.max())
                        )
                        if block_max_m > max_depth:
                            max_depth = block_max_m
                        for hid in np.unique(cat[wet]):
                            n_wet_hydroids.add(int(hid))

        log.info(
            f"Inundator branch {bid}: {n_wet_pixels} wet pixels, "
            f"{len(n_wet_hydroids)} HydroIDs, max depth {max_depth:.2f} m"
            f"{' (int16/mm storage)' if self.int16_mode else ''}"
        )
        return InundationResult(
            branch_id=bid,
            extent_path=self.extent_out,
            depth_path=self.depth_out,
            n_hydroids_wet=len(n_wet_hydroids),
            n_pixels_wet=n_wet_pixels,
            max_depth_m=max_depth,
        )

