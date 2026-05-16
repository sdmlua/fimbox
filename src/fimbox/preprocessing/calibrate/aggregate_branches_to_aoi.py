"""
Author: Supath Dhital
Date Updated: May 2026

Aggregate per-branch outputs (usgs_elev_table, ras_elev_table, hydroTable,
src_full_crosswalked, OSM bridge centroids, OSM road FIMpacts) into AOI-level
files. Port of inundation-mapping's ``aggregate_branches_to_huc.py`` —
generalised so the AOI can be any drainage area, not only a USGS HUC.

This is the bookend around the calibration chain:

  1. Pre-calibration call ``aggregate_branches(aoi_dir, usgs_elev=True, ras=True)``
     so the SRC-adjustment routines have AOI-wide elev tables to consume.
  2. Post-calibration call ``aggregate_branches(aoi_dir, htable=True,
     bridge=True, road=True)`` to publish the final AOI hydroTable +
     bridge / road centroid layers.

Inputs (per branch, found under ``<aoi_dir>/branches/<branch_id>/``)
    usgs_elev_table.csv                  - per-branch USGS gage DEM samples
    ras_elev_table.csv                   - per-branch RAS2FIM cross-section DEM samples
    hydroTable_{id}.csv                  - per-branch hydroTable from add_crosswalk
    src_full_crosswalked_{id}.csv        - per-branch SRC with NWM crosswalk
    osm_bridge_centroids_{id}.gpkg       - per-branch bridge centroids (heal_bridges_osm)
    osm_roads_fimpact_{id}.csv           - per-branch road FIMpact (process_roads_fimpact)

Outputs (written to ``<aoi_dir>/``)
    usgs_elev_table.csv         - AOI-wide
    ras_elev_table.csv          - AOI-wide
    hydrotable.csv              - AOI-wide
    hydrotable.feather          - subset of columns, fast inundation load
    hydrotable.parquet          - HydroID/feature_id indexed parquet
    src_full_crosswalked.csv    - AOI-wide
    osm_bridge_centroids.gpkg   - AOI-wide
    osm_roads_fimpact.csv       - AOI-wide

Note: the inundation-mapping ``HUC`` column inside the hydroTable schema is
preserved as-is because downstream FIM tools key off that column name. It
just stores the AOI identifier string.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import geopandas as gpd
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


# Column dtypes copied verbatim from inundation-mapping to keep wire-compatibility
USGS_DTYPES = {
    "location_id": str, "nws_lid": str, "feature_id": int, "HydroID": int,
    "levpa_id": str, "dem_elevation": float, "dem_adj_elevation": float,
    "order_": str, "LakeID": object, "HUC8": str, "snap_distance": float,
}
RAS_DTYPES = dict(USGS_DTYPES)

HYDROTABLE_DTYPES = {
    "HydroID": int, "branch_id": int, "feature_id": int, "NextDownID": int,
    "order_": int, "Number of Cells": int, "SurfaceArea (m2)": float,
    "BedArea (m2)": float, "TopWidth (m)": float, "LENGTHKM": float,
    "AREASQKM": float, "WettedPerimeter (m)": float, "HydraulicRadius (m)": float,
    "WetArea (m2)": float, "Volume (m3)": float, "SLOPE": float, "ManningN": float,
    "stage": float, "default_discharge_cms": float, "default_Volume (m3)": float,
    "default_WetArea (m2)": float, "default_HydraulicRadius (m)": float,
    "default_ManningN": float, "calb_applied": bool, "last_updated": str,
    "submitter": str, "obs_source": str, "precalb_discharge_cms": float,
    "calb_coef_usgs": float, "calb_coef_spatial": float, "calb_coef_final": float,
    "HUC": int, "LakeID": int, "subdiv_applied": bool, "channel_n": float,
    "overbank_n": float, "subdiv_discharge_cms": float, "discharge_cms": float,
}

SRC_CROSS_DTYPES = {
    "branch_id": int, "HydroID": int, "feature_id": int, "Stage": float,
    "Number of Cells": int, "SurfaceArea (m2)": float, "BedArea (m2)": float,
    "Volume (m3)": float, "SLOPE": float, "LENGTHKM": float, "AREASQKM": float,
    "ManningN": float, "NextDownID": int, "order_": int, "TopWidth (m)": float,
    "WettedPerimeter (m)": float, "WetArea (m2)": float, "HydraulicRadius (m)": float,
    "Discharge (m3s-1)": float, "bankfull_flow": float, "Stage_bankfull": float,
    "BedArea_bankfull": float, "Volume_bankfull": float, "HRadius_bankfull": float,
    "SurfArea_bankfull": float, "bankfull_proxy": str, "Volume_chan (m3)": float,
    "BedArea_chan (m2)": float, "WettedPerimeter_chan (m)": float,
    "Volume_obank (m3)": float, "BedArea_obank (m2)": float,
    "WettedPerimeter_obank (m)": float, "channel_n": float, "overbank_n": float,
    "subdiv_applied": bool, "WetArea_chan (m2)": float,
    "HydraulicRadius_chan (m)": float, "Discharge_chan (m3s-1)": float,
    "Velocity_chan (m/s)": float, "WetArea_obank (m2)": float,
    "HydraulicRadius_obank (m)": float, "Discharge_obank (m3s-1)": float,
    "Velocity_obank (m/s)": float, "Discharge (m3s-1)_subdiv": float,
}

BRIDGE_DTYPES = {
    "osmid": int, "name": str, "threshold_hand": float,
    "threshold_hand_75": float, "has_lidar_tif": str, "feature_id": int,
    "HydroID": int, "order_": str, "branch": str, "mainstem": int,
    "geometry": object,
}

ROAD_DTYPES = {
    "osmid": str, "highway": str, "name": str, "huc8": str,
    "osmid_catchid": str, "HydroID": str, "feature_id": str, "order_": str,
    "branch": str, "threshold_hand": float, "threshold_discharge": float,
    "threshold_hand_ft": float, "threshold_discharge_cfs": float,
}


@dataclass
class AggregationFlags:
    usgs_elev: bool = False
    ras_elev: bool = False
    htable: bool = False
    src_cross: bool = False
    bridge: bool = False
    road: bool = False


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
    """Walk every branch under ``<aoi_dir>/branches/`` and aggregate the
    requested file types into AOI-level outputs. Quiet no-ops when a per-branch
    file is missing — same behaviour as inundation-mapping.

    ``aoi_dir`` and ``huc_dir`` are equivalent — pass whichever matches your
    workflow."""
    from ._stub import resolve_aoi_dir

    aoi_dir = Path(resolve_aoi_dir(aoi_dir, huc_dir))
    if not aoi_dir.is_dir():
        raise NotADirectoryError(aoi_dir)

    aoi_id = aoi_dir.name
    flags = AggregationFlags(usgs_elev, ras_elev, htable, src_cross, bridge, road)
    log.info(f"--- aggregate_branches: {aoi_id} ---")

    agg_usgs = []
    agg_ras = []
    agg_htable = []
    agg_src = []
    agg_bridge = []
    agg_road = []

    branches_dir = aoi_dir / "branches"
    if not branches_dir.is_dir():
        log.warning(f"No branches directory under {aoi_dir}")
        return

    branch_ids = limit_branches or sorted(d.name for d in branches_dir.iterdir() if d.is_dir())
    for bid in branch_ids:
        bp = branches_dir / bid
        if flags.usgs_elev:
            _append_csv(bp / "usgs_elev_table.csv", USGS_DTYPES, agg_usgs)
        if flags.ras_elev:
            _append_csv(bp / "ras_elev_table.csv", RAS_DTYPES, agg_ras)
        if flags.htable:
            _append_htable(bp / f"hydroTable_{bid}.csv", bid, agg_htable)
        if flags.src_cross:
            _append_src_cross(bp / f"src_full_crosswalked_{bid}.csv", bid, agg_src)
        if flags.bridge:
            _append_bridge(bp, bid, agg_bridge)
        if flags.road:
            _append_road(bp, bid, agg_road)

    if flags.usgs_elev:
        _write_csv(aoi_dir / "usgs_elev_table.csv", agg_usgs)
    if flags.ras_elev:
        _write_csv(aoi_dir / "ras_elev_table.csv", agg_ras)
    if flags.src_cross:
        _write_csv(aoi_dir / "src_full_crosswalked.csv", agg_src)
    if flags.htable:
        _write_htable(aoi_dir, agg_htable)
    if flags.bridge:
        _write_bridge(aoi_dir, aoi_id, agg_bridge, default_crs)
    if flags.road:
        _write_road(aoi_dir, agg_road)


def _append_csv(path: Path, dtypes: dict, sink: list) -> None:
    if path.is_file():
        sink.append(pd.read_csv(path, dtype=dtypes))


def _append_htable(path: Path, branch_id: str, sink: list) -> None:
    if not path.is_file():
        return
    df = pd.read_csv(path, dtype=HYDROTABLE_DTYPES)
    df["branch_id"] = int(branch_id) if str(branch_id).isdigit() else branch_id
    if "calb_applied" in df.columns:
        df["calb_applied"] = df["calb_applied"].fillna(False)
    sink.append(df)


def _append_src_cross(path: Path, branch_id: str, sink: list) -> None:
    if not path.is_file():
        return
    df = pd.read_csv(path, dtype=SRC_CROSS_DTYPES)
    df["branch_id"] = int(branch_id) if str(branch_id).isdigit() else branch_id
    sink.append(df)


def _append_bridge(branch_path: Path, branch_id: str, sink: list) -> None:
    centroids = branch_path / f"osm_bridge_centroids_{branch_id}.gpkg"
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

    # Attach threshold_discharge by stage lookup on the per-branch hydroTable
    htable_path = branch_path / f"hydroTable_{branch_id}.csv"
    if htable_path.is_file():
        htable = pd.read_csv(htable_path, dtype=HYDROTABLE_DTYPES)
        bridge_pnts = _attach_threshold_discharge(bridge_pnts, htable)
    sink.append(bridge_pnts)


def _append_road(branch_path: Path, branch_id: str, sink: list) -> None:
    fimpact_path = branch_path / f"osm_roads_fimpact_{branch_id}.csv"
    if not fimpact_path.is_file():
        return
    df = pd.read_csv(fimpact_path)
    if df.empty:
        return

    # Drop dry roads (threshold_hand >= max HAND in table = 25 m by convention)
    df = df[df["threshold_hand"] < 25].copy()
    if df.empty:
        return

    htable_path = branch_path / f"hydroTable_{branch_id}.csv"
    if not htable_path.is_file():
        return
    htable = pd.read_csv(htable_path, dtype=HYDROTABLE_DTYPES)

    df["threshold_discharge"] = df.apply(
        lambda row: _flow_lookup(row.threshold_hand, row.HydroID, htable), axis=1
    )
    df["threshold_hand_ft"] = df["threshold_hand"] * 3.28084
    df["threshold_discharge_cfs"] = df["threshold_discharge"] * 35.3147
    sink.append(df)


def _attach_threshold_discharge(
    bridge_pnts: gpd.GeoDataFrame, htable: pd.DataFrame
) -> gpd.GeoDataFrame:
    """Interpolate discharge at each bridge's threshold_hand using its
    HydroID rating curve. Mirrors heal_bridges_osm.flows_from_hydrotable."""
    bridge_pnts = bridge_pnts.copy()
    bridge_pnts["threshold_discharge"] = bridge_pnts.apply(
        lambda r: _flow_lookup(r.threshold_hand, r.HydroID, htable), axis=1
    )
    return bridge_pnts


def _flow_lookup(stage: float, hydroid: int, htable: pd.DataFrame) -> float:
    """Interpolate discharge at ``stage`` from the HydroID's rating curve."""
    sub = htable[htable["HydroID"] == hydroid]
    if sub.empty:
        return float("nan")
    sub = sub.sort_values("stage")
    return float(np.interp(stage, sub["stage"].to_numpy(), sub["discharge_cms"].to_numpy()))


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

    # Streamlined views used by HydroVIS / inundation downstream
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
    temp = temp.sort_values([c for c in ("HydroID", "feature_id", "discharge_cms") if c in temp.columns])
    if {"HydroID", "feature_id"}.issubset(temp.columns):
        temp = temp.set_index(["HydroID", "feature_id"])
    temp.to_parquet(csv_path.with_suffix(".parquet"), compression="zstd", index=True)
    log.info(f"AOI hydroTable --> {csv_path.with_suffix('.parquet').name}")


def _write_bridge(
    aoi_dir: Path, aoi_id: str, frames: list, default_crs: Union[str, int]
) -> None:
    out_path = aoi_dir / "osm_bridge_centroids.gpkg"
    if out_path.is_file():
        out_path.unlink()
    if not frames:
        return
    bridge_pnts = pd.concat(frames, ignore_index=True)
    if bridge_pnts.empty:
        return

    # Use branch 0 to attach crossing_feature_id
    b0 = bridge_pnts.loc[bridge_pnts["branch"].astype(str) == "0", ["osmid", "feature_id"]]
    b0 = b0.rename(columns={"feature_id": "crossing_feature_id"})
    bridge_pnts = bridge_pnts.merge(b0, on="osmid", how="left")

    # Within each (osmid, feature_id), keep the row with minimum threshold_discharge
    g = bridge_pnts.groupby(["osmid", "feature_id"])["threshold_discharge"].transform("min")
    bridge_pnts = bridge_pnts[bridge_pnts["threshold_discharge"] == g].copy()

    bridge_pnts["is_backwater"] = 0
    c = bridge_pnts.groupby(["osmid"])["feature_id"].transform("count")
    bridge_pnts.loc[
        (c > 1) & (bridge_pnts["feature_id"] != bridge_pnts["crossing_feature_id"]),
        "is_backwater",
    ] = 1

    bridge_pnts = bridge_pnts.astype(BRIDGE_DTYPES, errors="ignore")
    bridge_pnts = gpd.GeoDataFrame(bridge_pnts, geometry="geometry")
    if bridge_pnts.crs is None:
        bridge_pnts = bridge_pnts.set_crs(default_crs)
    bridge_pnts.to_file(out_path, index=False)
    log.info(f"AOI bridge centroids --> {out_path.name} ({len(bridge_pnts)} rows)")


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


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="Aggregate per-branch outputs into AOI-level files."
    )
    parser.add_argument("-aoi_dir", required=True)
    parser.add_argument("-elev", "--usgs_elev_flag", action="store_true")
    parser.add_argument("-ras", "--ras_elev_flag", action="store_true")
    parser.add_argument("-htable", "--hydro_table_flag", action="store_true")
    parser.add_argument("-src", "--src_cross_flag", action="store_true")
    parser.add_argument("-bridge", "--bridge_flag", action="store_true")
    parser.add_argument("-road", "--road_flag", action="store_true")
    args = parser.parse_args()
    aggregate_branches(
        aoi_dir=args.aoi_dir,
        usgs_elev=args.usgs_elev_flag,
        ras_elev=args.ras_elev_flag,
        htable=args.hydro_table_flag,
        src_cross=args.src_cross_flag,
        bridge=args.bridge_flag,
        road=args.road_flag,
    )
