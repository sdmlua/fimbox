"""
Author: Supath Dhital
Date Created: August 2026

Cross-source standardisation of a staged stream network, ahead of branch
derivation.

NWM, NHDPlus HR and the NextGen hydrofabric describe the same hydrography under
different conventions, and the two the level-path walk depends on are exactly
the two that differ::

                geometry          connectivity            catchment key
    NWM         LineString        shared endpoints        ID
    NHDPlus HR  LineString        FromNode / ToNode       NHDPlusID / COMID
    NextGen     MultiLineString   toid -> nexus (gapped)  divide_id (cat-N)

Unhandled, the NextGen layout fails twice over: shapely refuses ``.coords`` on a
MultiLineString, and the nexus gap leaves no shared endpoints for coordinate
matching to find — which shreds the network into single-reach branches without
raising anything.

So the source is identified from the attributes the file actually carries, and
every network is then folded onto one shape: single-part geometry digitised
downstream, one row per reach, and connectivity read from whichever convention
the data honours. Detection decides only which columns to *read*. The source's
own ids (``toid``, ``divide_id``, ``wb_id``, ``mainstem``, ...) are carried
through untouched, so every output stays traceable to the hydrofabric it came
from.

:class:`HydrofabricFields` overrides any of it — declare the reach id, the nexus
/ downstream-id column, the stream order, the catchment key, or the discharge
join key, and the rest is still inferred.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Iterable, Optional

import geopandas as gpd
import shapely

log = logging.getLogger(__name__)

# Every stage after branch derivation keys reaches on ``ID``.
CANONICAL_REACH_ID = "ID"

# Column candidates, in preference order, matched case-insensitively.
_REACH_ID_CANDIDATES = (CANONICAL_REACH_ID, "wb_id", "divide_id", "nhdplusid", "comid")
_ORDER_CANDIDATES = ("order_", "order", "streamorde", "streamorder", "strmord")
_FEATURE_ID_CANDIDATES = ("feature_id", "comid", "nwm_id")

# Downstream-reach id columns. NWM uses ``to_``; NextGen uses ``toid`` and points
# at a nexus (``nex-270518``) whose numeric stem is the downstream reach's id.
_TO_ID_CANDIDATES = (
    "to_",
    "toid",
    "to_id",
    "tocomid",
    "dscomid",
    "ds_id",
    "downstream",
)

# Explicit node-id pairs (NHDPlus ``FromNode``/``ToNode``) — already node keys,
# so they are used verbatim.
_NODE_PAIR_CANDIDATES = (("fromnode", "tonode"), ("from_node", "to_node"))

# Catchment columns that may carry the reach id, tried when the requested one is
# absent or joins nothing.
CATCHMENT_ID_CANDIDATES = ("ID", "divide_id", "wb_id", "feature_id", "COMID", "comid")

# "no downstream" sentinels seen across sources.
_NULL_IDS = {"", "0", "-1", "none", "nan", "<na>", "null"}

_DIGITS = re.compile(r"\d+")

_MULTILINESTRING = 5  # shapely geometry type id

# Attribute signatures, most specific first. This only labels the source for the
# log and the run record — no column is renamed on the strength of it.
_SIGNATURES = (
    ("ngen", ("toid", "divide_id")),
    ("ngen", ("toid", "wb_id")),
    ("ngen", ("toid", "mainstem")),
    ("nhdplus", ("fromnode", "tonode")),
    ("nhdplus", ("nhdplusid",)),
    ("nwm", ("to_",)),
)


@dataclass(frozen=True)
class HydrofabricFields:
    """Column overrides for a staged network — every field optional.

    Anything left ``None`` is detected from the staged attributes. Naming
    ``to_id`` *forces* that connectivity column even where the geometry would
    link more reaches, so a source whose nexus ids are authoritative is honoured
    rather than second-guessed (the mismatch is logged either way).
    """

    reach_id: Optional[str] = None
    to_id: Optional[str] = None
    stream_order: Optional[str] = None
    catchment_id: Optional[str] = None
    feature_id: Optional[str] = None


@dataclass(frozen=True)
class HydrofabricProfile:
    """Which hydrofabric was staged, and the columns resolved on it."""

    source: str  # "ngen" | "nhdplus" | "nwm" | "unknown"
    reach_id: str
    to_id: Optional[str] = None
    node_pair: Optional[tuple[str, str]] = None
    stream_order: Optional[str] = None
    catchment_id: Optional[str] = None
    feature_id: Optional[str] = None
    forced_connectivity: bool = False


def find_column(gdf: gpd.GeoDataFrame, candidates: Iterable[str]) -> Optional[str]:
    """First candidate present in ``gdf``, matched without regard to case."""
    lookup = {str(col).lower(): col for col in gdf.columns}
    for name in candidates:
        hit = lookup.get(str(name).lower()) if name else None
        if hit is not None:
            return hit
    return None


def id_stem(value) -> str:
    """Numeric stem of an id, so ``nex-270518`` lines up with reach ``270518``."""
    match = _DIGITS.search(str(value))
    return match.group(0) if match else str(value).strip()


def endpoints(geom) -> tuple[tuple[float, float], tuple[float, float]]:
    """Upstream-most and downstream-most vertex of a flowline.

    Every part runs downstream, but ``line_merge`` reorders the parts of anything
    it could not stitch, so on a gapped reach the part order says nothing about
    flow — reading vertex one off the first part can hand back the outlet. The
    extremities are taken instead: the part start and part end lying farthest
    apart, which on a flowline with a gap in it is the inlet / outlet pair.
    """
    if geom.geom_type == "LineString":
        coords = geom.coords
        return coords[0], coords[-1]
    parts = list(geom.geoms)
    starts = [p.coords[0] for p in parts]
    ends = [p.coords[-1] for p in parts]
    return max(
        ((s, e) for s in starts for e in ends),
        key=lambda pair: (
            (pair[0][0] - pair[1][0]) ** 2 + (pair[0][1] - pair[1][1]) ** 2
        ),
    )


def merge_parts(geometry):
    """Stitch multi-part flowlines into single LineStrings, direction first.

    ``directed=True`` refuses to reverse a part to close a join, so a reach whose
    parts are digitised out of order stays multi instead of coming back running
    upstream — which would put its inlet vertex, the headwater seed, at the
    outlet. Those are then merged with the direction constraint relaxed, since
    single-part geometry is what every consumer expects, and counted in a warning:
    a reversed part is a defect in the staged data, not something to absorb
    silently. Genuinely disconnected parts stay multi either way.
    """
    merged = shapely.line_merge(geometry, directed=True)
    unmerged = shapely.get_type_id(merged) == _MULTILINESTRING
    if unmerged.any():
        relaxed = shapely.line_merge(merged[unmerged])
        n_reversed = int((shapely.get_type_id(relaxed) != _MULTILINESTRING).sum())
        if n_reversed:
            log.warning(
                "%d reach(es) only merge with a part reversed — stitched anyway, but "
                "their digitised direction is inconsistent in the staged data",
                n_reversed,
            )
        merged[unmerged] = relaxed
        n_broken = int((shapely.get_type_id(merged) == _MULTILINESTRING).sum())
        if n_broken:
            log.warning(
                "%d reach(es) stayed multi-part (disconnected segments); their "
                "farthest-apart endpoints are taken as inlet and outlet",
                n_broken,
            )
    return merged


def detect_hydrofabric(
    streams: gpd.GeoDataFrame,
    *,
    fields: Optional[HydrofabricFields] = None,
    reach_id: str = CANONICAL_REACH_ID,
    stream_order: str = "order_",
    to_id: Optional[str] = None,
) -> HydrofabricProfile:
    """Identify the staged hydrofabric and the columns branch derivation reads.

    ``fields`` wins over detection column by column, and ``reach_id`` /
    ``stream_order`` / ``to_id`` are the caller's defaults when it stays silent.
    """
    fields = fields or HydrofabricFields()
    staged = {str(c).lower() for c in streams.columns}
    source = next(
        (name for name, needs in _SIGNATURES if all(c in staged for c in needs)),
        "unknown",
    )

    resolved_reach_id = find_column(
        streams, [fields.reach_id or reach_id, *_REACH_ID_CANDIDATES]
    )
    if resolved_reach_id is None:
        raise KeyError(
            f"No reach id column in the stream network: neither "
            f"'{fields.reach_id or reach_id}' nor any of {_REACH_ID_CANDIDATES}. "
            "Pass hydrofabric_fields=HydrofabricFields(reach_id=<your column>)."
        )

    # A named downstream-id column is the caller's declaration of how this source
    # is wired, so it displaces the auto-detected node pair as well.
    requested_to_id = fields.to_id or to_id
    resolved_to_id = (
        find_column(streams, [requested_to_id]) if requested_to_id else None
    )
    if requested_to_id and resolved_to_id is None:
        log.warning(
            "Downstream-id column '%s' is not in the stream network — falling back "
            "to auto-detection",
            requested_to_id,
        )

    node_pair = None
    if resolved_to_id is None:
        node_pair = next(
            (
                pair
                for pair in (
                    (find_column(streams, [a]), find_column(streams, [b]))
                    for a, b in _NODE_PAIR_CANDIDATES
                )
                if all(pair)
            ),
            None,
        )
        # Node pairs are already node keys; only look for a downstream id without.
        if node_pair is None:
            resolved_to_id = find_column(streams, _TO_ID_CANDIDATES)

    return HydrofabricProfile(
        source=source,
        reach_id=resolved_reach_id,
        to_id=resolved_to_id,
        node_pair=node_pair,
        stream_order=find_column(
            streams, [fields.stream_order or stream_order, *_ORDER_CANDIDATES]
        )
        or (fields.stream_order or stream_order),
        catchment_id=fields.catchment_id,
        feature_id=fields.feature_id or find_column(streams, _FEATURE_ID_CANDIDATES),
        forced_connectivity=bool(requested_to_id and resolved_to_id),
    )


def standardize_network(
    streams: gpd.GeoDataFrame, profile: HydrofabricProfile
) -> tuple[gpd.GeoDataFrame, HydrofabricProfile]:
    """Fold a staged network onto the one shape branch derivation expects.

    One row per reach, one single-part LineString digitised downstream, and the
    node keys / lengths / inlet vertices the level-path walk reads. The returned
    profile carries the reach id column as it ends up on the frame.
    """
    log.info(
        "Hydrofabric '%s' — reach id '%s', stream order '%s'",
        profile.source,
        profile.reach_id,
        profile.stream_order,
    )
    streams, profile = _canonical_reach_id(streams, profile)
    streams = _fold_geometry(streams, profile.reach_id)
    if streams.empty:
        return streams, profile
    return _attach_network(streams, profile), profile


def standardize_catchments(
    catchments: gpd.GeoDataFrame,
    stream_reach_ids: Iterable[str],
    catchment_reach_id_attribute: Optional[str] = CANONICAL_REACH_ID,
) -> gpd.GeoDataFrame:
    """Add ``_reach_key``: the catchment id in the form the reach ids use.

    Sources label the same catchment differently — ``ID``, ``divide_id``
    (``cat-270517``), ``wb_id`` (``wb-270517``) — and a BYO layer may only carry
    one of them. The column that actually overlaps the reach ids wins, with
    numeric-stem matching as the last resort so a prefix mismatch alone never
    strands a branch.
    """
    requested = catchment_reach_id_attribute or CANONICAL_REACH_ID
    reach_ids = {str(rid) for rid in stream_reach_ids}
    stems = {id_stem(rid): rid for rid in reach_ids}

    candidates = [requested] + [c for c in CATCHMENT_ID_CANDIDATES if c != requested]
    best_column, best_keys, best_hits = None, None, 0
    for name in candidates:
        column = find_column(catchments, [name])
        if column is None:
            continue
        keys = catchments[column].astype(str)
        direct = keys.isin(reach_ids)
        # Only pay for the regex pass when the labels don't already line up.
        if not direct.all():
            keys = keys.where(direct, keys.map(lambda v: stems.get(id_stem(v), v)))
            direct = keys.isin(reach_ids)
        hits = int(direct.sum())
        if hits > best_hits:
            best_column, best_keys, best_hits = column, keys, hits
        if best_hits == len(catchments):
            break

    if best_column is None:
        raise KeyError(
            f"Catchment reach id column '{requested}' was not found in catchments, "
            f"and none of {CATCHMENT_ID_CANDIDATES} matched. Pass "
            "hydrofabric_fields=HydrofabricFields(catchment_id=<your column>)."
        )
    if best_column != requested:
        log.info(
            "Catchments joined on '%s' (%d match) — '%s' matched no reach ids",
            best_column,
            best_hits,
            requested,
        )

    catchments = catchments.copy()
    catchments["_reach_key"] = best_keys
    return catchments


def reach_lengths_km(streams: gpd.GeoDataFrame) -> list[float]:
    """Reach lengths in km, measured in a projected CRS whatever the input is."""
    geometry = streams.geometry
    if streams.crs is not None and streams.crs.is_geographic:
        geometry = streams.geometry.to_crs(streams.estimate_utm_crs())
    return [float(length) / 1000.0 for length in geometry.length]


def _canonical_reach_id(
    streams: gpd.GeoDataFrame, profile: HydrofabricProfile
) -> tuple[gpd.GeoDataFrame, HydrofabricProfile]:
    """Guarantee an ``ID`` column without losing the source's own spelling.

    A case variant is renamed, since SQLite would reject ``id`` and ``ID`` side
    by side in a GeoPackage; a differently named id is *copied*, so the original
    column still reaches every output. Ids become strings either way — the walk
    keys dictionaries on them, and an int/float round-trip through GPKG would
    stop comparing equal.
    """
    streams = streams.copy()
    reach_id = profile.reach_id

    if (
        reach_id != CANONICAL_REACH_ID
        and reach_id.lower() == CANONICAL_REACH_ID.lower()
    ):
        streams = streams.rename(columns={reach_id: CANONICAL_REACH_ID})
        reach_id = CANONICAL_REACH_ID

    streams[reach_id] = streams[reach_id].astype(str)

    if reach_id != CANONICAL_REACH_ID:
        existing = find_column(streams, [CANONICAL_REACH_ID])
        if existing is None:
            streams[CANONICAL_REACH_ID] = streams[reach_id]
            log.info(
                "Reach ids read from '%s' and mirrored into '%s' for the stages "
                "downstream; '%s' is kept as staged",
                reach_id,
                CANONICAL_REACH_ID,
                reach_id,
            )
        else:
            log.warning(
                "Reach ids read from '%s' while '%s' already holds different values "
                "— stages downstream key on '%s'",
                reach_id,
                existing,
                existing,
            )

    return streams, replace(profile, reach_id=reach_id)


def _fold_geometry(
    streams: gpd.GeoDataFrame, reach_id_attribute: str
) -> gpd.GeoDataFrame:
    """One row, one single-part LineString per reach — whatever was staged.

    Three source quirks are ironed out here so nothing downstream has to know
    which hydrography it is looking at:

    * empty / missing geometry is dropped rather than crashing the node walk;
    * a reach split across several rows is dissolved back into one;
    * MultiLineStrings are stitched by :func:`merge_parts` (NextGen wraps every
      flowpath in a multi, and shapely refuses ``.coords`` on those).
    """
    streams = streams.copy()
    geometry = streams.geometry.name

    blank = streams.geometry.isna() | streams.geometry.is_empty
    if blank.any():
        log.warning(
            "Dropping %d stream reach(es) with empty geometry", int(blank.sum())
        )
        streams = streams.loc[~blank].copy()
    if streams.empty:
        return streams

    # A duplicated reach id would silently collapse in the reach->length lookup
    # the level-path walk builds, so fold the parts together up front instead.
    duplicated = streams[reach_id_attribute].duplicated(keep=False)
    if duplicated.any():
        n_ids = streams.loc[duplicated, reach_id_attribute].nunique()
        log.warning(
            "Dissolving %d reach id(s) that span multiple rows into one row each",
            n_ids,
        )
        merged = streams.dissolve(by=reach_id_attribute, as_index=False)
        # dissolve keeps the first row's attributes, which is what we want; only
        # the column order needs restoring.
        streams = merged[
            [c for c in streams.columns if c in merged.columns and c != geometry]
            + [geometry]
        ]
        streams = gpd.GeoDataFrame(streams, geometry=geometry, crs=merged.crs)

    streams[geometry] = merge_parts(streams.geometry.values)

    if streams.crs is not None and streams.crs.is_geographic:
        log.warning(
            "Stream CRS %s is geographic — branch buffers and reach lengths are "
            "in metres, so project the AOI (EPSG:5070 by default) before staging",
            streams.crs.to_string(),
        )
    return streams.reset_index(drop=True)


def _coordinate_nodes(streams: gpd.GeoDataFrame) -> tuple[list[str], list[str]]:
    """Node keys from shared endpoint coordinates — the NWM / NHDPlus convention."""
    from_nodes, to_nodes = [], []
    for geom in streams.geometry:
        start, end = endpoints(geom)
        from_nodes.append(f"{round(start[0], 6)}_{round(start[1], 6)}")
        to_nodes.append(f"{round(end[0], 6)}_{round(end[1], 6)}")
    return from_nodes, to_nodes


def _explicit_nodes(
    streams: gpd.GeoDataFrame, reach_id_attribute: str, to_id_attribute: str
) -> tuple[list[str], list[str]]:
    """Node keys from a downstream-id column — the NextGen / nexus convention.

    Each reach owns the node at its head (``reach:<id>``) and points at the head
    node of the reach it drains into, so equality of these opaque keys carries
    exactly the same meaning as a shared coordinate would. Terminal reaches get
    a private key that nothing else can match.
    """
    reach_ids = streams[reach_id_attribute].astype(str)
    by_id = set(reach_ids)
    by_stem = {id_stem(rid): rid for rid in by_id}

    from_nodes = [f"reach:{rid}" for rid in reach_ids]
    to_nodes = []
    for rid, raw in zip(reach_ids, streams[to_id_attribute]):
        target = str(raw).strip()
        if target not in by_id:
            # ``nex-270518`` -> ``270518``; also rescues int/float round-trips.
            target = by_stem.get(id_stem(target), "") if target else ""
        if not target or target.lower() in _NULL_IDS or target == rid:
            to_nodes.append(f"outlet:{rid}")
        else:
            to_nodes.append(f"reach:{target}")
    return from_nodes, to_nodes


def _internal_edges(from_nodes: list[str], to_nodes: list[str]) -> int:
    """Upstream->downstream links these node keys resolve inside the network.

    This is the score the two connectivity conventions are compared on: a
    convention that leaves reaches unlinked scores low, whatever the reason.
    """
    heads: dict[str, int] = defaultdict(int)
    for node in from_nodes:
        heads[node] += 1
    return sum(heads.get(node, 0) for node in to_nodes)


def _attach_network(
    streams: gpd.GeoDataFrame, profile: HydrofabricProfile
) -> gpd.GeoDataFrame:
    """Attach the node keys, reach length, and inlet vertex the branch walk needs.

    Connectivity comes from whichever convention the staged network honours: the
    declared node columns when they link more reaches than the geometry does,
    otherwise shared endpoint coordinates. Scoring on connected-reach count means
    a source with a stale or partly-null ``to_`` column still falls back to
    coordinates instead of yielding a network of orphans — unless the caller named
    the column, in which case the declaration stands and the shortfall is logged.
    """
    streams = streams.copy()

    coord_from, coord_to = _coordinate_nodes(streams)
    from_nodes, to_nodes = coord_from, coord_to
    source = "endpoint coordinates"

    if profile.node_pair is not None:
        candidate = (
            [f"node:{v}" for v in streams[profile.node_pair[0]]],
            [f"node:{v}" for v in streams[profile.node_pair[1]]],
        )
        label = f"node columns {profile.node_pair[0]}/{profile.node_pair[1]}"
    elif profile.to_id and profile.to_id in streams.columns:
        candidate = _explicit_nodes(streams, profile.reach_id, profile.to_id)
        label = f"downstream-id column '{profile.to_id}'"
    else:
        candidate = None
        label = ""

    if candidate is not None:
        declared, by_coords = (
            _internal_edges(*candidate),
            _internal_edges(coord_from, coord_to),
        )
        if profile.forced_connectivity or declared > by_coords:
            if declared < by_coords:
                log.warning(
                    "%s links %d reach(es) where endpoint coordinates link %d — "
                    "honouring the column as passed",
                    label,
                    declared,
                    by_coords,
                )
            from_nodes, to_nodes = candidate
            source = label

    log.info(
        "Network connectivity from %s — %d of %d reach(es) linked",
        source,
        _internal_edges(from_nodes, to_nodes),
        len(streams),
    )

    streams["_from_node"] = from_nodes
    streams["_to_node"] = to_nodes
    streams["_length_km"] = reach_lengths_km(streams)
    # Inlet vertex, carried as plain floats so it survives the GeoPackage write
    # and headwater derivation never has to touch ``.coords`` again.
    inlets = [endpoints(geom)[0] for geom in streams.geometry]
    streams["_inlet_x"] = [float(pt[0]) for pt in inlets]
    streams["_inlet_y"] = [float(pt[1]) for pt in inlets]
    return streams
