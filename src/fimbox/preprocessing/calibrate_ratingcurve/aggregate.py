"""
Author: Supath Dhital
Date Updated: May 2026

Aggregate per-branch outputs (usgs / ras elev tables, hydroTable,
src_full_crosswalked, OSM bridge centroids, OSM road FIMpact) into
AOI-level files. Used twice in the pipeline: once before calibration
(to assemble the elev tables every adjustment routine consumes), and
once after (to publish the final AOI hydroTable + bridge / road layers).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import geopandas as gpd
import numpy as np
import pandas as pd

from ._common import (
    BRIDGE_DTYPES,
    HYDROTABLE_DTYPES,
    PathLike,
    RAS_DTYPES,
    ROAD_DTYPES,
    SRC_CROSS_DTYPES,
    USGS_DTYPES,
    resolve_aoi_dir,
)

log = logging.getLogger(__name__)


@dataclass
class BranchAggregator:
    # All-purpose AOI-level aggregator. Pass the flags you want; everything else is a no-op.

    aoi_dir: PathLike
    usgs_elev: bool = False
    ras_elev: bool = False
    htable: bool = False
    src_cross: bool = False
    bridge: bool = False
    road: bool = False
    limit_branches: Optional[Sequence[str]] = None
    default_crs: Union[str, int] = 5070

    def run(self) -> None:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        if not aoi_dir.is_dir():
            raise NotADirectoryError(aoi_dir)

        aoi_id = aoi_dir.name
        log.info(f"--- BranchAggregator: {aoi_id} ---")

        branches_dir = aoi_dir / "branches"
        if not branches_dir.is_dir():
            log.warning(f"No branches directory under {aoi_dir}")
            return

        # Sinks for each requested output.
        sinks = {
            "usgs": [], "ras": [], "htable": [], "src": [], "bridge": [], "road": [],
        }

        branch_ids = self.limit_branches or sorted(
            d.name for d in branches_dir.iterdir() if d.is_dir()
        )
        for bid in branch_ids:
            bp = branches_dir / bid
            if self.usgs_elev:
                self._append_csv(bp / "usgs_elev_table.csv", USGS_DTYPES, sinks["usgs"])
            if self.ras_elev:
                self._append_csv(bp / "ras_elev_table.csv", RAS_DTYPES, sinks["ras"])
            if self.htable:
                self._append_htable(bp / f"hydroTable_{bid}.csv", bid, sinks["htable"])
            if self.src_cross:
                self._append_src(bp / f"src_full_crosswalked_{bid}.csv", bid, sinks["src"])
            if self.bridge:
                self._append_bridge(bp, bid, sinks["bridge"])
            if self.road:
                self._append_road(bp, bid, sinks["road"])

        if self.usgs_elev:
            self._write_csv(aoi_dir / "usgs_elev_table.csv", sinks["usgs"])
        if self.ras_elev:
            self._write_csv(aoi_dir / "ras_elev_table.csv", sinks["ras"])
        if self.src_cross:
            self._write_csv(aoi_dir / "src_full_crosswalked.csv", sinks["src"])
        if self.htable:
            self._write_htable(aoi_dir, sinks["htable"])
        if self.bridge:
            self._write_bridge(aoi_dir, sinks["bridge"])
        if self.road:
            self._write_road(aoi_dir, sinks["road"])

    @staticmethod
    def _append_csv(path: Path, dtypes: dict, sink: list) -> None:
        if path.is_file():
            sink.append(pd.read_csv(path, dtype=dtypes))

    @staticmethod
    def _append_htable(path: Path, bid: str, sink: list) -> None:
        if not path.is_file():
            return
        df = pd.read_csv(path, dtype=HYDROTABLE_DTYPES)
        df["branch_id"] = int(bid) if str(bid).isdigit() else bid
        if "calb_applied" in df.columns:
            df["calb_applied"] = df["calb_applied"].fillna(False)
        sink.append(df)

    @staticmethod
    def _append_src(path: Path, bid: str, sink: list) -> None:
        if not path.is_file():
            return
        df = pd.read_csv(path, dtype=SRC_CROSS_DTYPES)
        df["branch_id"] = int(bid) if str(bid).isdigit() else bid
        sink.append(df)

    @staticmethod
    def _append_bridge(branch_path: Path, bid: str, sink: list) -> None:
        centroids = branch_path / f"osm_bridge_centroids_{bid}.gpkg"
        if not centroids.is_file():
            return
        bridge_pnts = gpd.read_file(centroids)
        for col, dtype in BRIDGE_DTYPES.items():
            if col in bridge_pnts.columns:
                try:
                    bridge_pnts[col] = bridge_pnts[col].astype(dtype)
                except Exception:
                    pass
        if bridge_pnts.empty:
            return

        ht_path = branch_path / f"hydroTable_{bid}.csv"
        if ht_path.is_file():
            ht = pd.read_csv(ht_path, dtype=HYDROTABLE_DTYPES)
            bridge_pnts = bridge_pnts.copy()
            bridge_pnts["threshold_discharge"] = bridge_pnts.apply(
                lambda r: _flow_lookup(r.threshold_hand, r.HydroID, ht), axis=1
            )
        sink.append(bridge_pnts)

    @staticmethod
    def _append_road(branch_path: Path, bid: str, sink: list) -> None:
        fimpact = branch_path / f"osm_roads_fimpact_{bid}.csv"
        if not fimpact.is_file():
            return
        df = pd.read_csv(fimpact)
        if df.empty:
            return

        # Drop dry roads (threshold_hand at the 25 m ceiling means never flooded).
        df = df[df["threshold_hand"] < 25].copy()
        if df.empty:
            return

        ht_path = branch_path / f"hydroTable_{bid}.csv"
        if not ht_path.is_file():
            return
        ht = pd.read_csv(ht_path, dtype=HYDROTABLE_DTYPES)
        df["threshold_discharge"] = df.apply(
            lambda r: _flow_lookup(r.threshold_hand, r.HydroID, ht), axis=1
        )
        df["threshold_hand_ft"] = df["threshold_hand"] * 3.28084
        df["threshold_discharge_cfs"] = df["threshold_discharge"] * 35.3147
        sink.append(df)

    @staticmethod
    def _write_csv(path: Path, frames: list) -> None:
        if path.is_file():
            path.unlink()
        if not frames:
            return
        out = pd.concat(frames, ignore_index=True)
        if out.empty:
            return
        out.to_csv(path, index=False)
        log.info(f"Aggregated --> {path.name} ({len(out)} rows)")

    @staticmethod
    def _write_htable(aoi_dir: Path, frames: list) -> None:
        csv_path = aoi_dir / "hydrotable.csv"
        if csv_path.is_file():
            csv_path.unlink()
        if not frames:
            return
        out = pd.concat(frames, ignore_index=True)
        if out.empty:
            return
        out.to_csv(csv_path, index=False)
        log.info(f"AOI hydroTable --> {csv_path.name} ({len(out)} rows)")

        # Compact view for downstream inundation tools (feather + parquet).
        req_cols = [
            "HUC", "branch_id", "feature_id", "HydroID", "stage", "discharge_cms",
            "SurfaceArea (m2)", "LakeID", "Bathymetry_source",
        ]
        present = [c for c in req_cols if c in out.columns]
        if not present:
            return
        dtype = {
            "HUC": str, "branch_id": int, "feature_id": str, "HydroID": str,
            "stage": float, "discharge_cms": float, "SurfaceArea (m2)": int, "LakeID": int,
        }
        temp = out.reset_index()[present].astype({k: v for k, v in dtype.items() if k in present})
        temp.to_feather(csv_path.with_suffix(".feather"))
        sort_cols = [c for c in ("HydroID", "feature_id", "discharge_cms") if c in temp.columns]
        if sort_cols:
            temp = temp.sort_values(sort_cols)
        if {"HydroID", "feature_id"}.issubset(temp.columns):
            temp = temp.set_index(["HydroID", "feature_id"])
        temp.to_parquet(csv_path.with_suffix(".parquet"), compression="zstd", index=True)
        log.info(f"AOI hydroTable --> {csv_path.with_suffix('.parquet').name}")

    def _write_bridge(self, aoi_dir: Path, frames: list) -> None:
        out_path = aoi_dir / "osm_bridge_centroids.gpkg"
        if out_path.is_file():
            out_path.unlink()
        if not frames:
            return
        bridge_pnts = pd.concat(frames, ignore_index=True)
        if bridge_pnts.empty:
            return

        # Branch 0 is treated as the "crossing" reference so each bridge knows
        # which feature_id it's nominally crossing (as opposed to backwater).
        b0 = bridge_pnts.loc[
            bridge_pnts["branch"].astype(str) == "0", ["osmid", "feature_id"]
        ].rename(columns={"feature_id": "crossing_feature_id"})
        bridge_pnts = bridge_pnts.merge(b0, on="osmid", how="left")

        # For each (osmid, feature_id) keep the lowest threshold_discharge row.
        g = bridge_pnts.groupby(["osmid", "feature_id"])["threshold_discharge"].transform("min")
        bridge_pnts = bridge_pnts[bridge_pnts["threshold_discharge"] == g].copy()

        bridge_pnts["is_backwater"] = 0
        cnt = bridge_pnts.groupby(["osmid"])["feature_id"].transform("count")
        bridge_pnts.loc[
            (cnt > 1) & (bridge_pnts["feature_id"] != bridge_pnts["crossing_feature_id"]),
            "is_backwater",
        ] = 1

        bridge_pnts = bridge_pnts.astype(BRIDGE_DTYPES, errors="ignore")
        bridge_pnts = gpd.GeoDataFrame(bridge_pnts, geometry="geometry")
        if bridge_pnts.crs is None:
            bridge_pnts = bridge_pnts.set_crs(self.default_crs)
        bridge_pnts.to_file(out_path, index=False)
        log.info(f"AOI bridge centroids --> {out_path.name} ({len(bridge_pnts)} rows)")

    @staticmethod
    def _write_road(aoi_dir: Path, frames: list) -> None:
        out_path = aoi_dir / "osm_roads_fimpact.csv"
        if out_path.is_file():
            out_path.unlink()
        if not frames:
            return
        out = pd.concat(frames, ignore_index=True)
        if out.empty:
            return
        out = out.astype(ROAD_DTYPES, errors="ignore")
        out.to_csv(out_path, index=False)
        log.info(f"AOI roads FIMpact --> {out_path.name} ({len(out)} rows)")


def _flow_lookup(stage: float, hydroid: int, htable: pd.DataFrame) -> float:
    # Interpolate discharge at the given stage from a HydroID's rating curve.
    sub = htable[htable["HydroID"] == hydroid]
    if sub.empty:
        return float("nan")
    sub = sub.sort_values("stage")
    return float(np.interp(stage, sub["stage"].to_numpy(), sub["discharge_cms"].to_numpy()))


# Convenience wrapper for callers that prefer a function-style entry.
def aggregate_branches(
    aoi_dir: Optional[PathLike] = None,
    *,
    huc_dir: Optional[PathLike] = None,
    usgs_elev: bool = False,
    ras_elev: bool = False,
    htable: bool = False,
    src_cross: bool = False,
    bridge: bool = False,
    road: bool = False,
    limit_branches: Optional[Sequence[str]] = None,
    default_crs: Union[str, int] = 5070,
) -> None:
    BranchAggregator(
        aoi_dir=resolve_aoi_dir(aoi_dir, huc_dir),
        usgs_elev=usgs_elev,
        ras_elev=ras_elev,
        htable=htable,
        src_cross=src_cross,
        bridge=bridge,
        road=road,
        limit_branches=limit_branches,
        default_crs=default_crs,
    ).run()
