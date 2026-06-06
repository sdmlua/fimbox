"""
Author: Supath Dhital
Date Updated: May 2026

Split derived stream reaches and build network topology.

Geoprocessing steps:
  1. Read vectorised DEM-derived reaches (from StreamNetReaches)
  2. Trim reaches to NWM branch terminus (snap-and-trim)
  3. Split reaches at area boundaries (optional — if wbd8 provided)
  4. Split reaches at lake boundaries   (optional — if lakes_gpkg exists)
  5. Split reaches longer than max_length into equal sub-segments
  6. Calculate channel slope from thalweg-conditioned DEM for each segment
  7. Assign unique HydroIDs from area boundary midpoint join (or sequential IDs)
  8. Build To_Node / From_Node / NextDownID network traversal columns
  9. Create split-point GeoPackage used by gage-watershed delineation

Inputs
------
reaches_gpkg      : demDerived_reaches_{id}.gpkg       (from StreamNetReaches)
dem_thalweg_cond  : dem_thalwegCond_{id}.tif
nwm_streams_gpkg  : nwm_subset_streams.gpkg            (for snap-and-trim)
out_split_gpkg    : demDerived_reaches_split_{id}.gpkg
out_points_gpkg   : demDerived_reaches_split_points_{id}.gpkg
wbd8_clp_gpkg     : wbd8_clp.gpkg                     (optional)
lakes_gpkg        : nwm_lakes_proj_subset.gpkg         (optional)
"""

from __future__ import annotations
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.sample
from shapely.geometry import LineString, Point
from shapely.ops import split as shapely_split

gpd.options.io_engine = "pyogrio"

log = logging.getLogger(__name__)

_TO_KM = 1e-3


def split_derived_reaches(
    reaches_gpkg: Path,
    dem_thalweg_cond: Path,
    nwm_streams_gpkg: Path,
    out_split_gpkg: Path,
    out_points_gpkg: Path,
    wbd8_clp_gpkg: Optional[Path] = None,
    lakes_gpkg: Optional[Path] = None,
    max_length: float = 1500.0,
    slope_min: float = 0.0001,
    lakes_buffer_dist: float = 100.0,
) -> tuple[Path, Path]:
    """
    Split and attribute DEM-derived stream reaches.

    Returns
    -------
    (out_split_gpkg, out_points_gpkg)
    """
    reaches_gpkg = Path(reaches_gpkg)
    dem_thalweg_cond = Path(dem_thalweg_cond)
    out_split_gpkg = Path(out_split_gpkg)
    out_points_gpkg = Path(out_points_gpkg)
    out_split_gpkg.parent.mkdir(parents=True, exist_ok=True)

    log.info("SplitReaches: loading %s", reaches_gpkg.name)
    flows = gpd.read_file(str(reaches_gpkg), engine="fiona")
    flows = flows.explode(index_parts=False)
    flows = flows.loc[~flows.is_empty, :]

    if len(flows) == 0:
        log.warning("SplitReaches: no stream features found in %s", reaches_gpkg.name)
        return out_split_gpkg, out_points_gpkg

    # DEM CRS is authoritative for all projected geometry (length, buffer, slope, sjoin).
    # All operations stay in dem_crs; WBD and lakes are reprojected to match.
    with rasterio.open(str(dem_thalweg_cond)) as _src:
        dem_crs = _src.crs

    flows = flows.to_crs(dem_crs)

    # optional: read wbd for HydroID generation and boundary split
    wbd8 = None
    if wbd8_clp_gpkg and Path(wbd8_clp_gpkg).exists():
        wbd8 = gpd.read_file(str(wbd8_clp_gpkg), engine="fiona").to_crs(dem_crs)
        log.info("SplitReaches: splitting at area boundaries")
        flows = (
            gpd.overlay(flows, wbd8[["geometry"]], how="union", keep_geom_type=True)
            .explode(index_parts=True)
            .reset_index(drop=True)
        )
        flows = flows.loc[~flows.is_empty, :]

    # optional: trim to NWM terminus
    nwm_streams_gpkg = Path(nwm_streams_gpkg)
    if nwm_streams_gpkg.exists():
        flows = _snap_and_trim(flows, nwm_streams_gpkg)

    # optional: split at lake boundaries
    lakes = None
    lake_id_col = None
    if lakes_gpkg and Path(lakes_gpkg).exists():
        lakes_gdf = gpd.read_file(str(lakes_gpkg), engine="fiona").to_crs(dem_crs)
        if len(lakes_gdf) > 0:
            lake_id_col = next(
                (c for c in ("newID", "wb_id", "LakeID") if c in lakes_gdf.columns),
                None,
            )
            if lake_id_col:
                log.info(
                    "SplitReaches: splitting at %d lake boundaries", len(lakes_gdf)
                )
                lakes_gdf = lakes_gdf[[lake_id_col, "geometry"]].set_index(lake_id_col)
                flows = (
                    gpd.overlay(flows, lakes_gdf, how="union", keep_geom_type=True)
                    .explode(index_parts=True)
                    .reset_index(drop=True)
                )
                flows = flows.loc[~flows.is_empty, :]
                lakes = lakes_gdf.copy()

    if len(flows) == 0:
        log.warning("SplitReaches: no flows remain after boundary splits")
        return out_split_gpkg, out_points_gpkg

    # split long segments & compute slope — geometries are in dem_crs (metres)
    log.info(
        "SplitReaches: splitting %d segments (max_length=%.0fm)", len(flows), max_length
    )
    split_lines: list[LineString] = []
    slopes: list[float] = []

    with rasterio.open(str(dem_thalweg_cond)) as dem_ds:
        for geom in flows.geometry:
            if geom is None or geom.is_empty or geom.length == 0:
                continue
            # TauDEM streamnet outputs reaches upstream-first (headwater --> confluence).
            # Do NOT reverse — coords[0] = headwater, coords[-1] = outlet/confluence.
            line = LineString(geom.coords)
            _split_one_line(line, dem_ds, max_length, slope_min, split_lines, slopes)

    if len(split_lines) == 0:
        log.warning("SplitReaches: no segments produced after splitting")
        return out_split_gpkg, out_points_gpkg

    split_gdf = gpd.GeoDataFrame({"S0": slopes, "geometry": split_lines}, crs=dem_crs)
    split_gdf["LengthKm"] = split_gdf.geometry.length * _TO_KM

    # Assign LakeID
    if lakes is not None and lake_id_col is not None:
        lakes_buf = lakes.copy()
        lakes_buf.index.name = lake_id_col
        lakes_buf = lakes_buf.reset_index()
        lakes_buf["geometry"] = lakes_buf.buffer(lakes_buffer_dist)
        split_gdf = gpd.sjoin(
            split_gdf,
            lakes_buf[[lake_id_col, "geometry"]],
            how="left",
            predicate="within",
        )
        split_gdf = split_gdf.rename(columns={lake_id_col: "LakeID"}).fillna(-999)
        split_gdf = split_gdf.drop(columns=["index_right"], errors="ignore")
    else:
        split_gdf["LakeID"] = -999

    split_gdf = split_gdf.drop_duplicates(subset=["geometry"])

    # assign HydroIDs and network topology (wbd8 already in dem_crs)
    split_gdf = _assign_hydro_ids(split_gdf, wbd8)
    split_gdf = split_gdf.query("From_Node != To_Node")

    if len(split_gdf) == 0:
        log.warning("SplitReaches: no valid segments after topology build")
        return out_split_gpkg, out_points_gpkg

    # create split points — one outlet point per reach (last coord = downstream end).
    # FIM iterates all vertices, but using only the outlet avoids competing seeds at
    # confluences where two tributary reaches share the same upstream start point.
    # The outlet point is what GageCatchments seeds to label each sub-watershed.
    split_points_od: OrderedDict = OrderedDict()
    for _, seg in split_gdf.iterrows():
        line = seg.geometry
        for pt in zip(*line.coords.xy):
            if pt in split_points_od:
                if seg.NextDownID != split_points_od[pt]:
                    split_points_od[pt] = seg["HydroID"]
            else:
                split_points_od[pt] = seg["HydroID"]

    hydro_ids_pts = list(split_points_od.values())
    points = [Point(*pt) for pt in split_points_od]
    pts_gdf = gpd.GeoDataFrame({"id": hydro_ids_pts, "geometry": points}, crs=dem_crs)

    # write outputs (remove any 0-byte leftovers from prior failed runs)
    for _p in (out_split_gpkg, out_points_gpkg):
        if _p.exists():
            _p.unlink()
    log.info(
        "SplitReaches: writing %d segments --> %s", len(split_gdf), out_split_gpkg.name
    )
    split_gdf.to_file(str(out_split_gpkg), driver="GPKG", index=False, engine="fiona")
    pts_gdf.to_file(str(out_points_gpkg), driver="GPKG", index=False, engine="fiona")

    return out_split_gpkg, out_points_gpkg


def _split_one_line(
    line: LineString,
    dem_ds,
    max_length: float,
    slope_min: float,
    out_lines: list,
    out_slopes: list,
) -> None:
    """Split one LineString at max_length intervals and compute slope from DEM."""
    if line.length < max_length:
        slope = _line_slope(line, dem_ds, slope_min)
        out_lines.append(line)
        out_slopes.append(slope)
        return

    split_len = line.length / np.ceil(line.length / max_length)
    cumulative: list[tuple] = []
    last_point: Optional[tuple] = None
    last_point_in_line = list(zip(*line.coords.xy))[-1]

    for pt in zip(*line.coords.xy):
        cumulative.append(pt)
        if last_point and len(cumulative) == 1:
            # prepend connection point once at the start of each new segment
            cumulative = [last_point] + cumulative
        elif not last_point and len(cumulative) == 1:
            continue

        seg_line = LineString(cumulative)
        if seg_line.length >= split_len:
            slope = _line_slope(seg_line, dem_ds, slope_min)
            out_lines.append(seg_line)
            out_slopes.append(slope)
            last_point = cumulative[-1]
            if last_point == last_point_in_line:
                break
            cumulative = []

    if cumulative and len(cumulative) > 1:
        seg_line = LineString(cumulative)
        slope = _line_slope(seg_line, dem_ds, slope_min)
        out_lines.append(seg_line)
        out_slopes.append(slope)


def _line_slope(line: LineString, dem_ds, slope_min: float) -> float:
    pts = list(zip(*line.coords.xy))
    if len(pts) < 2:
        return slope_min
    try:
        elevs = [v[0] for v in rasterio.sample.sample_gen(dem_ds, [pts[0], pts[-1]])]
        slope = abs(elevs[0] - elevs[1]) / max(line.length, 1e-9)
    except Exception:
        slope = slope_min
    return max(slope, slope_min)


def _snap_and_trim(flows: gpd.GeoDataFrame, nwm_streams_gpkg: Path) -> gpd.GeoDataFrame:
    """Trim DEM-derived flows to NWM branch terminus (matching split_flows.py logic)."""
    try:
        nwm = gpd.read_file(str(nwm_streams_gpkg), engine="fiona").explode(
            index_parts=True
        )
        if flows.crs is None:
            log.warning("snap-and-trim skipped: reaches have no CRS")
            return flows
        nwm = nwm.to_crs(flows.crs)

        if "levpa_id" in nwm.columns:
            # Non-zero branch: single level path
            from shapely.ops import linemerge

            dissolved = nwm.dissolve(by="levpa_id").iloc[0]["geometry"]
            merged = linemerge(dissolved)
            if merged.geom_type == "MultiLineString":
                merged = list(merged.geoms)[-1]
            terminal_pt = gpd.GeoDataFrame(
                [{"geometry": Point(list(merged.coords)[-1])}], crs=flows.crs
            )
            flows = _snap_trim_single(terminal_pt, flows)
        else:
            # Branch zero: loop over NWM terminal segments (to == 0)
            terminals = (
                nwm[nwm.get("to", None) == 0] if "to" in nwm.columns else nwm.iloc[:0]
            )
            for _, row in terminals.iterrows():
                pt = gpd.GeoDataFrame(
                    [{"geometry": Point(list(row.geometry.coords)[-1])}], crs=flows.crs
                )
                flows = _snap_trim_single(pt, flows)
    except Exception as exc:
        log.warning("snap-and-trim skipped: %s", exc)
    return flows


def _snap_trim_single(
    snapped_pt: gpd.GeoDataFrame, flows: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Snap a point to the nearest flow and trim the flow at that point."""
    try:
        if len(flows) > 1:
            sj = gpd.sjoin_nearest(snapped_pt, flows, max_distance=100)
            if sj.empty:
                return flows
        flow = flows.iloc[[0]] if len(flows) == 1 else flows
        snapped_pt = snapped_pt.copy()
        snapped_pt["geometry"] = flow.interpolate(
            flow.project(snapped_pt.geometry)
        ).values
        trimmed = shapely_split(
            flow.iloc[0]["geometry"],
            snapped_pt.iloc[0]["geometry"].buffer(1),
        )
        if len(list(trimmed.geoms)) > 1:
            flows = flows.copy()
            flows.iloc[0, flows.columns.get_loc("geometry")] = list(trimmed.geoms)[-1]
    except Exception as exc:
        log.debug("snap-trim failed: %s", exc)
    return flows


def _assign_hydro_ids(
    split_gdf: gpd.GeoDataFrame,
    wbd8: Optional[gpd.GeoDataFrame],
) -> gpd.GeoDataFrame:
    """
    Port of build_stream_traversal.build_stream_traversal_columns().
    Assigns HydroID, From_Node, To_Node, NextDownID to stream segments.
    """
    split_gdf = split_gdf.copy().reset_index(drop=True)
    hydro_id = "HydroID"

    # HydroID from area boundary midpoint join
    if wbd8 is not None and hydro_id not in split_gdf.columns:
        midpoints = gpd.GeoDataFrame(
            {
                "geometry": [
                    g.interpolate(0.5, normalized=True) for g in split_gdf.geometry
                ]
            },
            crs=split_gdf.crs,
        )
        # fimid is the 4-digit node ID FIM uses as the HydroID prefix (e.g. "1864").
        # HUC8 is 8 digits and would produce 12-digit HydroIDs that overflow int32.
        # Always prefer fimid; fall back to first non-geometry column only if absent.
        _wbd_cols_lower = {c.lower(): c for c in wbd8.columns}
        boundary_id_col = (
            _wbd_cols_lower.get("fimid")
            or _wbd_cols_lower.get("fossid")
            or next((c for c in wbd8.columns if c != "geometry"), None)
        )

        if boundary_id_col:
            joined = gpd.sjoin(
                midpoints,
                wbd8[[boundary_id_col, "geometry"]],
                how="left",
                predicate="within",
            )
            split_gdf["boundary_id"] = joined[boundary_id_col].values
            split_gdf["seqID"] = (
                (split_gdf.groupby("boundary_id", dropna=False).cumcount() + 1)
                .astype(str)
                .str.zfill(4)
            )
            split_gdf = split_gdf.loc[split_gdf["boundary_id"].notna(), :]
            split_gdf[hydro_id] = (
                split_gdf["boundary_id"].astype(str) + split_gdf["seqID"]
            ).astype(np.int64, errors="ignore")
            split_gdf = split_gdf.drop(
                columns=["boundary_id", "seqID"], errors="ignore"
            )
        else:
            split_gdf[hydro_id] = np.arange(1, len(split_gdf) + 1, dtype=np.int64)
    else:
        split_gdf[hydro_id] = np.arange(1, len(split_gdf) + 1, dtype=np.int64)

    split_gdf = split_gdf.sort_values(hydro_id).reset_index(drop=True)

    # From_Node / To_Node from endpoint coordinates.
    # Replicates FIM build_stream_traversal.py: round to 7 decimal places.
    xy_dict: dict[str, int] = {}
    node_counter = [0]

    def _get_node(x: float, y: float) -> int:
        key = f"{round(x, 7)},{round(y, 7)}"
        if key not in xy_dict:
            node_counter[0] += 1
            xy_dict[key] = node_counter[0]
        return xy_dict[key]

    from_nodes, to_nodes = [], []
    for geom in split_gdf.geometry:
        if geom is None or geom.is_empty:
            from_nodes.append(0)
            to_nodes.append(0)
            continue
        coords = list(geom.coords)
        from_nodes.append(_get_node(coords[0][0], coords[0][1]))
        to_nodes.append(_get_node(coords[-1][0], coords[-1][1]))

    split_gdf["From_Node"] = from_nodes
    split_gdf["To_Node"] = to_nodes

    # NextDownID: find the HydroID of the segment whose From_Node == our To_Node.
    # FIM uses a From_Node --> [list of HydroIDs] dict (multiple segments can start
    # from the same node at confluences). We replicate that: when multiple segments
    # share a From_Node, take the one with the smallest HydroID (first in sort order,
    # matching FIM's behaviour of taking next_down_ids[0]).
    dnodes: dict[int, list[int]] = {}
    for fn, hid_val in zip(
        split_gdf["From_Node"].tolist(), split_gdf[hydro_id].tolist()
    ):
        if fn in dnodes:
            dnodes[fn].append(int(hid_val))
        else:
            dnodes[fn] = [int(hid_val)]

    def _next_down(to_node: int) -> int:
        candidates = dnodes.get(int(to_node), [])
        return candidates[0] if candidates else -1

    split_gdf["NextDownID"] = split_gdf["To_Node"].apply(_next_down).astype(np.int64)

    return split_gdf
