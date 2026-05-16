"""
Author: Supath Dhital
Date Updated: May 2026

USGS / AHPS / RAS2FIM gage crosswalk to HAND branches.

This module is the fimbox port of inundation-mapping's two-script chain:

    usgs_gage_unit_setup.py   --> assigns gages to NWM feature_id + levpa_id
    usgs_gage_crosswalk.py    --> snaps each gage to its branch thalweg and
                                  samples DEM / thalweg-conditioned DEM at the
                                  snapped point

The output (``usgs_elev_table.csv``) provides the DEM-elevation reference
that lets the Stage IV calibration / SRC-adjustment step convert observed
gage stages into HAND stages — i.e. it is the per-branch bridge between
real-world gage readings and the synthetic rating curve.

Pipeline
--------
Stage 1 -- AOI level (``assign_gages_to_branches``)
    1. Read USGS gage points (and optional RAS2FIM cross-sections + AHPS sites)
    2. Filter to this AOI's identifier (default column is ``HUC8`` because
       upstream USGS gage tables ship that schema; override via
       ``aoi_filter_column`` for non-HUC AOIs)
    3. For any gage missing ``feature_id``, snap to the nearest NWM reach
    4. Left-join the NWM reach attributes so each gage carries
       ``feature_id`` and ``levpa_id`` (level path = branch id)
    5. Write ``usgs_subset_gages.gpkg``  (one row per gage, AOI-wide)

Stage 2 -- branch level (``run_branch_crosswalk``)
    1. Filter ``usgs_subset_gages.gpkg`` to the rows whose ``levpa_id`` matches
       this branch (branch zero gets a copy with ``levpa_id`` overwritten to "0")
    2. Spatial join to the branch's filtered catchments to attach HydroID
    3. Snap each gage to the nearest DEM-derived flow line (HydroID match)
    4. Sample two rasters at the snapped point:
         dem_meters_{id}.tif         --> ``dem_elevation``
         dem_thalwegCond_{id}.tif    --> ``dem_adj_elevation``
    5. Write ``usgs_elev_table.csv`` (USGS + AHPS rows)
       and ``ras_elev_table.csv``   (RAS2FIM rows, when present)

Inputs
------
USGS gage points GeoPackage
    Columns required: ``location_id``, the configured AOI filter column
    (default ``HUC8``), ``feature_id`` (may be null), ``nws_lid`` (optional),
    geometry.

NWM streams with level-path attribute
    Output of ``BranchDerivation``: ``nwm_subset_streams_levelPaths.gpkg``.
    Must contain ``ID`` / ``feature_id``, ``levpa_id``, ``order_``.

Filtered catchments GeoPackage
    Per-branch:
    ``gw_catchments_reaches_filtered_addedAttributes_crosswalked_{id}.gpkg``.

Branch flowline GeoPackage
    Per-branch: ``demDerived_reaches_split_filtered_{id}.gpkg``.

DEM rasters (per branch)
    ``dem_meters_{id}.tif`` and ``dem_thalwegCond_{id}.tif``.

Optional
    RAS2FIM points (``ras_rating_curve.gpkg``) and AHPS sites (``nws_lid.gpkg``).

Outputs
-------
AOI-level
    ``usgs_subset_gages.gpkg``
        All gages within the AOI, with ``feature_id`` and ``levpa_id`` assigned.
    ``usgs_subset_gages_{branch_zero_id}.gpkg``
        Same content with ``levpa_id`` overwritten to the branch-zero id; used
        by the branch-zero crosswalk pass.

Branch-level (written into the branch directory)
    ``usgs_elev_table.csv``
        One row per USGS / AHPS gage in this branch with columns
        ``location_id, HydroID, feature_id, levpa_id, <aoi_filter_column>,
        dem_elevation, dem_adj_elevation, source, snap_distance``. The AOI
        filter column is carried through verbatim (default ``HUC8``).
    ``ras_elev_table.csv``
        Same shape but for RAS2FIM cross-sections (only written when present).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import pandas as pd
import rasterio

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


# Stage 1 -- AOI-level gage-to-branch assignment
@dataclass
class GageBranchAssignment:
    """Result of ``assign_gages_to_branches``."""

    aoi_gages: gpd.GeoDataFrame  # all gages with feature_id + levpa_id
    aoi_gages_path: Path  # usgs_subset_gages.gpkg
    branch_zero_gages_path: Path  # usgs_subset_gages_{bzero_id}.gpkg


def assign_gages_to_branches(
    usgs_gages_gpkg: PathLike,
    nwm_streams_levelpaths_gpkg: PathLike,
    aoi_id: Optional[str] = None,
    out_dir: Optional[PathLike] = None,
    *,
    huc_id: Optional[str] = None,
    huc8: Optional[str] = None,
    target_crs: Union[str, int] = 5070,
    branch_zero_id: str = "0",
    ras_locs_gpkg: Optional[PathLike] = None,
    ahps_gpkg: Optional[PathLike] = None,
    out_name: str = "usgs_subset_gages.gpkg",
    aoi_filter_column: str = "HUC8",
) -> Optional[GageBranchAssignment]:
    """
    Stage 1: build the AOI-wide gage table with ``feature_id`` and ``levpa_id``.

    Returns ``None`` when no gages fall inside the AOI (in which case neither
    output file is written -- mirrors the bash short-circuit).

    The AOI identifier may be passed as ``aoi_id``, ``huc_id``, or ``huc8`` —
    they are equivalent. Pass whichever name best matches your data.

    The ``aoi_filter_column`` argument names the column in the USGS / AHPS
    layers that identifies which AOI each gage belongs to. Default ``"HUC8"``
    matches the upstream USGS gage schema; pass a different column name when
    your gage tables use a custom AOI key.
    """
    # Resolve any of the three identifier kwargs to a single value.
    aoi_id = next((x for x in (aoi_id, huc_id, huc8) if x is not None), None)
    if aoi_id is None:
        raise TypeError("Provide one of aoi_id=, huc_id=, or huc8=.")
    if out_dir is None:
        raise TypeError("out_dir= is required.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    gages = _load_aoi_gages(
        usgs_gages_gpkg, aoi_id, target_crs, ras_locs_gpkg, ahps_gpkg, aoi_filter_column
    )
    if gages.empty:
        log.warning(f"No gages identified for AOI {aoi_id}")
        return None

    gages = _attach_feature_id_and_levelpath(gages, nwm_streams_levelpaths_gpkg)

    aoi_gages_path = out_dir / out_name
    gages.to_file(aoi_gages_path, driver="GPKG", index=False)
    log.info(f"AOI gages ({len(gages)} features) --> {aoi_gages_path.name}")

    # Branch-zero copy: every gage points to branch zero regardless of its
    # derived level path. Used when running the crosswalk against branch 0,
    # which contains the full NWM network.
    bzero = gages.copy()
    bzero["levpa_id"] = branch_zero_id
    bzero_path = out_dir / f"{Path(out_name).stem}_{branch_zero_id}.gpkg"
    bzero.to_file(bzero_path, driver="GPKG", index=False)
    log.info(f"Branch-zero gages --> {bzero_path.name}")

    return GageBranchAssignment(
        aoi_gages=gages,
        aoi_gages_path=aoi_gages_path,
        branch_zero_gages_path=bzero_path,
    )


def _load_aoi_gages(
    usgs_gages_gpkg: PathLike,
    aoi_id: str,
    target_crs: Union[str, int],
    ras_locs_gpkg: Optional[PathLike],
    ahps_gpkg: Optional[PathLike],
    aoi_filter_column: str,
) -> gpd.GeoDataFrame:
    aoi_id = str(aoi_id)
    usgs = gpd.read_file(usgs_gages_gpkg)
    usgs["source"] = "usgs_gage"
    usgs = usgs.to_crs(target_crs)

    frames = [usgs]

    if ras_locs_gpkg and Path(ras_locs_gpkg).exists():
        ras = gpd.read_file(ras_locs_gpkg).to_crs(target_crs)
        # RAS2FIM uses lowercase "huc8"; normalise to the configured column.
        if aoi_filter_column not in ras.columns and "huc8" in ras.columns:
            ras = ras.rename(columns={"huc8": aoi_filter_column})
        if "fid_xs" in ras.columns:
            ras["location_id"] = ras["fid_xs"]
        # collapse MultiPoint -> Point
        ras["geometry"] = ras.representative_point()
        frames.append(ras)

    gages = pd.concat(frames, axis=0, ignore_index=True)
    if aoi_filter_column not in gages.columns:
        raise ValueError(
            f"Gage table does not have the AOI filter column "
            f"{aoi_filter_column!r}; available columns: {sorted(gages.columns)}"
        )
    gages = gages[gages[aoi_filter_column].astype(str) == aoi_id].copy()

    if ahps_gpkg and Path(ahps_gpkg).exists():
        ahps = gpd.read_file(ahps_gpkg)
        ahps = ahps[ahps[aoi_filter_column].astype(str) == aoi_id].copy()
        ahps = ahps.rename(
            columns={"nwm_feature_id": "feature_id", "usgs_site_code": "location_id"}
        ).to_crs(target_crs)
        # only keep AHPS sites not already in the USGS set
        ahps = ahps[ahps["location_id"].isna()]
        ahps["source"] = "ahps_site"
        keep_cols = [
            c
            for c in ("feature_id", "nws_lid", "location_id", aoi_filter_column, "name", "states", "geometry")
            if c in ahps.columns
        ]
        gages = pd.concat([gages, ahps[keep_cols]], ignore_index=True)

    if "nws_lid" in gages.columns:
        gages["location_id"] = gages["location_id"].fillna(gages["nws_lid"])
        gages.loc[gages["nws_lid"] == "Bogus_ID", "nws_lid"] = None

    return gpd.GeoDataFrame(gages, geometry="geometry", crs=target_crs)


def _attach_feature_id_and_levelpath(
    gages: gpd.GeoDataFrame,
    nwm_streams_levelpaths_gpkg: PathLike,
) -> gpd.GeoDataFrame:
    nwm = gpd.read_file(nwm_streams_levelpaths_gpkg).to_crs(gages.crs)
    # match inundation-mapping convention: NWM 'ID' is the feature_id
    if "ID" in nwm.columns and "feature_id" not in nwm.columns:
        nwm = nwm.rename(columns={"ID": "feature_id"})

    missing = gages[gages["feature_id"].isna()] if "feature_id" in gages.columns else gages
    if not missing.empty:
        union = nwm.geometry.union_all()
        gages.loc[missing.index, "feature_id"] = missing.geometry.apply(
            lambda pt: _nearest_feature_id(pt, nwm, union)
        )

    gages["feature_id"] = pd.to_numeric(gages["feature_id"], errors="coerce").astype("Int64")
    keep = [c for c in ("feature_id", "levpa_id", "order_") if c in nwm.columns]
    gages = gages.merge(nwm[keep], on="feature_id", how="left")
    return gages


def _nearest_feature_id(pt, nwm: gpd.GeoDataFrame, union):
    snap = union.interpolate(union.project(pt))
    idx = nwm.geometry.sindex.query(snap)
    if len(idx):
        return int(nwm.iloc[idx[0]]["feature_id"])
    return None


# Stage 2 -- branch-level crosswalk
def run_branch_crosswalk(
    aoi_gages_gpkg: Optional[PathLike] = None,
    branch_catchments_gpkg: Optional[PathLike] = None,
    branch_flows_gpkg: Optional[PathLike] = None,
    dem_path: Optional[PathLike] = None,
    dem_thalweg_path: Optional[PathLike] = None,
    branch_id: Optional[str] = None,
    out_dir: Optional[PathLike] = None,
    *,
    huc_gages_gpkg: Optional[PathLike] = None,
    target_crs: Union[str, int] = 5070,
    huc_crs: Optional[Union[str, int]] = None,
) -> dict[str, Optional[Path]]:
    """
    Stage 2: snap this branch's gages to its DEM-derived thalweg and sample
    the original + thalweg-conditioned DEM to fill ``dem_elevation`` and
    ``dem_adj_elevation``.

    The gage layer may be passed as ``aoi_gages_gpkg`` or ``huc_gages_gpkg``
    (they are equivalent). CRS likewise: ``target_crs`` or ``huc_crs``.

    Returns a dict ``{"usgs_elev_table", "ras_elev_table"}`` of output paths
    (entries are ``None`` when the corresponding table is empty).
    """
    aoi_gages_gpkg = aoi_gages_gpkg if aoi_gages_gpkg is not None else huc_gages_gpkg
    if aoi_gages_gpkg is None:
        raise TypeError("Provide aoi_gages_gpkg= (or huc_gages_gpkg=).")
    if huc_crs is not None:
        target_crs = huc_crs
    for name, val in (
        ("branch_catchments_gpkg", branch_catchments_gpkg),
        ("branch_flows_gpkg", branch_flows_gpkg),
        ("dem_path", dem_path),
        ("dem_thalweg_path", dem_thalweg_path),
        ("branch_id", branch_id),
        ("out_dir", out_dir),
    ):
        if val is None:
            raise TypeError(f"{name}= is required.")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    gages = gpd.read_file(aoi_gages_gpkg)
    gages = gages[gages["levpa_id"].astype(str) == str(branch_id)].copy()
    if gages.empty:
        log.warning(f"No gages for branch {branch_id}")
        return {"usgs_elev_table": None, "ras_elev_table": None}

    gages = gages.to_crs(target_crs)
    gages = _sjoin_catchments(gages, branch_catchments_gpkg, target_crs)
    if gages.empty:
        log.warning(f"No gages intersected branch {branch_id} catchments")
        return {"usgs_elev_table": None, "ras_elev_table": None}

    gages = _snap_to_thalweg(gages, branch_flows_gpkg)
    gages = _sample_raster(gages, dem_path, "dem_elevation")
    gages = _sample_raster(gages, dem_thalweg_path, "dem_adj_elevation")

    log.info(f"Branch {branch_id}: {len(gages)} gage(s) crosswalked")
    return _write_elev_tables(gages, out_dir)


def _sjoin_catchments(
    gages: gpd.GeoDataFrame,
    catchments_gpkg: PathLike,
    target_crs: Union[str, int],
) -> gpd.GeoDataFrame:
    catch = gpd.read_file(catchments_gpkg).to_crs(target_crs)
    cols = [c for c in ("HydroID", "LakeID", "geometry") if c in catch.columns]
    return gpd.sjoin(gages, catch[cols], how="inner", predicate="intersects").drop(
        columns=[c for c in ("index_right",) if c in gages.columns or True],
        errors="ignore",
    )


def _snap_to_thalweg(
    gages: gpd.GeoDataFrame,
    flows_gpkg: PathLike,
) -> gpd.GeoDataFrame:
    flows = gpd.read_file(flows_gpkg).to_crs(gages.crs)
    flows = flows[["HydroID", "geometry"]].rename(columns={"geometry": "geometry_ln"})
    merged = gages.merge(flows, on="HydroID", how="inner")
    if merged.empty:
        return merged.drop(columns=["geometry_ln"], errors="ignore")

    snapped = merged.apply(_snap_row, axis=1, result_type="expand")
    merged["geometry_snapped"] = snapped[0]
    merged["snap_distance"] = snapped[1]
    merged = merged.drop(columns=["geometry_ln"])
    # use snapped geometry going forward so DEM samples come from the thalweg
    return merged.set_geometry("geometry_snapped")


def _snap_row(row):
    line = row["geometry_ln"]
    if line is None or line.is_empty:
        return (None, None)
    snap = line.interpolate(line.project(row.geometry))
    return (snap, snap.distance(row.geometry))


def _sample_raster(
    gages: gpd.GeoDataFrame,
    raster_path: PathLike,
    column: str,
) -> gpd.GeoDataFrame:
    coords = [(geom.x, geom.y) for geom in gages.geometry]
    with rasterio.open(raster_path) as src:
        gages[column] = [val[0] for val in src.sample(coords)]
    return gages


def _write_elev_tables(
    gages: gpd.GeoDataFrame,
    out_dir: Path,
) -> dict[str, Optional[Path]]:
    table = gages.copy()
    if "nws_lid" in table.columns:
        table.loc[table["location_id"] == table["nws_lid"], "location_id"] = None
    table = table[table["location_id"].notna()].copy()
    if "source" in table.columns:
        table["source"] = table["source"].astype(str).str.lower()

    out = {"usgs_elev_table": None, "ras_elev_table": None}

    if "source" in table.columns:
        ras = table[table["source"].str.contains("ras2fim", na=False)]
        usgs = table[~table["source"].str.contains("ras2fim", na=False)]
    else:
        ras = table.iloc[0:0]
        usgs = table

    if not ras.empty:
        ras_cols = [
            c
            for c in (
                "location_id", "HydroID", "feature_id", "levpa_id", "HUC8",
                "dem_elevation", "dem_adj_elevation", "source", "stream_stn",
            )
            if c in ras.columns
        ]
        ras_path = out_dir / "ras_elev_table.csv"
        ras[ras_cols].to_csv(ras_path, index=False)
        log.info(f"RAS2FIM elev table ({len(ras)} rows) --> {ras_path.name}")
        out["ras_elev_table"] = ras_path

    if not usgs.empty:
        usgs_cols = [
            c
            for c in (
                "location_id", "HydroID", "feature_id", "levpa_id", "HUC8",
                "dem_elevation", "dem_adj_elevation", "source", "snap_distance",
            )
            if c in usgs.columns
        ]
        usgs_path = out_dir / "usgs_elev_table.csv"
        usgs[usgs_cols].to_csv(usgs_path, index=False)
        log.info(f"USGS elev table ({len(usgs)} rows) --> {usgs_path.name}")
        out["usgs_elev_table"] = usgs_path

    return out


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Assign USGS / AHPS / RAS2FIM gages to NWM feature_id + level path "
            "(stage 1) and crosswalk to a branch's catchments and thalweg-DEM "
            "(stage 2). Use --stage aoi or --stage branch."
        )
    )
    sub = parser.add_subparsers(dest="stage", required=True)

    aoi = sub.add_parser("aoi", help="Stage 1: assign AOI-wide gages to level paths")
    aoi.add_argument("--usgs-gages", required=True)
    aoi.add_argument("--nwm-streams-levelpaths", required=True)
    aoi.add_argument("--aoi-id", required=True, help="AOI identifier (often a HUC8 code)")
    aoi.add_argument("--out-dir", required=True)
    aoi.add_argument("--ras-locs", default=None)
    aoi.add_argument("--ahps", default=None)
    aoi.add_argument("--branch-zero-id", default="0")
    aoi.add_argument("--target-crs", default="5070")
    aoi.add_argument("--out-name", default="usgs_subset_gages.gpkg")
    aoi.add_argument(
        "--aoi-filter-column", default="HUC8",
        help="Column in the gage tables that identifies AOI membership (default HUC8)."
    )

    br = sub.add_parser("branch", help="Stage 2: per-branch crosswalk + DEM sample")
    br.add_argument("--aoi-gages", required=True, help="usgs_subset_gages*.gpkg")
    br.add_argument("--catchments", required=True)
    br.add_argument("--flows", required=True)
    br.add_argument("--dem", required=True)
    br.add_argument("--dem-thalweg", required=True)
    br.add_argument("--branch-id", required=True)
    br.add_argument("--out-dir", required=True)
    br.add_argument("--target-crs", default="5070")

    args = parser.parse_args()

    if args.stage == "aoi":
        assign_gages_to_branches(
            usgs_gages_gpkg=args.usgs_gages,
            nwm_streams_levelpaths_gpkg=args.nwm_streams_levelpaths,
            aoi_id=args.aoi_id,
            out_dir=args.out_dir,
            target_crs=args.target_crs,
            branch_zero_id=args.branch_zero_id,
            ras_locs_gpkg=args.ras_locs,
            ahps_gpkg=args.ahps,
            out_name=args.out_name,
            aoi_filter_column=args.aoi_filter_column,
        )
    else:
        run_branch_crosswalk(
            aoi_gages_gpkg=args.aoi_gages,
            branch_catchments_gpkg=args.catchments,
            branch_flows_gpkg=args.flows,
            dem_path=args.dem,
            dem_thalweg_path=args.dem_thalweg,
            branch_id=args.branch_id,
            out_dir=args.out_dir,
            target_crs=args.target_crs,
        )
