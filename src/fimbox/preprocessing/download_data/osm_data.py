"""
Author: Supath Dhital
Date updated: July 2026

Download major road segments AND bridge features from OpenStreetMap (OSM) within
a user-provided boundary.

Both downloaders share one Overpass client (:class:`_OverpassClient`) that:
  - rotates over several public Overpass mirrors and *permanently drops* a host
    for the session as soon as it refuses a connection or times out on connect,
    so a dead mirror costs one failed socket instead of every retry;
  - probes all mirrors once, in parallel, before the first real query, and keeps
    only the reachable ones (some networks block *.overpass-api.de outright);
  - treats an Overpass ``remark`` ("runtime error: Query timed out") as a
    failure. Overpass returns those inside an HTTP 200 body, so accepting the
    response at face value silently yields partial data.

Coverage strategy (identical for roads and bridges):
  - the AOI bbox is split into tiles of ~``_TILE_AREA_DEG_SQ`` sq-degrees;
  - tiles that do not touch the AOI polygon are dropped before any request
    (a watershed is never a rectangle — this is free speed);
  - tiles are fetched concurrently, spread round-robin across live mirrors;
  - a tile that fails every mirror is split into quadrants and retried
    (``_MAX_SUBSPLIT_DEPTH`` times) — this is what makes large AOIs work;
  - if tiles still fail, the download raises instead of writing a file that
    looks complete but has holes in it (set ``allow_partial=True`` to override).

Roads
  - highway types: motorway, trunk, primary, secondary, tertiary
  - explicitly EXCLUDES bridges (ways with bridge=*) to avoid unrealistic flood
    depth calcs
Bridges
  - ways carrying a bridge tag (bridge=no excluded), queried straight from
    Overpass rather than through osmnx, so bridges get the same tiling,
    mirror failover and quadrant-retry as roads
  - keeps a fixed, curated tag schema (no list columns, no duplicate-cased
    FIXME columns — the two things that used to break GPKG writes)
  - removes abandoned/proposed/demolished bridges based on bridge_type
  - dissolves touching bridge segments (buffer + graph connectivity) to form
    continuous bridge lines

Shared
  - boundary input can be: shapefile/gpkg/geojson path, GeoDataFrame/GeoSeries,
    shapely geometry, or bbox
  - output is saved to GeoPackage in EPSG:5070
  - user can pass out_dir, out_name (or ourfile), out_layer (or ourlayer)
"""

import logging
import math
import random
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import geopandas as gpd
import pandas as pd
import requests
from networkx import Graph, connected_components
from requests.adapters import HTTPAdapter
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import unary_union
from tqdm import tqdm

from .utils import select_intersecting

log = logging.getLogger(__name__)


# Public planet-wide Overpass instances, best-first. Availability varies by
# network: some campus/VPN routes refuse *.overpass-api.de outright, so the
# non-.de mirrors are not optional extras — they are what keeps the download
# working. Only add hosts that serve the whole planet: a region-limited
# instance (overpass.osm.ch, for example, is Switzerland only) answers HTTP 200
# with zero features for a US query, which reads as "no roads here".
OVERPASS_MIRRORS: Tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

_USER_AGENT = "fimbox/0.1 (+https://github.com/sdmlua/fimbox)"

# A mirror whose OSM base timestamp is missing, unparseable, or older than this
# is out of sync with the planet; its answers are not trustworthy.
_MAX_MIRROR_LAG_DAYS = 21


class _TileFailed(Exception):
    """One tile could not be fetched from any live mirror."""


class _Transient(Exception):
    """Mirror answered but the answer is unusable (rate limit, timeout, junk)."""


class _StaleMirror(Exception):
    """Mirror is up but its data is out of date or not planet-wide."""


class _OverpassClient:
    """Mirror-failover Overpass client.

    Mirror health is tracked at class level so the roads download and the
    bridges download that follows it share one probe and one dead-host list —
    bridges never re-learn that a host is down.
    """

    # Tiny query over a busy US interchange: cheap, and a mirror that serves
    # only a non-US region answers it with zero ways, which unmasks it.
    _PROBE = (
        "[out:json][timeout:10];"
        'way["highway"](33.7520,-84.3900,33.7620,-84.3800);out count;'
    )

    _PROBE_BUDGET_S = 6.0  # total wall clock allowed for the one-off probe

    _state_lock = threading.Lock()
    _live: Optional[List[str]] = None  # probed, reachable mirrors
    _dead: set = set()
    _rr = 0  # round-robin cursor, shared so parallel tiles spread out

    def __init__(
        self,
        timeout: int = 180,
        max_attempts: int = 4,
        sleep_base: float = 2.0,
        mirrors: Optional[Sequence[str]] = None,
    ):
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep_base = sleep_base
        self.mirrors = list(mirrors) if mirrors else list(OVERPASS_MIRRORS)

        self._session = requests.Session()
        self._session.headers["User-Agent"] = _USER_AGENT
        self._session.headers["Accept-Encoding"] = "gzip, deflate"
        # one pooled connection per worker per mirror
        adapter = HTTPAdapter(pool_connections=len(self.mirrors), pool_maxsize=16)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # mirror health

    def _probe_one(self, mirror: str) -> bool:
        """True if the host is usable: reachable, in sync with the planet, and
        actually holding data outside its own region. A 429 still counts as
        usable — rate-limited now, likely free by the time we need it."""
        try:
            r = self._session.get(mirror, params={"data": self._PROBE}, timeout=(3, 6))
            if r.status_code in (429, 502, 503, 504):
                return True  # up, just busy
            if r.status_code >= 400:
                return False
            data = r.json()
            _assert_fresh(data)
            elems = data.get("elements") or []
            ways = int((elems[0].get("tags") or {}).get("ways", 0)) if elems else 0
            if ways == 0:
                log.warning(
                    "Overpass: %s returned no data for a known-populated area "
                    "(region-limited mirror?) — skipping it",
                    _host(mirror),
                )
                return False
            return True
        except _StaleMirror as exc:
            log.warning("Overpass: %s %s — skipping it", _host(mirror), exc)
            return False
        except Exception:
            return False

    def live_mirrors(self) -> List[str]:
        """Reachable mirrors, probed once per process and shared from then on.

        The probe runs on a stopwatch: hosts that answer inside
        ``_PROBE_BUDGET_S`` decide the list, and a host still thinking when the
        budget runs out is simply left out of it. Two of the public mirrors
        accept the connection and then hang, and waiting on them would cost more
        than the download itself.
        """
        with self._state_lock:
            if _OverpassClient._live is not None:
                live = [m for m in _OverpassClient._live if m not in self._dead]
                return live or list(self.mirrors)

        live: List[str] = []
        answered: set = set()
        pool = ThreadPoolExecutor(max_workers=len(self.mirrors))
        futures = {pool.submit(self._probe_one, m): m for m in self.mirrors}
        try:
            for fut in as_completed(futures, timeout=self._PROBE_BUDGET_S):
                mirror = futures[fut]
                answered.add(mirror)
                if fut.result():
                    live.append(mirror)  # fastest responders end up first
        except FutureTimeout:
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        with self._state_lock:
            _OverpassClient._live = live
            # Only hosts that actually answered are condemned; the silent ones
            # stay in reserve for the fallback path below.
            _OverpassClient._dead |= answered - set(live)

        if live:
            log.info(
                "Overpass: %d/%d mirror(s) reachable (%s)",
                len(live),
                len(self.mirrors),
                ", ".join(_host(m) for m in live),
            )
        else:
            # Every probe failed. Do not give up here — the probe may have hit a
            # transient outage; fall back to the full list and let real queries
            # produce a real error message.
            log.warning(
                "Overpass: no mirror answered the health probe — trying all %d anyway",
                len(self.mirrors),
            )
            return list(self.mirrors)
        return live

    def _mark_dead(self, mirror: str) -> None:
        with self._state_lock:
            if mirror not in _OverpassClient._dead:
                _OverpassClient._dead.add(mirror)
                log.warning("Overpass: dropping unreachable mirror %s", _host(mirror))
            if _OverpassClient._live is not None:
                _OverpassClient._live = [
                    m for m in _OverpassClient._live if m != mirror
                ]

    def _next_start(self) -> int:
        with self._state_lock:
            _OverpassClient._rr += 1
            return _OverpassClient._rr

    # fetch

    def fetch(self, query: str, avoid: Optional[str] = None) -> Tuple[dict, str]:
        """Run ``query`` against live mirrors in turn.

        Returns ``(json, mirror_used)``. ``avoid`` skips one mirror, which is how
        an empty answer gets a second opinion from a different host.
        Raises ``_TileFailed`` when every mirror fails.
        """
        mirrors = [m for m in self.live_mirrors() if m != avoid]
        if not mirrors:
            raise _TileFailed("no Overpass mirror is reachable")

        attempts = max(self.max_attempts, len(mirrors))
        start = self._next_start()
        errors: List[str] = []

        for attempt in range(attempts):
            mirror = mirrors[(start + attempt) % len(mirrors)]
            if mirror in self._dead:
                continue
            try:
                r = self._session.get(
                    mirror,
                    params={"data": query},
                    timeout=(10, self.timeout + 60),
                )
                if r.status_code in (429, 502, 503, 504):
                    raise _Transient(f"HTTP {r.status_code}")
                r.raise_for_status()
                data = r.json()  # ValueError if the body was truncated
                remark = str(data.get("remark", ""))
                if remark and ("error" in remark.lower() or "timed out" in remark):
                    # Overpass reports query timeouts *inside* a 200 body. The
                    # payload is partial, so it must not be treated as data.
                    raise _Transient(f"overpass remark: {remark}")
                if "elements" not in data:
                    raise _Transient("response has no 'elements'")
                _assert_fresh(data)
                return data, mirror

            except _StaleMirror as exc:
                self._mark_dead(mirror)
                errors.append(f"{_host(mirror)}: {exc}")
                continue  # its data is wrong, not just late
            except requests.exceptions.ReadTimeout as exc:
                # Host is alive, the query is too heavy for it — keep the mirror,
                # let the caller shrink the tile.
                errors.append(f"{_host(mirror)}: read timeout")
                _ = exc
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.ConnectTimeout,
            ) as exc:
                self._mark_dead(mirror)
                errors.append(f"{_host(mirror)}: unreachable ({_brief(exc)})")
                continue  # dead host: no backoff, move on immediately
            except Exception as exc:
                errors.append(f"{_host(mirror)}: {_brief(exc)}")

            if attempt < attempts - 1:
                time.sleep(self.sleep_base * (attempt + 1) + random.uniform(0, 1.0))

        raise _TileFailed("; ".join(errors) or "all mirrors failed")


def _assert_fresh(data: dict) -> None:
    """Reject a response whose OSM base timestamp is missing, malformed or old.

    Every healthy instance stamps ``osm3s.timestamp_osm_base`` with the moment
    its planet copy was last updated. A mirror that is broken or serving a stale
    regional extract gets this wrong, and its empty answers look exactly like
    real ones — so the timestamp is the cheapest honest signal we have.
    """
    raw = str((data.get("osm3s") or {}).get("timestamp_osm_base", "")).strip()
    if not raw:
        raise _StaleMirror("gave no OSM base timestamp")
    try:
        base = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _StaleMirror(f"gave a malformed OSM base timestamp ({raw!r})") from None
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    lag = (datetime.now(timezone.utc) - base).days
    if lag > _MAX_MIRROR_LAG_DAYS:
        raise _StaleMirror(f"is {lag} days out of date (base {raw})")


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def _brief(exc: Exception, limit: int = 120) -> str:
    msg = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return msg if len(msg) <= limit else msg[: limit - 3] + "..."


BBox = Tuple[float, float, float, float]


def _quad_split(bbox: BBox) -> List[BBox]:
    minx, miny, maxx, maxy = bbox
    mx, my = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    return [
        (minx, miny, mx, my),
        (mx, miny, maxx, my),
        (minx, my, mx, maxy),
        (mx, my, maxx, maxy),
    ]


# shared boundary IO + tiled fetching (used by both roads and bridges)
@dataclass
class _OSMBoundaryIO:
    out_sr: int = 5070

    def _read_boundary_file(
        self, path: Union[str, Path], layer: Optional[str] = None
    ) -> gpd.GeoDataFrame:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)

        if path.suffix.lower() == ".gpkg":
            if layer is None:
                layers = gpd.list_layers(path)
                if layers is None or len(layers) == 0:
                    raise ValueError(f"No layers found in {path}")
                layer = layers.iloc[0]["name"]
            gdf = gpd.read_file(path, layer=layer)
        else:
            gdf = gpd.read_file(path)

        if gdf.empty:
            raise ValueError(f"Boundary file is empty: {path}")
        if gdf.crs is None:
            raise ValueError(f"Boundary CRS is missing: {path}")
        return gdf

    def _boundary_to_geom4326(
        self,
        boundary: Union[
            gpd.GeoDataFrame,
            gpd.GeoSeries,
            Polygon,
            MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float],
            str,
            Path,
        ],
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
    ) -> Union[Polygon, MultiPolygon]:
        # File path
        if isinstance(boundary, (str, Path)):
            gdf = self._read_boundary_file(boundary, layer=boundary_layer)
            geom = unary_union(gdf.to_crs("EPSG:4326").geometry)
            return self._ensure_poly(geom)

        # GeoPandas
        if isinstance(boundary, (gpd.GeoDataFrame, gpd.GeoSeries)):
            if boundary.crs is None:
                raise ValueError("Boundary GeoDataFrame/GeoSeries must have a CRS.")
            geom = unary_union(boundary.to_crs("EPSG:4326").geometry)
            return self._ensure_poly(geom)

        # bbox
        if (
            isinstance(boundary, (tuple, list))
            and len(boundary) == 4
            and all(isinstance(x, (int, float)) for x in boundary)
        ):
            geom = box(*boundary)
            if boundary_crs is not None:
                geom = (
                    gpd.GeoSeries([geom], crs=boundary_crs).to_crs("EPSG:4326").iloc[0]
                )
            return self._ensure_poly(geom)

        # shapely geometry
        if isinstance(boundary, (Polygon, MultiPolygon)):
            geom = boundary
            if boundary_crs is not None:
                geom = (
                    gpd.GeoSeries([geom], crs=boundary_crs).to_crs("EPSG:4326").iloc[0]
                )
            return self._ensure_poly(geom)

        raise TypeError(f"Unsupported boundary type: {type(boundary)}")

    @staticmethod
    def _ensure_poly(geom) -> Union[Polygon, MultiPolygon]:
        if geom.is_empty:
            raise ValueError("Boundary geometry is empty after dissolve/reproject.")
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            raise TypeError(
                f"Boundary must dissolve to Polygon/MultiPolygon, got {geom.geom_type}"
            )
        return geom

    def _write_gpkg(
        self,
        gdf: gpd.GeoDataFrame,
        out_dir: Union[str, Path],
        out_name: str,
        out_layer: str,
    ) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / out_name
        gdf.to_file(out_path, layer=out_layer, driver="GPKG")
        return out_path


@dataclass
class _TiledOverpassDownloader(_OSMBoundaryIO):
    """Tiling, concurrency and failed-tile recovery shared by roads/bridges."""

    timeout: int = 180  # per-tile Overpass timeout (seconds)
    max_attempts: int = 4  # mirror attempts per tile
    sleep_base: float = 2.0  # backoff between attempts (seconds)
    allow_partial: bool = False  # write what we got even if tiles failed
    verify_empty_tiles: bool = True  # confirm "no features" on a second mirror
    mirrors: Optional[Sequence[str]] = None

    # Tile area target in sq-degrees; tiles are sized adaptively from the bbox.
    _TILE_AREA_DEG_SQ: float = 0.25
    _MAX_WORKERS: int = 6
    _MAX_SUBSPLIT_DEPTH: int = 3

    _client: Optional[_OverpassClient] = field(default=None, repr=False)

    # subclasses set these
    _label: str = "features"

    @property
    def client(self) -> _OverpassClient:
        if self._client is None:
            self._client = _OverpassClient(
                timeout=self.timeout,
                max_attempts=self.max_attempts,
                sleep_base=self.sleep_base,
                mirrors=self.mirrors,
            )
        return self._client

    # tiling
    def _make_tiles(
        self, minx: float, miny: float, maxx: float, maxy: float
    ) -> List[BBox]:
        area = (maxx - minx) * (maxy - miny)
        n = max(1, math.ceil(area / self._TILE_AREA_DEG_SQ))
        # distribute tiles to respect the bbox aspect ratio
        aspect = (maxx - minx) / max(maxy - miny, 1e-9)
        ny = max(1, round(math.sqrt(n / aspect)))
        nx = max(1, math.ceil(n / ny))
        dx = (maxx - minx) / nx
        dy = (maxy - miny) / ny
        return [
            (minx + i * dx, miny + j * dy, minx + (i + 1) * dx, miny + (j + 1) * dy)
            for i in range(nx)
            for j in range(ny)
        ]

    def _tiles_for_geom(self, geom4326) -> List[BBox]:
        """Tiles covering the AOI, minus the ones the AOI never touches."""
        tiles = self._make_tiles(*geom4326.bounds)
        if len(tiles) == 1:
            return tiles
        kept = [t for t in tiles if geom4326.intersects(box(*t))]
        if len(kept) < len(tiles):
            log.info(
                "OSM %s: %d/%d tile(s) touch the AOI",
                self._label,
                len(kept),
                len(tiles),
            )
        return kept or tiles

    def _n_workers(self, n_tiles: int) -> int:
        n_live = max(1, len(self.client.live_mirrors()))
        # ~2 concurrent requests per live mirror keeps us fast without getting
        # rate-limited off a single host.
        return max(1, min(self._MAX_WORKERS, n_tiles, 2 * n_live))

    # per-tile hook implemented by subclasses
    def _build_query(self, bbox: BBox) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def _parse(self, osm_json: dict) -> gpd.GeoDataFrame:  # pragma: no cover
        raise NotImplementedError

    def _fetch_tile(self, bbox: BBox) -> gpd.GeoDataFrame:
        query = self._build_query(bbox)
        data, mirror = self.client.fetch(query)
        gdf = self._parse(data)

        # An empty tile is often genuine (no bridges out here), but it is also
        # what a misbehaving mirror returns. When another mirror is available,
        # ask it before believing the hole.
        if gdf.empty and self.verify_empty_tiles:
            others = [m for m in self.client.live_mirrors() if m != mirror]
            if others:
                try:
                    data2, mirror2 = self.client.fetch(query, avoid=mirror)
                except _TileFailed:
                    return gdf
                second = self._parse(data2)
                if not second.empty:
                    log.warning(
                        "Overpass: %s reported tile %s empty but %s returned %d "
                        "feature(s) — trusting %s",
                        _host(mirror),
                        _fmt_bbox(bbox),
                        _host(mirror2),
                        len(second),
                        _host(mirror2),
                    )
                    self.client._mark_dead(mirror)
                    return second
        return gdf

    def _run_pool(
        self, tiles: List[BBox], desc: str
    ) -> Tuple[List[gpd.GeoDataFrame], List[BBox]]:
        parts: List[gpd.GeoDataFrame] = []
        failed: List[BBox] = []
        workers = self._n_workers(len(tiles))

        def one(t: BBox):
            try:
                return t, self._fetch_tile(t), None
            except Exception as exc:
                return t, None, exc

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, t) for t in tiles]
            with tqdm(total=len(tiles), desc=desc, unit="tile") as pbar:
                for fut in as_completed(futures):
                    tile, gdf, exc = fut.result()
                    if exc is not None:
                        failed.append(tile)
                        tqdm.write(f"  [warn] tile {_fmt_bbox(tile)} failed: {exc}")
                    elif gdf is not None and not gdf.empty:
                        parts.append(gdf)
                    pbar.update(1)

        return parts, failed

    def _fetch_all(self, tiles: List[BBox]) -> gpd.GeoDataFrame:
        log.info(
            "OSM %s: %d tile(s), %d worker(s)",
            self._label,
            len(tiles),
            self._n_workers(len(tiles)),
        )
        parts, failed = self._run_pool(tiles, f"OSM {self._label}")

        # A failed tile is usually "too much data for this mirror" — quartering
        # it makes each request small enough to succeed. This is what lets big
        # AOIs finish instead of losing whole blocks of the domain.
        depth = 0
        while failed and depth < self._MAX_SUBSPLIT_DEPTH:
            depth += 1
            sub = [s for t in failed for s in _quad_split(t)]
            log.warning(
                "OSM %s: %d tile(s) failed — retrying as %d smaller tiles (pass %d)",
                self._label,
                len(failed),
                len(sub),
                depth,
            )
            more, failed = self._run_pool(sub, f"OSM {self._label} retry {depth}")
            parts.extend(more)

        if failed:
            msg = (
                f"OSM {self._label}: {len(failed)} tile(s) could not be downloaded "
                f"after {self._MAX_SUBSPLIT_DEPTH} sub-split retries "
                f"(e.g. {_fmt_bbox(failed[0])})"
            )
            if not self.allow_partial:
                # Refuse to write a layer with holes in it — downstream FIM
                # would read the gaps as "no roads/bridges here".
                raise RuntimeError(
                    msg + "; set allow_partial=True to keep partial data"
                )
            log.warning(msg + " — keeping partial data (allow_partial=True)")

        if not parts:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")

    # shared JSON -> lines parsing
    @staticmethod
    def _ways_to_rows(osm_json: dict, tag_cols: Sequence[str]) -> List[dict]:
        """Ways -> row dicts with a fixed column set.

        Handles both ``out geom`` (coords inline on the way) and the
        ``out body;>;out skel qt`` node-lookup form, so the parser survives a
        mirror that ignores the geometry modifier.
        """
        elems = osm_json.get("elements", [])
        nodes = {
            e["id"]: (e["lon"], e["lat"])
            for e in elems
            if e.get("type") == "node" and "lon" in e and "lat" in e
        }

        rows: List[dict] = []
        for e in elems:
            if e.get("type") != "way":
                continue
            geom = e.get("geometry")
            if geom:
                coords = [
                    (p["lon"], p["lat"])
                    for p in geom
                    if p and p.get("lon") is not None and p.get("lat") is not None
                ]
            else:
                coords = [nodes[n] for n in e.get("nodes", []) if n in nodes]
            if len(coords) < 2:
                continue
            tags = e.get("tags") or {}
            row: Dict[str, Any] = {"osmid": str(e["id"])}
            for col in tag_cols:
                row[col] = tags.get(col, "")
            row["geometry"] = LineString(coords)
            rows.append(row)
        return rows


def _fmt_bbox(bbox: BBox) -> str:
    return "({:.3f}, {:.3f}, {:.3f}, {:.3f})".format(*bbox)


@dataclass
class DownloadOSMRoads(_TiledOverpassDownloader):
    _label: str = "roads"

    _HIGHWAY_RE: str = "^motorway$|^trunk$|^primary$|^secondary$|^tertiary$"
    _TAG_COLS: Tuple[str, ...] = ("highway", "name", "ref", "surface", "lanes")

    def _build_query(self, bbox: BBox) -> str:
        # Overpass bbox order: S,W,N,E. `out geom` returns each way's coords
        # inline — smaller payload and no recursive node download.
        minx, miny, maxx, maxy = bbox
        return (
            f"[out:json][timeout:{self.timeout}];"
            f'way["highway"~"{self._HIGHWAY_RE}"][!"bridge"]'
            f"({miny},{minx},{maxy},{maxx});"
            f"out geom;"
        )

    def _parse(self, osm_json: dict) -> gpd.GeoDataFrame:
        rows = self._ways_to_rows(osm_json, self._TAG_COLS)
        if not rows:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
        gdf["highway"] = gdf["highway"].replace("", "unknown")
        return gdf

    # public API
    def query_to_gdf(
        self,
        boundary: Union[
            gpd.GeoDataFrame,
            gpd.GeoSeries,
            Polygon,
            MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float],
            str,
            Path,
        ],
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
        restrict_to_boundary: bool = True,
    ) -> gpd.GeoDataFrame:
        geom4326 = self._boundary_to_geom4326(boundary, boundary_layer, boundary_crs)
        gdf = self._fetch_all(self._tiles_for_geom(geom4326))
        if gdf.empty:
            log.warning("No OSM road features returned.")
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        gdf = gdf.drop_duplicates(subset=["osmid"]).reset_index(drop=True)
        log.info(f"OSM roads: {len(gdf)} unique segments after dedup")

        if restrict_to_boundary:
            # Whole segments that touch the AOI — a road sliced at the boundary
            # would lose the span that carries it across.
            gdf = select_intersecting(
                gdf, gpd.GeoDataFrame(geometry=[geom4326], crs="EPSG:4326")
            )
            log.info(f"OSM roads: {len(gdf)} segments intersecting the AOI")

        return gdf.to_crs(epsg=self.out_sr)

    def download(
        self,
        boundary: Union[
            gpd.GeoDataFrame,
            gpd.GeoSeries,
            Polygon,
            MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float],
            str,
            Path,
        ],
        out_dir: Union[str, Path],
        out_name: str = "osm_roads.gpkg",
        out_layer: str = "osm_roads",
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
        ourfile: Optional[str] = None,
        ourlayer: Optional[str] = None,
    ) -> gpd.GeoDataFrame:
        if ourfile:
            out_name = ourfile
        if ourlayer:
            out_layer = ourlayer
        gdf = self.query_to_gdf(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            restrict_to_boundary=True,
        )
        out_path = self._write_gpkg(gdf, out_dir, out_name, out_layer)
        log.info(f"{out_layer} --> {out_path.name}")
        return gdf


@dataclass
class DownloadOSMBridges(_TiledOverpassDownloader):
    _label: str = "bridges"

    requests_timeout: int = 180  # kept for backwards compatibility
    dissolve_buffer: float = 0.0001  # dissolve happens in EPSG:4326
    drop_list_columns: bool = True  # retained; schema is curated already

    # Only these tags are carried through. osmnx returned the raw OSM schema,
    # which differs per area and routinely broke the GPKG write with list-valued
    # and duplicate-cased columns; a fixed schema removes that failure mode.
    _TAG_COLS: Tuple[str, ...] = ("bridge", "highway", "railway", "name", "layer")

    def __post_init__(self):
        # honour the legacy field name if a caller sets it explicitly
        if self.requests_timeout and self.requests_timeout != self.timeout:
            self.timeout = self.requests_timeout

    def _build_query(self, bbox: BBox) -> str:
        # bridge=no means "explicitly not a bridge", so it is excluded.
        minx, miny, maxx, maxy = bbox
        return (
            f"[out:json][timeout:{self.timeout}];"
            f'way["bridge"]["bridge"!="no"]'
            f"({miny},{minx},{maxy},{maxx});"
            f"out geom;"
        )

    def _parse(self, osm_json: dict) -> gpd.GeoDataFrame:
        rows = self._ways_to_rows(osm_json, self._TAG_COLS)
        if not rows:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return gpd.GeoDataFrame(rows, crs="EPSG:4326")

    @staticmethod
    def _find_touching_groups(gdf: gpd.GeoDataFrame) -> List[set]:
        graph = Graph()
        graph.add_nodes_from(gdf.index)
        spatial_index = gdf.sindex
        for idx, row in gdf.iterrows():
            geom = row.geometry
            cand_idx = list(spatial_index.intersection(geom.bounds))
            cand = gdf.iloc[cand_idx]
            hits = cand[cand.intersects(geom)]
            for midx in hits.index:
                if midx != idx:
                    graph.add_edge(idx, midx)
        return list(connected_components(graph))

    @staticmethod
    def _clean_schema(
        gdf: gpd.GeoDataFrame, drop_list_columns: bool = True
    ) -> gpd.GeoDataFrame:
        if gdf is None or len(gdf) == 0:
            return gdf

        if drop_list_columns:
            cols_to_drop = []
            for col in gdf.columns:
                try:
                    if any(isinstance(v, list) for v in gdf[col].dropna()):
                        cols_to_drop.append(col)
                except Exception:
                    pass
            if cols_to_drop:
                gdf = gdf.drop(columns=list(set(cols_to_drop)))

        bad_column_names = [
            "id",
            "fid",
            "ID",
            "fixme",
            "FIXME",
            "NYSDOT_ref",
            "REF",
            "fixme:maxspeed",
            "LAYER",
            "unsigned_ref",
            "Fut_Ref",
            "Ref",
            "FIXME:ref",
        ]
        cols_to_drop2 = [c for c in bad_column_names if c in gdf.columns]
        if cols_to_drop2:
            gdf = gdf.drop(columns=cols_to_drop2)

        return gdf

    @staticmethod
    def _make_bridge_type(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if "highway" not in gdf.columns:
            gdf["highway"] = None
        if "railway" not in gdf.columns:
            gdf["railway"] = None

        def _label(row) -> str:
            hw, rw = row["highway"], row["railway"]
            if pd.notna(hw) and str(hw) != "":
                return f"highway-{hw}"
            if pd.notna(rw) and str(rw) != "":
                return f"railway-{rw}"
            return "bridge-other"

        gdf["bridge_type"] = gdf.apply(_label, axis=1)
        return gdf

    @staticmethod
    def _filter_unwanted_bridge_types(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        unwanted_bridge_types = [
            "highway-razed",
            "highway-proposed",
            "highway-abandoned",
            "highway-destroyed",
            "highway-dismantled",
            "highway-demolished",
            "railway-razed",
            "railway-proposed",
            "railway-abandoned",
            "railway-destroyed",
            "railway-dismantled",
            "railway-demolished",
        ]
        if "bridge_type" in gdf.columns:
            gdf = gdf[~gdf["bridge_type"].isin(unwanted_bridge_types)]
        return gdf

    @staticmethod
    def _force_lines(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if gdf.empty:
            return gdf

        def to_line(geom):
            if geom is None:
                return None
            gt = geom.geom_type
            if gt in ("LineString", "MultiLineString"):
                return geom
            if gt == "Polygon":
                return LineString(geom.exterior.coords)
            if gt == "MultiPolygon":
                polys = list(geom.geoms)
                if not polys:
                    return None
                p = max(polys, key=lambda x: x.area)
                return LineString(p.exterior.coords)
            return None

        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.apply(to_line)
        gdf = gdf[gdf.geometry.notna()].copy()
        return gdf

    def _dissolve_touching(self, gdf_lines_4326: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if gdf_lines_4326.empty:
            return gdf_lines_4326

        # Buffering in degrees is deliberate: 0.0001 deg is ~11 m, the tolerance
        # that joins two decks of the same crossing. The CRS warning that comes
        # with it is expected, so it is silenced here and nowhere else.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            buffered = gdf_lines_4326.copy()
            buffered["geometry"] = buffered.geometry.buffer(self.dissolve_buffer)

            groups = self._find_touching_groups(buffered)

            dissolved_groups = []
            for grp in groups:
                gg = buffered.loc[list(grp)]
                if gg.empty:
                    continue
                d = gg.dissolve()
                d = d.explode(index_parts=False)
                dissolved_groups.append(d)

        if not dissolved_groups:
            out = buffered.copy()
        else:
            out = gpd.GeoDataFrame(
                pd.concat(dissolved_groups, ignore_index=True), crs=buffered.crs
            )

        # buffered polygons -> linestring exteriors
        out["geometry"] = out.geometry.apply(
            lambda geom: (
                LineString(geom.exterior.coords)
                if geom is not None and geom.geom_type == "Polygon"
                else geom
            )
        )
        out = out[out.geometry.notna()].copy()
        return out

    def query_to_gdf(
        self,
        boundary: Union[
            gpd.GeoDataFrame,
            gpd.GeoSeries,
            Polygon,
            MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float],
            str,
            Path,
        ],
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
        restrict_to_boundary: bool = True,
    ) -> gpd.GeoDataFrame:
        geom4326 = self._boundary_to_geom4326(boundary, boundary_layer, boundary_crs)

        gdf = self._fetch_all(self._tiles_for_geom(geom4326))
        if gdf.empty:
            log.warning("No OSM bridge features returned.")
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        # a bridge on a tile seam comes back from both tiles
        gdf = gdf.drop_duplicates(subset=["osmid"]).reset_index(drop=True)
        log.info(f"OSM bridges: {len(gdf)} unique ways after dedup")

        gdf = self._clean_schema(gdf, drop_list_columns=self.drop_list_columns)
        gdf = self._make_bridge_type(gdf)
        gdf = self._filter_unwanted_bridge_types(gdf)
        gdf = self._force_lines(gdf)

        if gdf.empty:
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        # dissolve touching (in 4326), then select against the AOI (in 4326)
        gdf = gdf.to_crs("EPSG:4326")
        gdf = self._dissolve_touching(gdf)

        if restrict_to_boundary and not gdf.empty:
            # Whole bridges — a deck cut at the boundary would lose the part
            # spanning the channel, which is the part that matters for HAND.
            gdf = select_intersecting(
                gdf, gpd.GeoDataFrame(geometry=[geom4326], crs="EPSG:4326")
            )

        if gdf.empty:
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        log.info(f"OSM bridges: {len(gdf)} bridge lines in the AOI")
        return gdf.to_crs(epsg=self.out_sr)

    def download(
        self,
        boundary: Union[
            gpd.GeoDataFrame,
            gpd.GeoSeries,
            Polygon,
            MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float],
            str,
            Path,
        ],
        out_dir: Union[str, Path],
        out_name: str = "osm_bridges.gpkg",
        out_layer: str = "osm_bridges",
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
        ourfile: Optional[str] = None,
        ourlayer: Optional[str] = None,
    ) -> gpd.GeoDataFrame:
        if ourfile:
            out_name = ourfile
        if ourlayer:
            out_layer = ourlayer

        gdf = self.query_to_gdf(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            restrict_to_boundary=True,
        )
        out_path = self._write_gpkg(gdf, out_dir, out_name, out_layer)
        log.info(f"{out_layer} --> {out_path.name}")
        return gdf


# CLI--> single entry; choose roads vs bridges via --mode
if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Download OSM roads (excluding bridges) OR OSM bridges within a boundary; output EPSG:5070 GPKG."
    )
    p.add_argument(
        "--mode",
        required=True,
        choices=["roads", "bridges"],
        help="Which dataset to download",
    )
    p.add_argument(
        "--boundary",
        required=True,
        help="Boundary file path (shp/gpkg/geojson) OR bbox 'minx,miny,maxx,maxy' (assumed EPSG:4326 unless --boundary_crs given)",
    )
    p.add_argument("--out_dir", required=True, help="Output directory")
    p.add_argument(
        "--out_name",
        default=None,
        help="Output GeoPackage name (defaults depend on mode)",
    )
    p.add_argument(
        "--out_layer", default=None, help="Output layer name (defaults depend on mode)"
    )
    p.add_argument(
        "--boundary_layer",
        default=None,
        help="Boundary layer name if boundary is a GeoPackage",
    )
    p.add_argument(
        "--boundary_crs", default=None, help="CRS for bbox/shapely boundary, e.g. 4326"
    )
    p.add_argument(
        "--allow_partial",
        action="store_true",
        help="Write the layer even if some tiles could not be downloaded",
    )
    args = p.parse_args()

    boundary_val: Any = args.boundary
    boundary_crs_val = int(args.boundary_crs) if args.boundary_crs is not None else None

    # bbox convenience: "minx,miny,maxx,maxy"
    if isinstance(boundary_val, str) and "," in boundary_val:
        parts = [s.strip() for s in boundary_val.split(",")]
        if len(parts) == 4:
            try:
                boundary_val = tuple(float(s) for s in parts)
            except ValueError:
                pass

    if args.mode == "roads":
        dl = DownloadOSMRoads(out_sr=5070, allow_partial=args.allow_partial)
        dl.download(
            boundary=boundary_val,
            out_dir=args.out_dir,
            out_name=args.out_name or "osm_roads.gpkg",
            out_layer=args.out_layer or "osm_roads",
            boundary_layer=args.boundary_layer,
            boundary_crs=boundary_crs_val,
        )
    else:
        dl = DownloadOSMBridges(out_sr=5070, allow_partial=args.allow_partial)
        dl.download(
            boundary=boundary_val,
            out_dir=args.out_dir,
            out_name=args.out_name or "osm_bridges.gpkg",
            out_layer=args.out_layer or "osm_bridges",
            boundary_layer=args.boundary_layer,
            boundary_crs=boundary_crs_val,
        )
