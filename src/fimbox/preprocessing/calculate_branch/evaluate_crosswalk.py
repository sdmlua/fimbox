"""
Author: Supath Dhital
Date Updated: May 2026

Evaluate crosswalk accuracy between DEM-derived reaches and NWM flowlines.

Two complementary checks:
1. **Intersections check.** For every DEM-derived reach, look at every NWM
   reach it overlaps. The crosswalk is "correct" for a reach when the NWM
   reach with the most intersection points is the same NWM reach the
   crosswalk assigned (highest overlap = correct match).

2. **Network check.** Build upstream-walks of both the DEM network and the
   NWM network from outlets to headwaters. For every DEM reach, compare its
   set of immediate-upstream NWM ``feature_id``s against the NWM network's
   own upstream set. Status codes:
       0  -> crosswalk agrees (correct)
       1  -> crosswalk disagrees (incorrect)
       2  -> upstream set empty
       3  -> reach is at a headwater
      -1  -> duplicate feature_id at confluence (skipped)

Inputs
------
flows_gpkg                : demDerived_reaches_split_filtered_addedAttributes_crosswalked_{B}.gpkg
nwm_flows_gpkg            : nwm_subset_streams.gpkg (with ``ID``, ``to``)
nwm_headwaters_gpkg       : nwm_headwater_points_subset.gpkg
aoi_id                    : AOI identifier (recorded on every output row)
branch_id                 : branch identifier (recorded on every output row)

Outputs
-------
out_csv : crosswalk_evaluation_{B}.csv with columns
    ``huc, branch, method, correct, total, proportion``
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

import geopandas as gpd
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def evaluate_crosswalk(
    flows_gpkg: PathLike,
    nwm_flows_gpkg: PathLike,
    nwm_headwaters_gpkg: PathLike,
    out_csv: PathLike,
    aoi_id: str,
    branch_id: str,
) -> pd.DataFrame:
    """Run both crosswalk-accuracy checks and write the summary CSV."""
    flows_gpkg = Path(flows_gpkg)
    nwm_flows_gpkg = Path(nwm_flows_gpkg)
    nwm_headwaters_gpkg = Path(nwm_headwaters_gpkg)
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    intersections = _evaluate_intersections(flows_gpkg, nwm_flows_gpkg)
    int_correct = int(intersections["crosswalk"].sum())
    int_total = int(len(intersections))
    int_prop = (int_correct / int_total) if int_total else float("nan")

    network = _evaluate_network(flows_gpkg, nwm_flows_gpkg, nwm_headwaters_gpkg)
    network = network[network["status"] >= 0]
    net_correct = int((network["status"] == 0).sum())
    net_total = int(len(network))
    net_prop = (net_correct / net_total) if net_total else float("nan")

    results = pd.DataFrame(
        {
            "huc": [aoi_id, aoi_id],
            "branch": [branch_id, branch_id],
            "method": ["intersections", "network"],
            "correct": [int_correct, net_correct],
            "total": [int_total, net_total],
            "proportion": [int_prop, net_prop],
        }
    )
    results.to_csv(out_csv, index=False)
    log.info(
        f"Crosswalk evaluation (branch {branch_id}): "
        f"intersections {int_correct}/{int_total} ({int_prop:.2%}), "
        f"network {net_correct}/{net_total} ({net_prop:.2%}) --> {out_csv.name}"
    )
    return results


def _evaluate_intersections(
    flows_gpkg: Path, nwm_flows_gpkg: Path
) -> pd.DataFrame:
    flows = gpd.read_file(flows_gpkg)
    nwm_streams = gpd.read_file(nwm_flows_gpkg)
    intersects = flows.sjoin(nwm_streams)

    xwalks: list[list] = []
    for idx in intersects.index:
        flows_idx = intersects.loc[intersects.index == idx, "HydroID"].unique()

        ids_at_idx = intersects.loc[idx, "ID"]
        if isinstance(ids_at_idx, np.integer):
            streams_idxs = [int(ids_at_idx)]
        else:
            streams_idxs = np.unique(np.asarray(ids_at_idx)).tolist()

        for flow_hid in flows_idx:
            for stream_id in streams_idxs:
                inter = gpd.overlay(
                    flows[flows["HydroID"] == flow_hid],
                    nwm_streams[nwm_streams["ID"] == stream_id],
                    keep_geom_type=False,
                )
                fid_series = flows.loc[flows["HydroID"] == flow_hid, "feature_id"]
                if inter.empty:
                    intersect_points = 0
                    feature_id = fid_series
                elif inter.geometry.iloc[0].geom_type == "Point":
                    intersect_points = 1
                    feature_id = fid_series
                else:
                    intersect_points = len(inter.geometry.iloc[0].geoms)
                    feature_id = int(fid_series.iloc[0])
                xwalks.append([flow_hid, feature_id, stream_id, intersect_points])

    df = pd.DataFrame(
        xwalks, columns=["HydroID", "feature_id", "ID", "intersect_points"]
    )
    if df.empty:
        return df.assign(crosswalk=pd.Series(dtype=bool))

    df["feature_id"] = df["feature_id"].astype(int)
    df["match"] = df["feature_id"] == df["ID"]
    max_per_hid = df[["HydroID", "intersect_points"]].groupby("HydroID").max()
    df = df.merge(max_per_hid, on="HydroID", how="left")
    df["max"] = df["intersect_points_x"] == df["intersect_points_y"]
    df["crosswalk"] = df["match"] == df["max"]
    return df


def _evaluate_network(
    flows_gpkg: Path, nwm_flows_gpkg: Path, nwm_headwaters_gpkg: Path
) -> pd.DataFrame:
    flows = gpd.read_file(flows_gpkg)
    flows["HydroID"] = flows["HydroID"].astype(int)
    nwm_streams = gpd.read_file(nwm_flows_gpkg).rename(columns={"ID": "feature_id"})
    nwm_headwaters = gpd.read_file(nwm_headwaters_gpkg)

    streams_outlets = nwm_streams.loc[
        ~nwm_streams["to"].isin(nwm_streams["feature_id"]), "feature_id"
    ]
    flows_outlets = flows.loc[~flows["NextDownID"].isin(flows["HydroID"]), "HydroID"]

    nwm_streams_hw_mask = ~nwm_streams["feature_id"].isin(nwm_streams["to"])
    flows_hw_mask = ~flows["HydroID"].isin(flows["NextDownID"])

    nwm_streams_hw = nwm_streams[nwm_streams_hw_mask]
    flows_hw = flows[flows_hw_mask]

    flows_hw = flows_hw.sjoin_nearest(nwm_headwaters)[["HydroID", "ID"]]
    nwm_streams_hw = nwm_streams_hw.sjoin_nearest(nwm_headwaters)[["feature_id", "ID"]]

    def _hid_to_fid(df: pd.DataFrame, hid: int, hid_col: str, fid_col: str):
        return df.loc[df[hid_col] == hid, fid_col]

    def _walk_upstream(data, data_hw, acc, hid, hid_col, down_col):
        acc[hid] = list(data.loc[data[down_col] == hid, hid_col].values)
        for child in acc[hid]:
            if child in data_hw[hid_col].values:
                acc[child] = data_hw.loc[data_hw[hid_col] == child, "ID"].values[0]
            else:
                acc = _walk_upstream(data, data_hw, acc, child, hid_col, down_col)
        return acc

    flows_tree: dict = {}
    for h in flows_outlets:
        flows_tree = _walk_upstream(
            flows, flows_hw, flows_tree, int(h), "HydroID", "NextDownID"
        )

    streams_tree: dict = {}
    for f in streams_outlets:
        streams_tree = _walk_upstream(
            nwm_streams, nwm_streams_hw, streams_tree, int(f), "feature_id", "to"
        )

    results: list[list] = []
    for flow_hid in flows_tree:
        try:
            fid = int(_hid_to_fid(flows, flow_hid, "HydroID", "feature_id").iloc[0])
        except IndexError:
            continue
        upstream_hids = flows_tree[flow_hid]
        nwm_fids = streams_tree.get(fid, [])
        upstream_fids: list[int] = []

        if isinstance(upstream_hids, np.integer):
            results.append([flow_hid, fid, upstream_fids, nwm_fids, 3])
            continue

        if not upstream_hids:
            results.append([flow_hid, fid, upstream_fids, nwm_fids, 2])
            continue

        for child in upstream_hids:
            try:
                child_fid = int(
                    _hid_to_fid(flows, child, "HydroID", "feature_id").iloc[0]
                )
                upstream_fids.append(child_fid)
            except IndexError:
                continue

        if isinstance(nwm_fids, np.integer):
            nwm_fids = [int(nwm_fids)]

        if fid in upstream_fids:
            status = -1
        elif set(upstream_fids) == set(nwm_fids):
            status = 0
        else:
            status = 1
        results.append([flow_hid, fid, upstream_fids, nwm_fids, status])

    return pd.DataFrame(
        results,
        columns=["HydroID", "feature_id", "upstream_fids", "upstream_nwm_fids", "status"],
    )


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[3])
    parser.add_argument("-a", "--flows", required=True)
    parser.add_argument("-b", "--nwm-flows", required=True)
    parser.add_argument("-d", "--nwm-headwaters", required=True)
    parser.add_argument("-c", "--out-csv", required=True)
    parser.add_argument("-u", "--aoi-id", required=True)
    parser.add_argument("-z", "--branch-id", required=True)
    args = parser.parse_args()
    evaluate_crosswalk(
        flows_gpkg=args.flows,
        nwm_flows_gpkg=args.nwm_flows,
        nwm_headwaters_gpkg=args.nwm_headwaters,
        out_csv=args.out_csv,
        aoi_id=args.aoi_id,
        branch_id=args.branch_id,
    )
