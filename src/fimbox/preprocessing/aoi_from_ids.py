"""
Author: Supath Dhital
Date Updated: July 2026

Build AOI boundaries from HUC ids or NWM reach ids, so a run can start from an
id list instead of a boundary file.

Grouping is explicit, never inferred from geography:

    [101, 102, 103]            one AOI holding all three
    [[101, 102], [201, 202]]   one AOI per inner list
    [101, 102], separate=True  one AOI per id

An NWM AOI is the dissolved footprint of its catchment polygons, fetched by id
straight from the service — no boundary needed to get started.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Union

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from .download_data.nhdplus import (
    NWMCatchmentsDownloader,
    NWMFlowlinesDownloader,
)

log = logging.getLogger(__name__)

# The reach/catchment key on both NWM layers.
ID_FIELD = "ID"

# Ids per `ID IN (...)` request. The services cap the query string, so long
# lists are fetched in chunks and concatenated.
_ID_CHUNK = 200

ReachId = Union[int, str]
IdSpec = Union[ReachId, Sequence[ReachId], Sequence[Sequence[ReachId]]]


def normalize_groups(ids: IdSpec, separate: bool = False) -> list[list[str]]:
    """Resolve an id spec into AOI groups.

    A flat list is one group; a nested list is one group per inner list;
    ``separate`` overrides both to one group per id.
    """
    if ids is None:
        return []
    if isinstance(ids, (str, int)):
        groups = [[ids]]
    elif _is_nested(ids):
        groups = [list(inner) for inner in ids if len(list(inner))]
    else:
        groups = [list(ids)]

    cleaned: list[list[str]] = []
    for grp in groups:
        seen: dict[str, None] = {}
        for raw in grp:
            key = str(raw).strip()
            if key:
                seen.setdefault(key, None)
        if seen:
            cleaned.append(list(seen))

    if separate:
        return [[one] for grp in cleaned for one in grp]
    return cleaned


def normalize_huc_groups(hucs: IdSpec, together: bool = False) -> list[list[str]]:
    """Resolve a HUC spec into AOI groups.

    HUCs invert the reach default: a flat list is one AOI per HUC, since that is
    how FIM is normally run. Nesting groups them; ``together`` merges all into
    one AOI.
    """
    if hucs is None:
        return []
    if isinstance(hucs, (str, int)):
        groups = [[str(hucs).strip()]]
    elif _is_nested(hucs):
        groups = normalize_groups(hucs)
    else:
        groups = normalize_groups(hucs, separate=True)

    if together:
        merged = [h for grp in groups for h in grp]
        return [merged] if merged else []
    return groups


# Distance used to weld hairline gaps between adjacent catchment polygons. NWM
# catchment edges are only approximately coincident, so dissolving them can leave
# slivers and pinholes that would punch nodata into the AOI's DEM.
WELD_M = 50.0


def weld(geom, distance: float = WELD_M):
    """Close hairline gaps in a dissolved footprint and drop interior pinholes.

    A morphological close (buffer out, then back in) fuses slivers between
    neighbouring catchments; the exterior is then kept on its own so no ring
    survives inside the AOI. The outward and inward buffers cancel, so the
    footprint does not grow.
    """
    if geom is None or geom.is_empty or distance <= 0:
        return geom

    closed = geom.buffer(distance, join_style=2).buffer(-distance, join_style=2)
    if closed.is_empty:
        return geom

    parts = list(closed.geoms) if closed.geom_type == "MultiPolygon" else [closed]
    solid = [Polygon(p.exterior) for p in parts if p.geom_type == "Polygon"]
    if not solid:
        return closed
    return solid[0] if len(solid) == 1 else MultiPolygon(solid)


def fim_id(ids: Sequence[ReachId]) -> str:
    """4-digit HydroID prefix for a group, taken from the first id.

    Matches the branch-id convention so HydroIDs stay inside int32 — an 8-digit
    prefix would overflow once the 4-digit sequence is appended.
    """
    key = "".join(ch for ch in str(ids[0]) if ch.isdigit()) or "0"
    return key[:4].zfill(4)


def _is_nested(ids: Iterable) -> bool:
    return any(isinstance(item, (list, tuple, set)) for item in ids if item is not None)


def _where_chunks(ids: Sequence[ReachId]) -> list[str]:
    quoted = [str(i).strip() for i in ids]
    return [
        f"{ID_FIELD} IN ({','.join(quoted[i : i + _ID_CHUNK])})"
        for i in range(0, len(quoted), _ID_CHUNK)
    ]


def _fetch_by_ids(downloader, ids: Sequence[ReachId], label: str) -> gpd.GeoDataFrame:
    frames = []
    for where in _where_chunks(ids):
        got = downloader.download(boundary=None, where=where)
        if got is not None and not got.empty:
            frames.append(got)
    if not frames:
        return gpd.GeoDataFrame()
    out = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), crs=frames[0].crs
    ).drop_duplicates(subset=[ID_FIELD])
    log.info(f"{label}: {len(out)}/{len(ids)} ids resolved")
    return out


def fetch_nwm_flowlines_by_id(
    ids: Sequence[ReachId], epsg: int = 5070, n_workers: int = 8
) -> gpd.GeoDataFrame:
    """NWM flowlines for the given reach ids."""
    return _fetch_by_ids(
        NWMFlowlinesDownloader(out_sr=epsg, n_workers=n_workers), ids, "flowlines"
    )


def fetch_nwm_catchments_by_id(
    ids: Sequence[ReachId],
    epsg: int = 5070,
    n_workers: int = 8,
    reaches: Optional[gpd.GeoDataFrame] = None,
) -> gpd.GeoDataFrame:
    """NWM catchment polygons for the given reach ids.

    ``ID`` is unindexed on the catchment layer, so an ``IN`` query there costs
    ~45 s and starts erroring past a handful of ids. The reach geometries give a
    bounding box instead: fetch spatially (indexed, fast), then filter by id.
    """
    if reaches is None:
        reaches = fetch_nwm_flowlines_by_id(ids, epsg=epsg, n_workers=n_workers)
    if reaches.empty:
        return gpd.GeoDataFrame()

    bbox = tuple(reaches.to_crs(4326).total_bounds)
    nearby = NWMCatchmentsDownloader(out_sr=epsg, n_workers=n_workers).download(
        boundary=bbox, boundary_crs=4326
    )
    if nearby is None or nearby.empty:
        return gpd.GeoDataFrame()

    want = {str(i).strip() for i in ids}
    out = nearby[nearby[ID_FIELD].astype(str).isin(want)].copy()
    log.info(f"catchments: {len(out)}/{len(want)} ids resolved")
    return out


@dataclass
class ReachAOI:
    """Resolved reach group: AOI boundary plus the data it came from."""

    boundary: gpd.GeoDataFrame  # dissolved catchment footprint, EPSG:4326
    reaches: gpd.GeoDataFrame
    catchments: gpd.GeoDataFrame
    requested: list[str]
    missing: list[str]

    @property
    def reach_count(self) -> int:
        return len(self.reaches)


def resolve_reach_group(
    ids: Sequence[ReachId],
    epsg: int = 5070,
    n_workers: int = 8,
    strict: bool = True,
) -> ReachAOI:
    """Resolve a reach group into an AOI boundary and its source layers.

    ``strict`` raises when any id fails to resolve; otherwise the missing ones
    are logged and the rest are used.
    """
    requested = [str(i).strip() for i in ids]
    reaches = fetch_nwm_flowlines_by_id(requested, epsg=epsg, n_workers=n_workers)
    if reaches.empty:
        raise ValueError(f"No NWM flowlines found for ids: {requested}")

    found = {str(v) for v in reaches[ID_FIELD].astype("int64", errors="ignore")}
    found |= {str(v) for v in reaches[ID_FIELD]}
    missing = [i for i in requested if i not in found]
    if missing:
        msg = f"{len(missing)} reach id(s) not found: {missing}"
        if strict:
            raise ValueError(msg)
        log.warning(msg)

    cat = fetch_nwm_catchments_by_id(
        requested, epsg=epsg, n_workers=n_workers, reaches=reaches
    )
    if cat.empty:
        raise ValueError(f"No NWM catchments found for ids: {requested}")

    boundary = gpd.GeoDataFrame(
        # fimid is the 4-digit prefix HydroIDs are built from, so it has to be
        # the AOI's identity — not an incidental attribute like a reach count.
        {"fimid": [fim_id(requested)], "reach_count": [len(reaches)]},
        geometry=[weld(unary_union(cat.geometry))],
        crs=cat.crs,
    ).to_crs("EPSG:4326")
    return ReachAOI(boundary, reaches, cat, requested, missing)


def reaches_to_boundary(
    ids: Sequence[ReachId],
    epsg: int = 5070,
    n_workers: int = 8,
    strict: bool = True,
) -> gpd.GeoDataFrame:
    """AOI boundary for a reach group: the dissolved catchment footprint."""
    return resolve_reach_group(
        ids, epsg=epsg, n_workers=n_workers, strict=strict
    ).boundary


def group_label(ids: Sequence[ReachId], prefix: str = "nwm") -> str:
    """AOI folder name for a reach group: nwm_<first id>, or
    nwm_<first id>and<n>more when the group holds several reaches."""
    keys = [str(i).strip() for i in ids]
    if len(keys) == 1:
        return f"{prefix}_{keys[0]}"
    return f"{prefix}_{keys[0]}and{len(keys) - 1}more"


def huc_label(hucs: Sequence[str]) -> str:
    """AOI folder name for a HUC group, matching the reach naming: HUC<id>, or
    HUC<first id>and<n>more when several HUCs share one AOI."""
    keys = [str(h).strip() for h in hucs]
    if len(keys) == 1:
        return f"HUC{keys[0]}"
    return f"HUC{keys[0]}and{len(keys) - 1}more"


def hucs_to_boundary(hucs: Sequence[str]) -> gpd.GeoDataFrame:
    """Dissolved boundary for one or more HUC8s."""
    from .download_data.utils import HUC8Finder

    finder = HUC8Finder()
    frames = []
    for huc in hucs:
        gdf = finder.from_huc8(str(huc).strip())
        if gdf is None or gdf.empty:
            raise ValueError(f"HUC8 {huc!r} not found.")
        frames.append(gdf.to_crs("EPSG:4326"))

    if len(frames) == 1:
        return frames[0]

    # Weld in a projected CRS so the distance is in metres, then return to 4326.
    joined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), crs="EPSG:4326"
    ).to_crs(5070)
    return gpd.GeoDataFrame(
        {"fimid": [fim_id(list(hucs))], "huc_count": [len(frames)]},
        geometry=[weld(unary_union(joined.geometry))],
        crs=5070,
    ).to_crs("EPSG:4326")
