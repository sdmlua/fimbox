"""
Author: Supath Dhital
Date Updated: June 2026

Shared SRC roughness-optimization engine.

Every observation-driven calibrator (USGS rating curves, spatial obs) feeds the
same routine: take observed (HAND, flow) pairs, find the matching SRC stage,
derive a per-HydroID calibration coefficient (Qsrc / Qobs), propagate it
downstream along the flow network, and recompute discharge. The calibrators
differ only in how they assemble the observation table; the math below is
common to all of them.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Walk/propagation distance and the acceptable adjusted-roughness window.
DOWNSTREAM_THRESHOLD = 8.0  # km to carry a group coefficient downstream
USGS_CALB_TRACE_DIST = 8.0  # km to walk the network from a gage
ROUGHNESS_MAX_THRESH = 0.8
ROUGHNESS_MIN_THRESH = 0.001

# Per-source name for the coefficient column written to the hydroTable.
_CALB_COL = {
    "point_obs": "calb_coef_spatial",
    "usgs_rating": "calb_coef_usgs",
    "ras2fim_rating": "calb_coef_ras2fim",
}


def update_rating_curve(
    branch_dir: Path,
    water_edge_median_df: pd.DataFrame,
    htable_path: Path,
    aoi_id: str,
    branch_id: str,
    catchments_poly_path: Path,
    debug_outputs: bool,
    source_tag: str,
    merge_prev_adj: bool = False,
    down_dist_thresh: float = DOWNSTREAM_THRESHOLD,
) -> str:
    # Calibrate the branch hydroTable from observed (HAND, flow) pairs.
    # water_edge_median_df carries: hydroid, flow, submitter, coll_time, layer, hand.
    calb_type = _CALB_COL.get(source_tag, "calb_coef_spatial")
    msg = f"{source_tag} calib aoi={aoi_id} branch={branch_id}"

    df_n = water_edge_median_df.copy().reset_index(drop=True)
    df_n = df_n[(df_n.hydroid.notnull()) & (df_n.hydroid > 0)]

    df_ht = pd.read_csv(
        htable_path,
        dtype={
            "HUC": object,
            "last_updated": object,
            "submitter": object,
            "obs_source": object,
        },
    )
    # Subdivision adds channel_n / overbank_n; before it runs only ManningN
    # exists, so fall back to it for the roughness lookup.
    if "channel_n" not in df_ht.columns:
        df_ht["channel_n"] = df_ht.get("ManningN", np.nan)
    if "overbank_n" not in df_ht.columns:
        df_ht["overbank_n"] = df_ht.get("ManningN", np.nan)

    # First calibration on this hydroTable — seed the calibration columns.
    if "precalb_discharge_cms" not in df_ht.columns:
        df_ht["calb_applied"] = False
        for c in (
            "last_updated",
            "submitter",
            "obs_source",
            "precalb_discharge_cms",
            "calb_coef_usgs",
            "calb_coef_spatial",
            "calb_coef_final",
        ):
            df_ht[c] = pd.NA
    if df_ht["precalb_discharge_cms"].isnull().values.any():
        df_ht["precalb_discharge_cms"] = df_ht["discharge_cms"].values

    # Retain prior usgs/ras2fim adjustments so a later source can blend, not clobber.
    df_prev = pd.DataFrame()
    if merge_prev_adj and not df_ht["calb_coef_final"].isnull().all():
        prev = (
            df_ht[
                [
                    "HydroID",
                    "submitter",
                    "last_updated",
                    "obs_source",
                    "calb_coef_final",
                ]
            ]
            .rename(
                columns={
                    "submitter": "submitter_prev",
                    "last_updated": "last_updated_prev",
                    "calb_coef_final": "calb_coef_final_prev",
                    "obs_source": "obs_source_prev",
                }
            )
            .groupby("HydroID")
            .first()
        )
        df_prev = prev[
            prev["obs_source_prev"].str.contains("usgs_rating|ras2fim_rating", na=False)
        ]

    # Drop this source's prior columns, fold precalb back to the working discharge.
    df_ht = df_ht.drop(
        [
            "discharge_cms",
            "submitter",
            "last_updated",
            calb_type,
            "calb_coef_final",
            "calb_applied",
            "obs_source",
        ],
        axis=1,
        errors="ignore",
    ).rename(columns={"precalb_discharge_cms": "discharge_cms"})

    # For each observation, snap to the closest SRC stage and copy that row's geometry.
    for idx, row in df_n.iterrows():
        if row.hydroid not in df_ht["HydroID"].values:
            continue
        hyd = df_ht[(df_ht.HydroID == row.hydroid) & (df_ht.stage > 0)]
        if hyd.empty:
            continue
        pick = hyd.loc[hyd["stage"].sub(row.hand).abs().idxmin()]
        for col, src in (
            ("feature_id", "feature_id"),
            ("LakeID", "LakeID"),
            ("NextDownID", "NextDownID"),
            ("LENGTHKM", "LENGTHKM"),
            ("src_stage", "stage"),
            ("channel_n", "channel_n"),
            ("overbank_n", "overbank_n"),
            ("discharge_cms", "discharge_cms"),
        ):
            df_n.loc[idx, col] = pick[src]

    if "discharge_cms" not in df_n:
        return f"{msg}: SKIP no stage match"

    # Coefficient = Qsrc / Qobs; scale Manning's n and reject out-of-range adjustments.
    df_n = df_n.rename(columns={"hydroid": "HydroID"})
    df_n["hydroid_calb_coef"] = df_n["discharge_cms"] / df_n["flow"]
    df_n["channel_n_calb"] = df_n["hydroid_calb_coef"] * df_n["channel_n"]
    df_n["overbank_n_calb"] = df_n["hydroid_calb_coef"] * df_n["overbank_n"]
    df_n["Mann_flag"] = np.where(
        (df_n["channel_n_calb"] >= ROUGHNESS_MAX_THRESH)
        | (df_n["overbank_n_calb"] >= ROUGHNESS_MAX_THRESH)
        | (df_n["channel_n_calb"] <= ROUGHNESS_MIN_THRESH)
        | (df_n["overbank_n_calb"] <= ROUGHNESS_MIN_THRESH)
        | (df_n["hydroid_calb_coef"].isnull()),
        "Fail",
        "Pass",
    )

    # layer encodes "_<lid>____<magnitude>-year"; split out lid for metadata.
    df_n["magnitude"] = df_n["layer"].str.split("_").str[5]
    df_n["ahps_lid"] = df_n["layer"].str.split("_").str[1]
    df_n = df_n.drop(["layer"], axis=1)

    if debug_outputs:
        df_n.to_csv(
            Path(branch_dir) / f"{calb_type}_src_calcs_{branch_id}.csv", index=False
        )

    df_n = df_n[df_n["Mann_flag"] == "Pass"]
    if df_n.empty:
        return f"{msg}: no valid coefficients after filtering"

    # Most-recent submitter + median coefficient per HydroID.
    df_updated = (
        df_n[["HydroID", "coll_time", "submitter", "ahps_lid"]]
        .sort_values("coll_time")
        .drop_duplicates(["HydroID"], keep="last")
        .rename(columns={"coll_time": "last_updated"})
    )
    df_mann = df_n.groupby(["HydroID"])[["hydroid_calb_coef"]].median()

    df_ht = df_ht.rename(columns={"discharge_cms": "precalb_discharge_cms"})

    # One row per HydroID for network tracing; lakes excluded from the trace.
    df_m = df_ht[
        ["HydroID", "feature_id", "NextDownID", "LENGTHKM", "LakeID", "order_"]
    ].drop_duplicates(["HydroID"], keep="first")
    if df_m.loc[df_m["LakeID"] == -999].empty:
        return f"{msg}: SKIP all-lake branch"

    df_m = branch_network_tracer(df_m)
    df_m = df_m.merge(df_mann, how="left", on="HydroID").merge(
        df_updated, how="left", on="HydroID"
    )
    df_m = group_manningn_calc(df_m, down_dist_thresh)

    # Borrow a feature_id-level mean for HydroIDs that share a calibrated feature_id.
    feat = (
        df_m.groupby(["feature_id"])[["hydroid_calb_coef"]]
        .mean()
        .rename(columns={"hydroid_calb_coef": "featid_calb_coef"})
    )
    feat_attr = df_m.groupby("feature_id").first()
    feat_attr = feat_attr[feat_attr["submitter"].notna()][["last_updated", "submitter"]]
    df_m = df_m.merge(feat, how="left", on="feature_id").set_index("feature_id")
    df_m = df_m.combine_first(feat_attr).reset_index()

    if df_m["hydroid_calb_coef"].isnull().all():
        return f"{msg}: no valid hydroid coefficients (lakes only)"

    # Priority: direct hydroid coef, else feature_id mean, else downstream group mean.
    conditions = [
        (df_m["hydroid_calb_coef"].isnull()) & (df_m["featid_calb_coef"].notnull()),
        (df_m["hydroid_calb_coef"].isnull())
        & (df_m["featid_calb_coef"].isnull())
        & (df_m["group_calb_coef"].notnull()),
    ]
    df_m[calb_type] = np.select(
        conditions,
        [df_m["featid_calb_coef"], df_m["group_calb_coef"]],
        default=df_m["hydroid_calb_coef"],
    )
    df_m["obs_source"] = np.where(df_m[calb_type].notnull(), source_tag, pd.NA)
    df_m = df_m.drop(
        ["feature_id", "NextDownID", "LENGTHKM", "LakeID", "order_"],
        axis=1,
        errors="ignore",
    )

    # Blend in retained prior adjustments where this source left a HydroID uncalibrated.
    if not df_prev.empty:
        df_m = pd.merge(df_m, df_prev, on="HydroID", how="outer")
        take_prev = df_m[calb_type].isnull() & df_m["calb_coef_final_prev"].notnull()
        df_m["submitter"] = np.where(
            take_prev, df_m["submitter_prev"], df_m["submitter"]
        )
        df_m["last_updated"] = np.where(
            take_prev, df_m["last_updated_prev"], df_m["last_updated"]
        )
        df_m["obs_source"] = np.where(
            take_prev, df_m["obs_source_prev"], df_m["obs_source"]
        )
        df_m["calb_coef_final"] = np.where(
            take_prev, df_m["calb_coef_final_prev"], df_m[calb_type]
        )
        df_m = df_m.drop(
            [
                "submitter_prev",
                "last_updated_prev",
                "calb_coef_final_prev",
                "obs_source_prev",
            ],
            axis=1,
            errors="ignore",
        )
    else:
        df_m["calb_coef_final"] = df_m[calb_type]

    _update_catchments(catchments_poly_path, df_m)

    if debug_outputs:
        df_m.to_csv(
            Path(branch_dir) / f"{calb_type}_merge_vals_{branch_id}.csv", index=False
        )

    # Merge coefficients back, flag applied rows, recompute discharge.
    df_m = df_m.drop(
        [
            "ahps_lid",
            "start_catch",
            "route_count",
            "branch_id",
            "hydroid_calb_coef",
            "featid_calb_coef",
            "group_calb_coef",
            "src_calibrated",
        ],
        axis=1,
        errors="ignore",
    )
    df_ht = df_ht.merge(df_m, how="left", on="HydroID")
    df_ht["calb_applied"] = np.where(
        df_ht["calb_coef_final"].notnull(), "True", "False"
    )
    df_ht["discharge_cms"] = np.where(
        df_ht["calb_coef_final"].isnull(),
        df_ht["precalb_discharge_cms"],
        df_ht["precalb_discharge_cms"] / df_ht["calb_coef_final"],
    )
    # Preserve sentinel discharges (thalweg-notch workaround) untouched.
    df_ht["discharge_cms"] = df_ht["discharge_cms"].mask(
        df_ht["precalb_discharge_cms"] == 0.0, 0.0
    )
    df_ht["discharge_cms"] = df_ht["discharge_cms"].mask(
        df_ht["precalb_discharge_cms"] == -999, -999
    )

    df_ht.to_csv(Path(branch_dir) / f"hydroTable_{branch_id}.csv", index=False)
    n_applied = int((df_ht.drop_duplicates("HydroID")["calb_applied"] == "True").sum())
    return f"{msg}: OK ({n_applied} HydroIDs calibrated)"


def _update_catchments(catchments_poly_path: Path, df_m: pd.DataFrame) -> None:
    # Tag the catchments .gpkg with src_calibrated / obs_source / calb_coef_final.
    path = Path(catchments_poly_path)
    if not path.is_file():
        return
    import geopandas as gpd

    try:
        cat = gpd.read_file(path)
        cat = cat.drop(
            ["src_calibrated", "obs_source", "calb_coef_final"], axis=1, errors="ignore"
        )
        df_m["src_calibrated"] = np.where(
            df_m["calb_coef_final"].notnull(), "True", "False"
        )
        out = cat.merge(
            df_m[["HydroID", "src_calibrated", "obs_source", "calb_coef_final"]],
            how="left",
            on="HydroID",
        )
        out["src_calibrated"] = out["src_calibrated"].fillna("False")
        out.to_file(path, driver="GPKG", index=False, engine="fiona")
    except Exception as exc:  # a viz-only attribute write must not sink the calibration
        log.warning(f"catchments gpkg not updated ({path.name}): {exc}")


def branch_network_tracer(df_ht: pd.DataFrame) -> pd.DataFrame:
    # Rank HydroIDs upstream->downstream per branch so coefficients can flow down.
    df_ht = df_ht.astype({"NextDownID": "int64"})
    df_ht = df_ht.loc[df_ht["LakeID"] == -999]
    df_ht["start_catch"] = ~df_ht["HydroID"].isin(df_ht["NextDownID"])
    df_ht = df_ht.set_index("HydroID", drop=False)

    heads = deque(df_ht[df_ht["start_catch"]]["HydroID"].tolist())
    visited: set = set()
    branch_count = 0
    while heads:
        hid = heads.popleft()
        q = deque(df_ht[df_ht["HydroID"] == hid]["HydroID"].tolist())
        vert = 0
        branch_count += 1
        while q:
            cur = q.popleft()
            if cur in visited:
                continue
            df_ht.loc[df_ht.HydroID == cur, "route_count"] = vert
            df_ht.loc[df_ht.HydroID == cur, "branch_id"] = branch_count
            vert += 1
            visited.add(cur)
            nextid = df_ht.loc[cur, "NextDownID"]
            order = df_ht.loc[cur, "order_"]
            if nextid not in visited and nextid in df_ht.HydroID:
                # Stop at a confluence where flow order increases (a new branch head).
                confluence = (df_ht.NextDownID == nextid).sum() > 1
                if df_ht.loc[nextid, "order_"] > order and confluence:
                    heads.append(nextid)
                    continue
                q.append(nextid)
    df_ht = df_ht.reset_index(drop=True).sort_values(["branch_id", "route_count"])
    return df_ht


def group_manningn_calc(df_m: pd.DataFrame, down_dist_thresh: float) -> pd.DataFrame:
    # Running-mean coefficient over consecutive calibrated HydroIDs, carried to the
    # first down_dist_thresh km of uncalibrated reaches below them.
    dist_accum = 0.0
    hyid_count = 0
    hyid_accum_count = 0
    run_accum = 0.0
    group_coef = 0.0
    branch_start = 1
    for idx, row in df_m.iterrows():
        if int(df_m.loc[idx, "branch_id"]) != branch_start:
            dist_accum = hyid_count = hyid_accum_count = 0
            run_accum = group_coef = 0.0
            branch_start = int(df_m.loc[idx, "branch_id"])
        if np.isnan(df_m.loc[idx, "hydroid_calb_coef"]):
            df_m.loc[idx, "accum_dist"] = row["LENGTHKM"] + dist_accum
            dist_accum += row["LENGTHKM"]
            hyid_count = 0
            df_m.loc[idx, "hyid_accum_count"] = hyid_accum_count
            if dist_accum < down_dist_thresh and hyid_accum_count > 1:
                df_m.loc[idx, "group_calb_coef"] = group_coef
        else:
            dist_accum = 0.0
            hyid_count += 1
            df_m.loc[idx, "accum_dist"] = 0
            if hyid_count == 1:
                run_accum = 0.0
                hyid_accum_count = 0
            group_coef = (row["hydroid_calb_coef"] + run_accum) / float(hyid_count)
            df_m.loc[idx, "group_calb_coef"] = group_coef
            df_m.loc[idx, "hyid_count"] = hyid_count
            run_accum += row["hydroid_calb_coef"]
            hyid_accum_count += 1
            df_m.loc[idx, "hyid_accum_count"] = hyid_accum_count

    return df_m.drop(
        ["hyid_count", "accum_dist", "hyid_accum_count"], axis=1, errors="ignore"
    )
