"""
Author: Supath Dhital
Date Updated: June 2026

SRC calibration against observed data.

ManualCalibrator applies a per-feature_id coefficient. UsgsRatingCalibrator and
SpatialObsCalibrator drive the shared optimization engine (src_optimization.py)
from USGS rating curves and benchmark inundation points respectively. All three
recompute discharge per HydroID; ras2fim is left as a stub.
"""

from __future__ import annotations

import glob
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ._common import (
    HYDROTABLE_DTYPES,
    PathLike,
    aoi_id_of,
    iter_branches,
    not_yet_ported,
    read_table,
    resolve_aoi_dir,
)
from .src_adjust import _run_branches
from .src_optimization import (
    DOWNSTREAM_THRESHOLD,
    USGS_CALB_TRACE_DIST,
    update_rating_curve,
)

log = logging.getLogger(__name__)

# USGS gage acceptance filter — coordinate / altitude / site-type codes that
# qualify a gage's rating curve for SRC calibration.
_ACC_COORD = ["H", "1", "5", "S", "R", "B", "C", "D", "E", 5, 1]
_ACC_METHOD = ["C", "D", "W", "X", "Y", "Z", "N", "M", "L", "G", "R", "F", "S"]
_ACC_ALT_METHOD = ["A", "D", "F", "I", "J", "L", "N", "R", "W", "X", "Y", "Z"]
_ACC_SITE_TYPE = ["ST"]
_ACC_ALT_THRESH = 1.0

# NWM recurrence intervals sampled from each observed rating curve.
_RECUR_INTERVALS = ("2", "5", "10", "25", "50")


# Manual calibration
@dataclass
class ManualCalibrator:
    # discharge = pre_manual / calb_coef_manual per feature_id (coef<1 raises
    # discharge, >1 lowers it). Backs up each hydroTable first.

    aoi_dir: PathLike
    calibration_file: PathLike

    def run(self) -> None:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        calib_path = Path(self.calibration_file)
        if not calib_path.exists():
            raise FileNotFoundError(f"No calibration file at {calib_path}")

        aoi_id = aoi_id_of(aoi_dir)
        log.info(f"--- ManualCalibrator: {aoi_id} ---")

        calib = pd.read_csv(calib_path, index_col=False)
        if "Unnamed: 0" in calib.columns:
            calib = calib.drop(columns=["Unnamed: 0"])

        # Accept aoi_id (preferred) or HUC8 (legacy) as the AOI key column.
        if "aoi_id" in calib.columns:
            calib["aoi_id"] = calib["aoi_id"].astype(str)
        elif "HUC8" in calib.columns:
            calib = calib.rename(columns={"HUC8": "aoi_id"})
            calib["aoi_id"] = calib["aoi_id"].astype(str)
        else:
            raise ValueError(
                "Manual calibration CSV must have either an 'aoi_id' or 'HUC8' column."
            )
        calib["feature_id"] = calib["feature_id"].astype(int)
        calib["calb_coef_manual"] = calib["calb_coef_manual"].astype(float)

        min_coef = calib["calb_coef_manual"].min()
        if min_coef <= 0:
            raise ValueError(
                f"Manual calibration coefficients must be > 0. Minimum found: {min_coef}"
            )

        if aoi_id not in calib["aoi_id"].unique():
            log.info(f"No manual calibration entry for AOI {aoi_id} — skipping")
            return

        branches_dir = aoi_dir / "branches"
        htable_files = sorted(
            Path(p)
            for p in glob.glob(
                str(branches_dir / "**" / "hydroTable_*.csv"), recursive=True
            )
        )
        for ht_file in htable_files:
            self._apply(ht_file, calib)

    @staticmethod
    def _apply(ht_file: Path, calib: pd.DataFrame) -> None:
        backup = ht_file.with_name(
            ht_file.name.replace("hydroTable_", "htable_pre_manual_calib_")
        )
        shutil.copyfile(ht_file, backup)

        df = pd.read_csv(ht_file, dtype=HYDROTABLE_DTYPES, index_col=False)
        for col in ("postcalb_discharge_cms", "calb_coef_manual", "HydroID Int16"):
            if col in df.columns:
                df = df.drop(columns=[col])

        df["pre_manual_calb_discharge_cms"] = df["discharge_cms"]
        df = df.merge(calib, how="left", on="feature_id")
        df = df.drop(columns=["aoi_id", "HUC8"], errors="ignore")

        df["discharge_cms"] = np.where(
            df["calb_coef_manual"].isnull(),
            df["pre_manual_calb_discharge_cms"],
            df["pre_manual_calb_discharge_cms"] / df["calb_coef_manual"],
        )
        df.to_csv(ht_file, index=False)
        log.info(f"Applied manual calibration --> {ht_file.name}")


def manual_calibration(
    aoi_dir: Optional[PathLike] = None,
    calibration_file: Optional[PathLike] = None,
    *,
    huc_dir: Optional[PathLike] = None,
) -> None:
    if calibration_file is None:
        raise TypeError("calibration_file= is required.")
    ManualCalibrator(
        aoi_dir=resolve_aoi_dir(aoi_dir, huc_dir),
        calibration_file=calibration_file,
    ).run()


# USGS rating-curve calibration
def _filter_usgs_sites(sites: pd.DataFrame) -> pd.DataFrame:
    # Keep gages whose coordinate / altitude / site-type codes meet the bar.
    ok_codes = (
        sites["usgs_data_coord_accuracy_code"].isin(_ACC_COORD)
        & sites["usgs_data_coord_method_code"].isin(_ACC_METHOD)
        & sites["usgs_data_alt_method_code"].isin(_ACC_ALT_METHOD)
        & sites["usgs_data_site_type"].isin(_ACC_SITE_TYPE)
    )
    sites = sites.astype({"usgs_data_alt_accuracy_code": float}).copy()
    ok_alt = sites["usgs_data_alt_accuracy_code"] <= _ACC_ALT_THRESH
    return sites[ok_codes & ok_alt]


def _read_sites(path: Path) -> pd.DataFrame:
    # Acceptable-gage metadata: CSV, Parquet, or a gage .gpkg (attributes only).
    if path.suffix.lower() in (".gpkg", ".shp"):
        import geopandas as gpd

        return pd.DataFrame(
            gpd.read_file(path).drop(columns="geometry", errors="ignore")
        )
    return read_table(path, dtype={"location_id": object})


def _build_usgs_database(
    usgs_rc_csv: Path,
    usgs_sites_csv: Path,
    usgs_elev_df: pd.DataFrame,
    nwm_recur_file: Path,
    aoi_id: str,
) -> pd.DataFrame:
    # Crosswalk USGS ratings to HydroIDs, convert to HAND, and sample the rating
    # at each NWM recurrence flow. Returns one row per (gage, branch, interval).
    rc = read_table(
        usgs_rc_csv,
        dtype={"location_id": object},
        usecols=["location_id", "flow", "stage", "elevation_navd88"],
    )
    rc["location_id"] = rc["location_id"].astype(str)
    # Acceptable-gage list is optional: when given, keep only gages that pass
    # the quality filter; otherwise use every gage in the rating-curve file.
    if usgs_sites_csv is not None and Path(usgs_sites_csv).is_file():
        sites = _read_sites(Path(usgs_sites_csv))
        sites["location_id"] = sites["location_id"].astype(str)
        keep = _filter_usgs_sites(sites)["location_id"].drop_duplicates().tolist()
        rc = rc[rc["location_id"].isin(keep)]
    rc["elevation_navd88_m"] = rc["elevation_navd88"] / 3.28084
    rc["discharge_cms"] = rc["flow"] / 35.3147
    rc = rc.drop(columns=["flow"])

    # HUC8 labels the gage's basin; fall back to the AOI id when absent.
    elev = usgs_elev_df.copy()
    if "HUC8" not in elev.columns:
        elev["HUC8"] = aoi_id
    cross = elev[
        [
            "location_id",
            "HydroID",
            "feature_id",
            "levpa_id",
            "HUC8",
            "dem_adj_elevation",
        ]
    ].rename(
        columns={"dem_adj_elevation": "hand_datum", "HydroID": "hydroid", "HUC8": "huc"}
    )
    cross["location_id"] = cross["location_id"].astype(str)
    cross = cross[cross.location_id.notnull()]

    rc = rc.merge(cross, how="left", on="location_id")
    rc = rc[rc["hydroid"].notna()]
    if rc.empty:
        return pd.DataFrame()

    rc["hand"] = rc["elevation_navd88_m"] - rc["hand_datum"]
    rc = rc[
        [
            "location_id",
            "feature_id",
            "hydroid",
            "levpa_id",
            "huc",
            "hand",
            "discharge_cms",
        ]
    ]
    rc["feature_id"] = rc["feature_id"].astype(int)

    recur = read_table(nwm_recur_file, dtype={"feature_id": int})
    recur = recur.drop(columns=["Unnamed: 0"], errors="ignore").rename(
        columns={
            f"{i}_0_year_recurrence_flow_17C": f"{i}_0_year" for i in (2, 5, 10, 25, 50)
        }
    )
    year_cols = [f"{i}_0_year" for i in (2, 5, 10, 25, 50)]
    recur[year_cols] = recur[year_cols].astype("float64") * 0.028317  # cfs -> cms

    merge = rc.merge(recur, how="left", on="feature_id")
    final = pd.DataFrame()
    for interval in _RECUR_INTERVALS:
        col = f"{interval}_0_year"
        merge["Q_find"] = (merge["discharge_cms"] - merge[col]).abs().fillna(999999)
        calc = merge.loc[
            merge.groupby(["location_id", "levpa_id"])["Q_find"].idxmin()
        ].reset_index(drop=True)
        calc["check_variance"] = (
            (calc["discharge_cms"] - calc[col]) / calc["discharge_cms"]
        ).abs()
        calc["nwm_recur"] = col
        calc["layer"] = f"_usgs-gage____{interval}-year"
        calc = calc.rename(columns={col: "nwm_recur_flow_cms"})[
            [
                "location_id",
                "hydroid",
                "feature_id",
                "levpa_id",
                "huc",
                "hand",
                "discharge_cms",
                "check_variance",
                "nwm_recur_flow_cms",
                "nwm_recur",
                "layer",
            ]
        ]
        final = pd.concat([final, calc], ignore_index=True)
        # Drop negative-HAND and >10%-variance samples so we sample at known flows.
        final = final[(final["hand"] > 0) & (final["check_variance"] < 0.1)]
        final["submitter"] = "usgs_rating_" + final["location_id"].astype(str)
        final["coll_time"] = "usgs_rating"

    return final.rename(columns={"discharge_cms": "flow"})


def _trace_network(df: pd.DataFrame, start_id: int):
    # Walk up to USGS_CALB_TRACE_DIST km up and down from a gage HydroID,
    # staying on the same stream order and off lakes.
    up, down = [], []
    start_order = None
    # Downstream walk along NextDownID.
    cur, accum = start_id, 0.0
    while True:
        row = df[df["HydroID"] == cur]
        if row.empty:
            break
        order = row["order_"].values[0]
        if start_order is None:
            start_order = order
        if order != start_order:
            break
        accum += row["LengthKm"].values[0]
        if accum >= USGS_CALB_TRACE_DIST or row["LakeID"].values[0] > 0:
            break
        down.append(int(cur))
        cur = row["NextDownID"].values[0]
    # Upstream walk: reaches whose NextDownID points back at the current id.
    cur, accum = start_id, 0.0
    while True:
        row = df[(df["NextDownID"] == cur) & (df["order_"] == start_order)]
        if row.empty:
            break
        accum += row["LengthKm"].values[0]
        if accum >= USGS_CALB_TRACE_DIST or row["LakeID"].values[0] > 0:
            break
        if cur != start_id:
            up.append(cur)
        cur = row["HydroID"].values[0]
    return up, down


def _usgs_one_branch(
    branch_dir: Path, bid: str, usgs_df: pd.DataFrame, debug: bool
) -> str:
    # Trace each gage's reach neighborhood, then run the optimization engine.
    import geopandas as gpd

    htable = branch_dir / f"hydroTable_{bid}.csv"
    catch = (
        branch_dir
        / f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{bid}.gpkg"
    )
    reaches = (
        branch_dir
        / f"demDerived_reaches_split_filtered_addedAttributes_crosswalked_{bid}.gpkg"
    )
    if not htable.is_file() or not reaches.is_file():
        return "SKIP missing hydroTable/reaches"

    gages = usgs_df[usgs_df["levpa_id"].astype(str) == str(bid)]
    if gages.empty:
        return "SKIP no gages in branch"

    net = gpd.read_file(reaches)[
        ["HydroID", "order_", "LengthKm", "NextDownID", "LakeID"]
    ]
    net["HydroID"] = net["HydroID"].astype(int)
    net["NextDownID"] = net["NextDownID"].astype(int)

    gages = gages.copy()
    for idx, row in gages.iterrows():
        up, down = _trace_network(net, int(row["hydroid"]))
        gages.loc[idx, "trace"] = ",".join(map(str, up + down))

    # Expand the gage rows out to every traced HydroID in their neighborhood.
    gages["trace"] = (
        gages["trace"].fillna("").apply(lambda s: [int(x) for x in s.split(",") if x])
    )
    traced = gages.explode("trace")
    traced = traced[traced["trace"].notnull() & (traced["trace"] != 0)]
    if traced.empty:
        return "SKIP no traceable hydroids"
    traced = traced.rename(columns={"hydroid": "hydroid_gauge", "trace": "hydroid"})

    return update_rating_curve(
        branch_dir,
        traced,
        htable,
        str(traced["huc"].iloc[0]),
        bid,
        catch,
        debug,
        "usgs_rating",
        merge_prev_adj=False,
    )


@dataclass
class UsgsRatingCalibrator:
    # Calibrate from USGS rating curves at NWM recurrence flows: crosswalk gages
    # via usgs_elev_table.csv, trace each gage's reaches, run the engine.

    aoi_dir: PathLike
    usgs_rating_curve_csv: PathLike
    nwm_recur_file: PathLike
    usgs_acceptable_gages: Optional[PathLike] = None  # optional quality filter
    n_workers: int = 1
    debug_outputs: bool = False

    def run(self) -> dict[str, str]:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        aoi_id = aoi_id_of(aoi_dir)

        # Rating curve + recurrence flows are required; the acceptable-gage list
        # is optional (unfiltered when absent).
        for f in (self.usgs_rating_curve_csv, self.nwm_recur_file):
            if f is None or not Path(f).is_file():
                log.info(f"UsgsRatingCalibrator: input absent — skipping ({f})")
                return {}
        elev = aoi_dir / "usgs_elev_table.csv"
        if not elev.is_file():
            log.info(
                f"UsgsRatingCalibrator: usgs_elev_table.csv absent at {aoi_dir} — skipping"
            )
            return {}

        usgs_elev_df = pd.read_csv(elev, dtype={"location_id": object})
        usgs_df = _build_usgs_database(
            Path(self.usgs_rating_curve_csv),
            self.usgs_acceptable_gages,
            usgs_elev_df,
            Path(self.nwm_recur_file),
            aoi_id,
        )
        if usgs_df.empty:
            log.info(
                f"UsgsRatingCalibrator: no acceptable gages crosswalked for {aoi_id} — skipping"
            )
            return {}
        # levpa_id arrives as float (0.0) from the merge; normalize to the
        # bare branch-folder form ("0", "1123000014").
        usgs_df["levpa_id"] = usgs_df["levpa_id"].astype("int64").astype(str)

        branches = list(iter_branches(aoi_dir, exclude_zero=False))
        log.info(
            f"UsgsRatingCalibrator: {aoi_id} ({len(branches)} branches, {self.n_workers} workers)"
        )
        return _run_branches(
            branches,
            _usgs_one_branch,
            (usgs_df, self.debug_outputs),
            self.n_workers,
            "UsgsRatingCalibrator",
        )


# Spatial observation calibration
def _spatial_one_branch(branch_dir: Path, bid: str, points, debug: bool) -> str:
    # Sample HAND + HydroID rasters at each obs point, then run the engine.
    import geopandas as gpd
    import rasterio

    htable = branch_dir / f"hydroTable_{bid}.csv"
    catch = (
        branch_dir
        / f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{bid}.gpkg"
    )
    hand = branch_dir / f"rem_zeroed_masked_{bid}.tif"
    cat_rast = branch_dir / f"gw_catchments_reaches_filtered_addedAttributes_{bid}.tif"
    prefix_path = branch_dir / "hydroid_prefix.txt"
    if not htable.is_file() or not hand.is_file() or not cat_rast.is_file():
        return "SKIP missing hydroTable/HAND/catchments raster"

    pts = points.to_crs(_fim_crs())
    coords = [(x, y) for x, y in zip(pts.geometry.x, pts.geometry.y)]
    if not coords:
        return "SKIP no points"

    process_int16 = prefix_path.is_file()
    int_hid_prefix = (
        int(prefix_path.read_text().strip()) * 10000 if process_int16 else 0
    )

    with rasterio.open(hand) as h, rasterio.open(cat_rast) as c:
        pts["hand"] = (
            [np.float32(v[0]) / 1000 for v in h.sample(coords)]
            if process_int16
            else [v[0] for v in h.sample(coords)]
        )
        hids = []
        for v in c.sample(coords):
            val = abs(v[0])
            hids.append(int_hid_prefix + val if process_int16 else val)
        pts["hydroid"] = hids

    pts = pts[
        (pts["hydroid"].notnull())
        & (pts["hand"] > 0)
        & (pts["hand"] != 32.767)
        & (pts["hydroid"] > int_hid_prefix)
    ]
    if pts.empty:
        return "SKIP no valid points in branch"

    med = (
        pts.groupby(
            ["hydroid", "flow", "submitter", "coll_time", "flow_unit", "layer"]
        )["hand"]
        .median()
        .reset_index()
    )
    med["coll_time"] = med["coll_time"].astype(str)

    return update_rating_curve(
        branch_dir,
        med,
        htable,
        "spatial",
        bid,
        catch,
        debug,
        "point_obs",
        merge_prev_adj=True,
    )


def _fim_crs() -> str:
    # Project obs points to the FIM grid CRS before sampling rasters.
    import os

    return os.getenv("DEFAULT_FIM_PROJECTION_CRS", "EPSG:5070")


@dataclass
class SpatialObsCalibrator:
    # Calibrate from benchmark inundation points (per-AOI parquet): sample
    # HAND/HydroID rasters at each point, run the engine, blend with prior USGS.

    aoi_dir: PathLike
    calib_points_file: Optional[PathLike] = None
    n_workers: int = 1
    down_dist_thresh: float = DOWNSTREAM_THRESHOLD
    debug_outputs: bool = False

    def run(self) -> dict[str, str]:
        import geopandas as gpd

        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        aoi_id = aoi_id_of(aoi_dir)

        if self.calib_points_file is None or not Path(self.calib_points_file).is_file():
            log.info(
                f"SpatialObsCalibrator: calib points absent — skipping ({self.calib_points_file})"
            )
            return {}

        points = gpd.read_parquet(self.calib_points_file)
        if points.empty:
            log.info(
                f"SpatialObsCalibrator: no points in {self.calib_points_file} — skipping"
            )
            return {}

        branches = list(iter_branches(aoi_dir, exclude_zero=False))
        log.info(
            f"SpatialObsCalibrator: {aoi_id} ({len(branches)} branches, {self.n_workers} workers)"
        )
        return _run_branches(
            branches,
            _spatial_one_branch,
            (points, self.debug_outputs),
            self.n_workers,
            "SpatialObsCalibrator",
        )


# ras2fim — not ported yet (needs a ras_elev_table; shares the same engine)
@dataclass
class Ras2fimCalibrator:
    aoi_dir: PathLike
    ras_rating_curve_csv: PathLike
    nwm_recur_file: PathLike
    n_workers: int = 1

    def run(self) -> None:
        not_yet_ported("Ras2fimCalibrator")
