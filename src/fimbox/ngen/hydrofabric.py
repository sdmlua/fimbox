"""
Author: Supath Dhital
Date Created: July 2026

NextGen (ngen) hydrofabric flowpaths + divides, read from the community parquet
mirror.

``ngiab_data_preprocess`` queries a single CONUS GeoPackage: a 1.7 GB archive
expanded to 4.9 GB under ``~/.ngiab/``. fimbox stages one AOI at a time, so this
reads the same upstream data in its parquet form and fetches only the row groups
the AOI touches. Everything ngiab exposes without that GeoPackage is imported
from it rather than reimplemented -- see :mod:`._ngiab`.

Selection
---------
``divides.divide_id`` and ``flowpaths.id`` are sorted across row groups, so an id
predicate prunes on parquet statistics. The geometry columns carry no bbox
statistics, so a spatial predicate prunes nothing and would stream the whole
national layer. Every entry point therefore resolves an id set from the small
non-geometry columns first, then fetches geometry by id::

    huc8 / boundary -> divide centroids -> point-in-polygon --.
    cat-id ----------> network edge list -> upstream walk -----+-> id set -> geometry
    feature-id ------> network.hf_id -> wb-id -> upstream -----|
    gage-id ---------> flowpath-attributes.gage -> upstream ---'

Indexes are cached locally (~60 MB, fetched once) beside the other fimbox
reference data; no multi-GB artefact is written.

Schema
------
Output follows the fimbox canonical schema, so ngen is a drop-in for the NWM /
NHDPlus layers::

    ID          numeric stem shared by wb-N / cat-N, joins catchments to reaches
    order_      flowpaths."order"
    levpa_id    flowpaths.mainstem   (native level path)
    feature_id  network.hf_id        (NWM / NHD comid)
    toid        flowpaths.toid       (downstream nexus; the connectivity key)

``feature_id`` is load-bearing: FIM is driven from NWM feature-id discharge, so
mapping it from ``hf_id`` keeps the existing streamflow sources working against
ngen geometry. Native ngen keys (``wb_id``, ``divide_id``) are kept alongside.

``toid`` matters as much for an AOI subset. ngen routes reaches through nexus
points and leaves a gap between neighbouring geometries, so the shared-endpoint
matching the NWM layers rely on finds no network here — branch derivation reads
``toid`` instead, whose numeric stem is the downstream flowpath's ``ID``. Branch
derivation then re-derives its own level paths from that network rather than
using ``levpa_id``, so branches mean the same thing across every source.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

import geopandas as gpd

from ..preprocessing.calculate_branch.standardize_network import merge_parts
from ..preprocessing.source_naming import source_name
from . import _ngiab

log = logging.getLogger(__name__)

PathLike = Union[str, Path]
IdLike = Union[str, int]

# Bucket comes from ngiab's FilePaths where installed, so the two stay in step.
HF_PARQUET_BASE = _ngiab.hf_bucket() + "hydrofabrics/community/parquet/"

# The hydrofabric is authored in EPSG:5070, which is also fimbox's default.
HF_CRS = "EPSG:5070"

DEFAULT_IDENTIFIER = "ngen"

# Cached index -> (remote table, columns, filter). No geometry: these exist to
# resolve an AOI to an id set without touching a geometry column.
_INDEXES = {
    "network": (
        "network",
        ("id", "toid", "divide_id", "hf_id", "hf_hydroseq", "mainstem", "vpuid"),
        "id IS NOT NULL",
    ),
    "centroids": (
        "divide-attributes",
        ("divide_id", "centroid_x", "centroid_y"),
        "centroid_x IS NOT NULL",
    ),
    "gages": ("flowpath-attributes", ("id", "gage"), "gage IS NOT NULL"),
}

_DIGITS = re.compile(r"(\d+)")


def _as_wb(feature_id: IdLike) -> str:
    """Normalise any catchment reference to the ``wb-N`` network key.

    ``cat-1096367``, ``wb-1096367`` and ``1096367`` share a stem, which is also
    what lets catchments join to reaches on one canonical ``ID``.
    """
    match = _DIGITS.search(str(feature_id))
    if match is None:
        raise ValueError(f"cannot read a hydrofabric id out of {feature_id!r}")
    return f"wb-{match.group(1)}"


def _quote(values: Iterable) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


def _cache_dir() -> Path:
    """Index cache, following :mod:`fimbox.datasets`: the OS cache dir,
    overridable with ``$FIMBOX_DATA_DIR``."""
    override = os.environ.get("FIMBOX_DATA_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        import pooch  # lazy: only used to locate the OS cache dir

        base = Path(pooch.os_cache("fimbox"))
    path = base / "ngen_hydrofabric"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class NgenSelection:
    """A resolved set of ngen features, before any geometry is fetched.

    Both key sets are carried because each prunes a different sorted column:
    ``wb_ids`` prunes ``flowpaths.id``, ``divide_ids`` prunes
    ``divides.divide_id``. Deriving one from the other by string surgery would
    drop divides that have no flowline.
    """

    wb_ids: list[str] = field(default_factory=list)
    divide_ids: list[str] = field(default_factory=list)
    requested: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.divide_ids)

    @property
    def is_empty(self) -> bool:
        return not self.divide_ids and not self.wb_ids


class NgenHydrofabric:
    """Query the community ngen hydrofabric parquet mirror for one AOI.

    Parameters
    ----------
    epsg : int, optional
        CRS of the returned GeoDataFrames. Default 5070 (native, no reprojection).
    cache_dir : str or Path, optional
        Index cache location. Defaults to the fimbox cache dir.
    refresh : bool, optional
        Re-fetch cached indexes; use after an upstream hydrofabric release.
    base_url : str, optional
        Override the parquet mirror, e.g. to point at a local copy.
    use_ngiab : bool, optional
        Walk the network with ``ngiab_data_preprocess`` instead of the parquet
        index, for semantics identical to ngiab's own subsetter. Off by default:
        ngiab is not a fimbox dependency (it cannot be co-resolved with teehr),
        and its graph is a separate 42 MB download.
    """

    def __init__(
        self,
        epsg: int = 5070,
        cache_dir: Optional[PathLike] = None,
        refresh: bool = False,
        base_url: str = HF_PARQUET_BASE,
        use_ngiab: bool = False,
    ):
        self.epsg = epsg
        self.base_url = base_url.rstrip("/") + "/"
        self.cache_dir = Path(cache_dir) if cache_dir else _cache_dir()
        self.refresh = refresh
        self.use_ngiab = use_ngiab
        self._con = None
        self._loaded: set[str] = set()

    # connection + index cache
    def _remote(self, table: str) -> str:
        return f"{self.base_url}{table}.parquet"

    @property
    def con(self):
        """Lazily opened duckdb connection with httpfs + spatial loaded."""
        if self._con is None:
            try:
                import duckdb
            except ImportError as exc:
                raise ImportError(
                    "the ngen hydrofabric needs duckdb — `pip install duckdb`."
                ) from exc
            con = duckdb.connect()
            for ext in ("httpfs", "spatial"):
                try:
                    con.execute(f"INSTALL {ext}; LOAD {ext};")
                except Exception:
                    # Already bundled, or offline; the first query reports it.
                    log.debug(f"duckdb extension {ext} unavailable", exc_info=True)
            self._con = con
        return self._con

    def _index(self, name: str) -> None:
        """Cache index ``name`` locally and register it as a view."""
        if name in self._loaded:
            return

        table, cols, where = _INDEXES[name]
        local = self.cache_dir / f"{name}.parquet"
        if self.refresh and local.exists():
            local.unlink()

        if not local.exists():
            log.info(f"Caching ngen {name} index --> {local.name}")
            # The projection means only these columns cross the network.
            projection = ", ".join(f'"{c}"' for c in cols)
            self.con.execute(
                f"COPY (SELECT {projection} FROM read_parquet('{self._remote(table)}') "
                f"WHERE {where}) TO '{local}' (FORMAT PARQUET)"
            )

        self.con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{local}')"
        )
        self._loaded.add(name)

    def _query(self, sql: str) -> list[tuple]:
        return self.con.execute(sql).fetchall()

    # id resolution
    def _traverse(
        self, wb_ids: Sequence[str], include_outlet: bool = True
    ) -> NgenSelection:
        """Everything upstream of ``wb_ids``, as both wb and divide keys.

        ``include_outlet`` mirrors ngiab's ``--subset_type``: set (``nexus``) the
        walk is rooted at each seed's downstream node, so the subset is
        everything draining into that nexus, siblings included; unset
        (``catchment``) it starts at the seed itself.
        """
        if self.use_ngiab:
            via_ngiab = _ngiab.upstream(wb_ids, include_outlet)
            if via_ngiab is not None:
                return NgenSelection(wb_ids=via_ngiab[0], divide_ids=via_ngiab[1])
            log.warning(
                "use_ngiab=True but ngiab_data_preprocess is not installed — "
                "walking the parquet network index instead."
            )

        self._index("network")
        root = "toid" if include_outlet else "id"
        rows = self._query(f"""
            WITH RECURSIVE up(id) AS (
                SELECT DISTINCT {root} AS id FROM network WHERE id IN ({_quote(wb_ids)})
                UNION
                SELECT n.id FROM network n JOIN up ON n.toid = up.id
            )
            SELECT DISTINCT n.id, n.divide_id FROM network n
            WHERE n.id IN (SELECT id FROM up)
        """)
        return NgenSelection(
            wb_ids=sorted({r[0] for r in rows if r[0] and r[0].startswith("wb-")}),
            divide_ids=sorted({r[1] for r in rows if r[1]}),
        )

    def _from_seeds(
        self,
        requested: list[str],
        keys: list,
        wb_by_key: dict,
        label: str,
        include_outlet: bool,
    ) -> NgenSelection:
        """Shared tail of every id selector: report unresolved ids, then walk."""
        missing = [r for r, k in zip(requested, keys) if k not in wb_by_key]
        if missing:
            log.warning(f"{len(missing)} {label} not in the hydrofabric: {missing}")

        wb = sorted({wb_by_key[k] for k in keys if k in wb_by_key})
        selection = self._traverse(wb, include_outlet) if wb else NgenSelection()
        selection.requested, selection.missing = requested, missing
        return selection

    def select_by_cat_ids(
        self, cat_ids: Sequence[IdLike], include_outlet: bool = True
    ) -> NgenSelection:
        """Resolve catchment ids — ``cat-123``, ``wb-123``, or bare ``123``."""
        requested = [str(c).strip() for c in cat_ids if str(c).strip()]
        keys = [_as_wb(c) for c in requested]

        self._index("network")
        found = self._query(
            f"SELECT DISTINCT id FROM network WHERE id IN ({_quote(keys)})"
        )
        return self._from_seeds(
            requested,
            keys,
            {r[0]: r[0] for r in found},
            "catchment id(s)",
            include_outlet,
        )

    def select_by_feature_ids(
        self, feature_ids: Sequence[IdLike], include_outlet: bool = True
    ) -> NgenSelection:
        """Resolve NWM / NHD feature ids (comids) through ``network.hf_id``.

        This is what lets an ngen run start from the same reach ids as an NWM run.
        """
        requested = [str(f).strip() for f in feature_ids if str(f).strip()]
        # hf_id is stored as DOUBLE; compare on the integer so callers can pass
        # ints, strings, or floats interchangeably.
        keys = [int(float(f)) for f in requested]

        self._index("network")
        found = self._query(f"""
            SELECT DISTINCT CAST(hf_id AS BIGINT), id FROM network
            WHERE hf_id IS NOT NULL AND id LIKE 'wb-%'
              AND CAST(hf_id AS BIGINT) IN ({_quote(keys)})
        """)
        return self._from_seeds(
            requested, keys, dict(found), "feature id(s)", include_outlet
        )

    def select_by_gages(self, gages: Sequence[IdLike]) -> NgenSelection:
        """Resolve USGS gage ids via ``flowpath-attributes.gage``.

        The walk stops at the gage: it marks the outlet of interest, so reaching
        across its nexus would pull in the neighbouring catchment.
        """
        requested, keys = [], []
        for raw in gages:
            digits = "".join(c for c in str(raw) if c.isdigit())
            if digits:
                requested.append(str(raw).strip())
                # Hydrofabric gages are zero-padded to at least 8 digits.
                keys.append(f"{int(digits):08d}" if len(digits) < 8 else digits)

        self._index("gages")
        found = self._query(
            f"SELECT DISTINCT gage, id FROM gages WHERE gage IN ({_quote(keys)})"
        )
        return self._from_seeds(
            requested, keys, {g: i for g, i in found if i}, "gage(s)", False
        )

    def select_by_boundary(
        self,
        boundary: Union[PathLike, gpd.GeoDataFrame],
        boundary_layer: Optional[str] = None,
    ) -> NgenSelection:
        """Every catchment whose centroid falls inside ``boundary``.

        Centroids come from the cached ``divide-attributes`` index, not the divide
        geometry: that column has no bbox statistics, so a spatial predicate
        against it would stream the national layer. Selecting on centroids also
        keeps a divide straddling the AOI edge whole, matching the
        intersect-never-clip rule the NWM downloads follow.
        """
        from ..preprocessing.download_data.nhdplus import _boundary_to_geom

        geom = _boundary_to_geom(boundary, boundary_layer)
        aoi = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(HF_CRS).iloc[0]
        minx, miny, maxx, maxy = aoi.bounds

        self._index("centroids")
        # Bbox in SQL, exact point-in-polygon in shapely: avoids pushing a
        # complex AOI polygon through a WKT round-trip.
        rows = self._query(f"""
            SELECT divide_id, centroid_x, centroid_y FROM centroids
            WHERE centroid_x BETWEEN {minx} AND {maxx}
              AND centroid_y BETWEEN {miny} AND {maxy}
        """)
        if not rows:
            return NgenSelection()

        inside = gpd.GeoSeries(
            gpd.points_from_xy([r[1] for r in rows], [r[2] for r in rows]), crs=HF_CRS
        ).within(aoi)
        divide_ids = sorted({r[0] for r, keep in zip(rows, inside) if keep})
        if not divide_ids:
            return NgenSelection()

        self._index("network")
        wb = sorted(
            r[0]
            for r in self._query(
                f"SELECT DISTINCT id FROM network WHERE id LIKE 'wb-%' "
                f"AND divide_id IN ({_quote(divide_ids)})"
            )
        )
        log.info(f"boundary selected {len(divide_ids)} ngen catchment(s)")
        return NgenSelection(wb_ids=wb, divide_ids=divide_ids)

    # geometry fetch
    @staticmethod
    def _id_filter(key: str, ids: list[str]) -> str:
        """Row-group-pruning predicate. BETWEEN is what prunes -- the key column
        is sorted across row groups, so the bounds limit which groups are read;
        IN then makes the result exact."""
        return f"{key} BETWEEN '{ids[0]}' AND '{ids[-1]}' AND {key} IN ({_quote(ids)})"

    def _read_geom(self, sql: str) -> gpd.GeoDataFrame:
        """Run a query whose ``wkb`` column holds the geometry."""
        df = self.con.execute(sql).df()
        if df.empty:
            return gpd.GeoDataFrame()
        # ST_AsWKB arrives as bytearray; shapely.from_wkb only takes bytes/str.
        wkb = df.pop("wkb").map(lambda b: bytes(b) if b is not None else None)
        gdf = gpd.GeoDataFrame(
            df, geometry=gpd.GeoSeries.from_wkb(wkb, crs=HF_CRS), crs=HF_CRS
        )
        return gdf.to_crs(self.epsg)

    def fetch_flowlines(self, selection: NgenSelection) -> gpd.GeoDataFrame:
        """Flowpath geometry for a selection, in the canonical schema."""
        if not selection.wb_ids:
            return gpd.GeoDataFrame()

        ids = _quote(selection.wb_ids)
        self._index("network")
        # arg_min takes hf_id at the lowest hf_hydroseq, matching how ngiab's
        # get_cat_to_nhd_feature_id collapses several NHD features to one.
        gdf = self._read_geom(f"""
            WITH hf AS (
                SELECT id, arg_min(CAST(hf_id AS BIGINT), hf_hydroseq) AS feature_id
                FROM network WHERE hf_id IS NOT NULL AND id IN ({ids}) GROUP BY id
            )
            SELECT
                CAST(regexp_extract(f.id, '(\\d+)', 1) AS BIGINT) AS ID,
                f."order"                  AS order_,
                CAST(f.mainstem AS BIGINT) AS levpa_id,
                hf.feature_id              AS feature_id,
                f.id                       AS wb_id,
                f.divide_id, f.toid, f.hydroseq, f.lengthkm,
                f.tot_drainage_areasqkm, f.vpuid,
                ST_AsWKB(f.geom)           AS wkb
            FROM read_parquet('{self._remote("flowpaths")}') f
            LEFT JOIN hf ON hf.id = f.id
            WHERE {self._id_filter("f.id", selection.wb_ids)}
        """)
        # Flowpaths are stored as MultiLineString, one part per reach. The NWM /
        # NHDPlus layers this stands in for are plain LineStrings, and several
        # downstream steps read ``.coords`` — which shapely refuses on a multi —
        # so the parts are stitched here, at the schema boundary.
        if not gdf.empty:
            gdf["geometry"] = merge_parts(gdf.geometry.values)
        log.info(f"ngen flowlines: {len(gdf)} reach(es)")
        return gdf

    def fetch_catchments(self, selection: NgenSelection) -> gpd.GeoDataFrame:
        """Divide geometry for a selection, keyed on the canonical ``ID``."""
        if not selection.divide_ids:
            return gpd.GeoDataFrame()

        gdf = self._read_geom(f"""
            SELECT
                CAST(regexp_extract(divide_id, '(\\d+)', 1) AS BIGINT) AS ID,
                divide_id, id AS wb_id, toid, areasqkm,
                tot_drainage_areasqkm, has_flowline, vpuid,
                ST_AsWKB(geom) AS wkb
            FROM read_parquet('{self._remote("divides")}')
            WHERE {self._id_filter("divide_id", selection.divide_ids)}
        """)
        log.info(f"ngen catchments: {len(gdf)} divide(s)")
        return gdf

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
            self._loaded.clear()

    def __enter__(self) -> "NgenHydrofabric":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def getNgenData(
    boundary: Optional[Union[PathLike, gpd.GeoDataFrame]] = None,
    boundary_layer: Optional[str] = None,
    out_dir: Optional[PathLike] = None,
    epsg: int = 5070,
    download_flowlines: bool = True,
    download_catchments: bool = True,
    identifier: str = DEFAULT_IDENTIFIER,
    cat_ids: Optional[Sequence[IdLike]] = None,
    feature_ids: Optional[Sequence[IdLike]] = None,
    gages: Optional[Sequence[IdLike]] = None,
    include_outlet: bool = True,
    refresh_index: bool = False,
    use_ngiab: bool = False,
) -> dict:
    """
    ngen hydrofabric flowpaths + divides for an AOI, saved under the same
    filenames as the NWM / NHDPlus HR downloads.

    One selector is used, in precedence order ``cat_ids``, ``feature_ids``,
    ``gages``, ``boundary``. The id selectors walk the network upstream from their
    seeds; ``boundary`` takes every catchment whose centroid falls inside the
    polygon, so a HUC8 boundary gives that HUC8's catchments.

    Parameters
    ----------
    boundary : file path, GeoDataFrame, or shapely geometry
        AOI polygon; used when no id selector is given.
    boundary_layer : layer name when ``boundary`` is a multi-layer GeoPackage
    out_dir : directory to save outputs; None returns the data without saving
    epsg : output CRS (default 5070, the hydrofabric's native CRS)
    download_flowlines, download_catchments : per-layer toggles
    identifier : filename prefix (default ``"ngen"`` ->
        ``ngen_subset_streams.gpkg``, ``ngen_catchments_proj_subset.gpkg``)
    cat_ids : ngen catchment ids — ``cat-123``, ``wb-123``, or bare ``123``
    feature_ids : NWM / NHD feature ids (comids), via ``network.hf_id``
    gages : USGS gage ids, via ``flowpath-attributes.gage``
    include_outlet : mirrors ngiab's ``--subset_type``. True (``nexus``) takes
        everything draining into the seed's downstream nexus; False
        (``catchment``) stops at the seed. Always False for ``gages``.
    refresh_index : re-fetch the cached id indexes
    use_ngiab : walk the network with ``ngiab_data_preprocess`` where installed

    Returns
    -------
    dict with keys ``"flowlines"``, ``"catchments"`` (GeoDataFrames or None)
    """
    if not any([cat_ids, feature_ids, gages, boundary is not None]):
        raise ValueError(
            "the ngen source needs cat_ids, feature_ids, gages, or boundary."
        )

    results: dict = {"flowlines": None, "catchments": None}

    with NgenHydrofabric(epsg=epsg, refresh=refresh_index, use_ngiab=use_ngiab) as hf:
        if cat_ids:
            what, selection = (
                "catchment id(s)",
                hf.select_by_cat_ids(cat_ids, include_outlet),
            )
        elif feature_ids:
            what, selection = (
                "feature id(s)",
                hf.select_by_feature_ids(feature_ids, include_outlet),
            )
        elif gages:
            what, selection = "gage(s)", hf.select_by_gages(gages)
        else:
            what, selection = (
                "boundary",
                hf.select_by_boundary(boundary, boundary_layer),
            )
        log.info(f"--- ngen hydrofabric: {what} ---")

        if selection.is_empty:
            log.warning("ngen hydrofabric: nothing selected for this AOI.")
            return results

        log.info(
            f"ngen selection: {len(selection.divide_ids)} catchment(s), "
            f"{len(selection.wb_ids)} reach(es)"
        )

        out_dir = Path(out_dir) if out_dir else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        layers = [
            ("flowlines", download_flowlines, "streams", hf.fetch_flowlines),
            ("catchments", download_catchments, "catchments", hf.fetch_catchments),
        ]
        for key, wanted, kind, fetch in layers:
            if not wanted:
                continue
            try:
                gdf = fetch(selection)
                if gdf is None or gdf.empty:
                    log.warning(f"ngen {key}: no features returned.")
                    continue
                results[key] = gdf
                if out_dir:
                    out_name = source_name(kind, identifier)
                    gdf.to_file(out_dir / out_name, layer=key, driver="GPKG")
                    log.info(f"{key} --> {out_name}")
            except Exception as exc:
                log.error(f"ngen {key} fetch failed: {exc}", exc_info=True)

    return results


# CLI
if __name__ == "__main__":
    import argparse

    from ..logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="Download ngen hydrofabric flowpaths / divides for an AOI."
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--boundary", help="AOI polygon (gpkg/shp/geojson)")
    selector.add_argument("--cat-ids", help="comma-separated ngen catchment ids")
    selector.add_argument("--feature-ids", help="comma-separated NWM feature ids")
    selector.add_argument("--gages", help="comma-separated USGS gage ids")
    parser.add_argument("--boundary-layer", default=None)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--epsg", type=int, default=5070)
    parser.add_argument("--identifier", default=DEFAULT_IDENTIFIER)
    parser.add_argument("--no-flowlines", action="store_true")
    parser.add_argument("--no-catchments", action="store_true")
    parser.add_argument(
        "--subset-type",
        choices=["nexus", "catchment"],
        default="nexus",
        help="nexus (default): everything draining into the seed's outlet; "
        "catchment: stop at the seed",
    )
    parser.add_argument("--refresh-index", action="store_true")
    parser.add_argument(
        "--use-ngiab",
        action="store_true",
        help="walk the network with ngiab_data_preprocess where installed",
    )
    args = parser.parse_args()

    def _split(value):
        return [v.strip() for v in value.split(",") if v.strip()] if value else None

    getNgenData(
        boundary=args.boundary,
        boundary_layer=args.boundary_layer,
        out_dir=args.out_dir,
        epsg=args.epsg,
        download_flowlines=not args.no_flowlines,
        download_catchments=not args.no_catchments,
        identifier=args.identifier,
        cat_ids=_split(args.cat_ids),
        feature_ids=_split(args.feature_ids),
        gages=_split(args.gages),
        include_outlet=args.subset_type == "nexus",
        refresh_index=args.refresh_index,
        use_ngiab=args.use_ngiab,
    )
