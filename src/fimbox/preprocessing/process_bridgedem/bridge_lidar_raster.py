"""
Author: Supath Dhital
Updated = June 2026
---------------------
Downloads USGS 3DEP LiDAR points for each bridge in a GeoPackage
and IDW-interpolates them into per-bridge elevation rasters.

LiDAR source : USGS 3DEP via Entwine Point Tiles (EPT) on AWS S3
Tile index   : https://github.com/hobuinc/usgs-lidar  (boundaries.topojson)
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading as _threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional, Union

import geopandas as gpd
import numpy as np
import requests
from scipy.spatial import KDTree
from tqdm import tqdm

from .bridge_source import resolve_bridge_gpkg

log = logging.getLogger(__name__)

# LAS classification 13=bridge deck, 17=bridge deck
_BRIDGE_CLASSES = {13, 17}
_ENTWINE_INDEX_URL = "https://raw.githubusercontent.com/hobuinc/usgs-lidar/master/boundaries/boundaries.topojson"

_thread_local = _threading.local()

_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = Lock()


def _session() -> requests.Session:
    """One pooled, retrying session shared by every download in the process.

    A session per thread means a fresh DNS lookup and TLS handshake for each of
    the thousands of short-lived tile threads — more wall-clock than the tile
    download itself, and enough resolver traffic that S3 starts handing back
    intermittent NXDOMAIN. Retries cover those transients so a hiccup costs a
    backoff instead of the whole bridge.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            from urllib3.util.retry import Retry

            s = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=16,
                pool_maxsize=64,
                max_retries=Retry(
                    total=4,
                    connect=4,
                    read=2,
                    backoff_factor=0.4,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset(["GET"]),
                ),
            )
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            _SESSION = s
        return _SESSION


def _transformer(src: str, dst: str):
    """Thread-local pyproj Transformer — building one costs more than it should
    to repeat 3k times, and PROJ objects can't be shared across threads."""
    cache = getattr(_thread_local, "transformers", None)
    if cache is None:
        cache = _thread_local.transformers = {}
    if (src, dst) not in cache:
        from pyproj import Transformer

        cache[(src, dst)] = Transformer.from_crs(src, dst, always_xy=True)
    return cache[(src, dst)]


# EPT manifest + hierarchy cache: fetch once per unique EPT URL, reuse for all bridges
_EPT_CACHE: dict = {}
_EPT_CACHE_LOCK = Lock()


def _ept_meta(base: str) -> tuple[dict, dict]:
    """Return (manifest, hierarchy) for `base`, fetching at most once per URL."""
    with _EPT_CACHE_LOCK:
        if base not in _EPT_CACHE:
            s = _session()
            manifest = s.get(f"{base}/ept.json", timeout=30)
            manifest.raise_for_status()
            hierarchy = s.get(f"{base}/ept-hierarchy/0-0-0-0.json", timeout=30)
            hierarchy.raise_for_status()
            _EPT_CACHE[base] = (manifest.json(), hierarchy.json())
        return _EPT_CACHE[base]


# Decoded-tile cache. Bridges are neighbours far more often than not, so the
# same octree tiles keep coming back: on a HUC8 with ~2.9k bridges, 7.8k tile
# requests resolve to under 2k distinct tiles, and the coarse depth-6 ones are
# asked for over a hundred times each. Tiles are small (~0.5 MB decoded), so
# holding a working set in memory turns most of those requests into a dict hit.
_TILE_MISS = object()
_TILE_CACHE: "OrderedDict[tuple, Optional[np.ndarray]]" = OrderedDict()
_TILE_CACHE_LOCK = Lock()
_TILE_CACHE_BYTES = 0
_TILE_CACHE_MAX_BYTES = 1024 * 1024 * 1024
_TILE_FETCH_LOCKS: dict = {}
_TILE_REQUESTS = 0
_TILE_DOWNLOADS = 0


def _tile_cache_get(key: tuple):
    with _TILE_CACHE_LOCK:
        if key in _TILE_CACHE:
            _TILE_CACHE.move_to_end(key)
            return _TILE_CACHE[key]
    return _TILE_MISS


def _tile_cache_put(key: tuple, pts: Optional[np.ndarray]) -> None:
    global _TILE_CACHE_BYTES
    with _TILE_CACHE_LOCK:
        old = _TILE_CACHE.pop(key, None)
        if old is not None:
            _TILE_CACHE_BYTES -= old.nbytes
        _TILE_CACHE[key] = pts
        _TILE_CACHE_BYTES += 0 if pts is None else pts.nbytes
        while _TILE_CACHE_BYTES > _TILE_CACHE_MAX_BYTES and len(_TILE_CACHE) > 1:
            _, evicted = _TILE_CACHE.popitem(last=False)
            if evicted is not None:
                _TILE_CACHE_BYTES -= evicted.nbytes


def _tile_fetch_lock(key: tuple) -> Lock:
    with _TILE_CACHE_LOCK:
        lk = _TILE_FETCH_LOCKS.get(key)
        if lk is None:
            lk = _TILE_FETCH_LOCKS[key] = Lock()
        return lk


def _download_tile(base: str, tile_key: str) -> Optional[np.ndarray]:
    """Download + decode one EPT .laz tile.

    Returns every last-return point in the tile as (N,4) [x,y,z,cls] in
    EPSG:3857 — unclipped, so the result is reusable by any bridge that lands
    in this tile — or None if the tile is absent or has no last returns.
    """
    import laspy

    global _TILE_DOWNLOADS
    resp = _session().get(f"{base}/ept-data/{tile_key}.laz", timeout=60)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    with _TILE_CACHE_LOCK:
        _TILE_DOWNLOADS += 1

    # Decoding from memory: the tile is already fully buffered, so a temp file
    # only adds a write and a read.
    las = laspy.read(io.BytesIO(resp.content))
    last = np.asarray(las.return_number) == np.asarray(las.number_of_returns)
    if not last.any():
        return None
    return np.column_stack(
        [
            np.asarray(las.x)[last],
            np.asarray(las.y)[last],
            np.asarray(las.z)[last],
            np.asarray(las.classification)[last].astype(float),
        ]
    )


def _tile_points(base: str, tile_key: str) -> Optional[np.ndarray]:
    """Cached last-return points for one EPT tile, downloading it at most once."""
    global _TILE_REQUESTS
    key = (base, tile_key)
    with _TILE_CACHE_LOCK:
        _TILE_REQUESTS += 1

    pts = _tile_cache_get(key)
    if pts is not _TILE_MISS:
        return pts

    # Per-tile lock so concurrent bridges wanting the same tile fetch it once.
    with _tile_fetch_lock(key):
        pts = _tile_cache_get(key)
        if pts is not _TILE_MISS:
            return pts
        pts = _download_tile(base, tile_key)
        _tile_cache_put(key, pts)
    return pts


def _bridge_tiles(ept_url: str, bounds: tuple, min_depth: int = 6) -> tuple:
    """Resolve a bridge's 4326 `bounds` to (base, tile keys, query bbox in 3857).

    `min_depth` skips coarse octree tiles (depth < min_depth) that contain
    almost no points inside a tiny bridge bbox, saving several tile downloads.
    """
    base = ept_url.rstrip("/").replace("/ept.json", "").rstrip("/")
    manifest, hierarchy = _ept_meta(base)
    ept_bounds = manifest["bounds"]  # [xmin,ymin,zmin,xmax,ymax,zmax] in EPSG:3857

    tr = _transformer("EPSG:4326", "EPSG:3857")
    qxmin, qymin = tr.transform(bounds[0], bounds[1])
    qxmax, qymax = tr.transform(bounds[2], bounds[3])
    query = (qxmin, qymin, qxmax, qymax)
    return base, tuple(_intersecting_tiles(hierarchy, ept_bounds, query, min_depth)), query


def _fetch_ept_points(
    base: str,
    tiles: tuple,
    query: tuple,
    out_crs: str,
    pool: ThreadPoolExecutor,
) -> Optional[np.ndarray]:
    """
    Fetch last-return LiDAR points from EPT within `query` (EPSG:3857 bbox).
    Returns (N,4) [x,y,z,cls] reprojected to out_crs, or None.
    """
    if not tiles:
        return None

    qxmin, qymin, qxmax, qymax = query
    all_pts = []
    for arr in pool.map(lambda t: _tile_points(base, t), tiles):
        if arr is None:
            continue
        inbox = (
            (arr[:, 0] >= qxmin)
            & (arr[:, 0] <= qxmax)
            & (arr[:, 1] >= qymin)
            & (arr[:, 1] <= qymax)
        )
        if inbox.any():
            all_pts.append(arr[inbox])

    if not all_pts:
        return None

    pts_3857 = np.vstack(all_pts)
    tr_out = _transformer("EPSG:3857", out_crs)
    ox, oy = tr_out.transform(pts_3857[:, 0], pts_3857[:, 1])
    return np.column_stack([ox, oy, pts_3857[:, 2], pts_3857[:, 3]])


def _intersecting_tiles(
    hierarchy: dict, ept_bounds: list, query: tuple, min_depth: int = 0
) -> list:
    """Return EPT tile keys whose spatial extent intersects query bbox, depth >= min_depth."""
    qxmin, qymin, qxmax, qymax = query
    results = []

    def _recurse(key, bx0, by0, bz0, bx1, by1, bz1):
        if bx1 < qxmin or bx0 > qxmax or by1 < qymin or by0 > qymax:
            return
        if hierarchy.get(key, 0) == 0:
            return
        depth = int(key.split("-")[0])
        if depth >= min_depth:
            results.append(key)
        d, x, y, z = (int(v) for v in key.split("-"))
        mx, my, mz = (bx0 + bx1) / 2, (by0 + by1) / 2, (bz0 + bz1) / 2
        for dx in range(2):
            for dy in range(2):
                for dz in range(2):
                    ck = f"{d + 1}-{x * 2 + dx}-{y * 2 + dy}-{z * 2 + dz}"
                    if ck in hierarchy:
                        _recurse(
                            ck,
                            bx0 if dx == 0 else mx,
                            by0 if dy == 0 else my,
                            bz0 if dz == 0 else mz,
                            mx if dx == 0 else bx1,
                            my if dy == 0 else by1,
                            mz if dz == 0 else bz1,
                        )

    _recurse("0-0-0-0", *ept_bounds)
    return results


# Main class
@dataclass
class generateBridgeRaster:
    """
    For each bridge line in `bridge_gpkg`, streams LiDAR points from USGS EPT,
    filters last-return bridge-deck points, denoises, and IDW-rasterizes using
    WhiteboxTools into per-bridge elevation .tif files.

    Parameters
    ----------
    bridge_gpkg  : path to any bridge lines GeoPackage (OSM or custom)
    out_dir      : root output directory
    resolution   : output raster pixel size in metres (default 10 m)
    buffer_m     : half-width buffer around bridge centerline for LiDAR query (default 10 m).
                   Set to ~half the bridge deck width — 10 m covers most 2-lane road bridges.
    id_col       : unique ID column. Auto-detects 'osmid' if present; falls back to
                   user-supplied value; uses row index if not found.
    skip_ids     : ID values to skip
    n_workers    : parallel worker threads for bridge-level processing (default: all CPUs)
    tile_workers : threads per bridge for EPT tile downloads (default 8). Backed by one
                   shared pool of n_workers * tile_workers threads, so connections and
                   downloaded tiles are reused across bridges.
    min_tile_depth: skip EPT octree tiles shallower than this depth (default 6).
                   Coarse tiles cover huge areas; almost zero bridge points fall in them.
    bridge_cls_threshold: fraction of points that must be class 13/17 to use only those;
                   if below threshold, uses ALL last-return points in bbox (handles surveys
                   that don't classify bridge decks).
    skip_existing: if True (default), skip bridges whose output .tif already exists so
                   re-runs only process new bridges instead of re-downloading everything.
    tile_cache_mb: memory budget for the decoded-tile cache (default 1024 MB). Neighbouring
                   bridges share octree tiles; cached tiles are ~4x fewer downloads.
    """

    bridge_gpkg: Union[str, Path]
    out_dir: Optional[Union[str, Path]] = None
    resolution: float = 10.0
    buffer_m: float = 10.0
    n_workers: int = field(default_factory=lambda: os.cpu_count() or 4)
    tile_workers: int = 8
    min_tile_depth: int = 6
    bridge_cls_threshold: float = 0.05
    skip_existing: bool = True
    tile_cache_mb: float = 1024.0
    id_col: Optional[str] = None
    skip_ids: list = field(default_factory=lambda: ["229091666"])

    def __post_init__(self):
        from ...logging_utils import default_output_dir

        self.bridge_gpkg = Path(self.bridge_gpkg)
        # log lives in the user-supplied root (falls back to <cwd>/out)
        self._log_dir = Path(self.out_dir) if self.out_dir else default_output_dir()
        self.out_dir = self._log_dir / "bridge_dem"
        self._tif_dir = self.out_dir / "lidar_osm_rasters"
        self._tif_dir.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict:
        """Compare GeoPackage bridge IDs against existing rasters and print a summary.

        Returns a dict with keys: total, done, pending, done_ids, pending_ids.
        Call this before run() to see what will be skipped vs processed.
        """
        bridges = self._load_bridges()
        existing = (
            {f.stem for f in self._tif_dir.glob("*.tif")}
            if self._tif_dir.exists()
            else set()
        )
        all_ids = bridges["_bridge_id"].tolist()
        done_ids = [b for b in all_ids if b in existing]
        pending_ids = [b for b in all_ids if b not in existing]

        log.info(f"Bridge raster status: {self._tif_dir}")
        log.info(f"  total   : {len(all_ids)}")
        log.info(f"  done    : {len(done_ids)} (will be skipped on re-run)")
        log.info(f"  pending : {len(pending_ids)} (will be processed)")
        if pending_ids:
            preview = pending_ids[:5]
            more = f" +{len(pending_ids) - 5} more" if len(pending_ids) > 5 else ""
            log.info(f"  pending IDs: {preview}{more}")
        return {
            "total": len(all_ids),
            "done": len(done_ids),
            "pending": len(pending_ids),
            "done_ids": done_ids,
            "pending_ids": pending_ids,
        }

    def run(self) -> Path:
        from ...logging_utils import attach_case_log

        attach_case_log(self._log_dir)
        bridges = self._load_bridges()
        footprints = self._make_footprints(bridges)
        index = self._load_entwine_index()
        footprints = self._assign_lidar_urls(footprints, index)
        log.info(
            f"Processing {len(footprints)} bridges: {self.n_workers} workers, "
            f"{self.tile_workers} tile-threads, min_tile_depth={self.min_tile_depth}, "
            f"skip_existing={self.skip_existing}"
        )
        self._process_parallel(footprints)
        n_out = len(list(self._tif_dir.glob("*.tif")))
        log.info(f"Bridge rasters complete: {n_out} files --> {self._tif_dir.name}")
        return self._tif_dir

    def _load_bridges(self) -> gpd.GeoDataFrame:
        self.bridge_gpkg = resolve_bridge_gpkg(self.bridge_gpkg)
        gdf = gpd.read_file(self.bridge_gpkg)
        if "osmid" in gdf.columns:
            col = "osmid"
        elif self.id_col and self.id_col in gdf.columns:
            col = self.id_col
        else:
            if self.id_col:
                log.warning(
                    f"id_col='{self.id_col}' not found; using row index as bridge ID"
                )
            gdf["_bridge_id"] = [f"bridge_{i}" for i in range(len(gdf))]
            return gdf
        gdf["_bridge_id"] = gdf[col].astype(str)
        gdf = gdf[~gdf["_bridge_id"].isin([str(s) for s in self.skip_ids])].reset_index(
            drop=True
        )
        return gdf

    def _make_footprints(self, bridges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        fp = bridges.copy()
        if fp.crs is None:
            fp = fp.set_crs("EPSG:4326")
        proj_crs = fp.estimate_utm_crs()
        fp_proj = fp.to_crs(proj_crs)
        fp_proj["geometry"] = fp_proj.geometry.buffer(self.buffer_m)
        fp = fp_proj.to_crs("EPSG:4326")
        if "name" in fp.columns:
            fp = fp.rename(columns={"name": "bridge_name"})
        return fp

    def _load_entwine_index(self) -> gpd.GeoDataFrame:
        log.info("Loading USGS LiDAR tile index...")
        # Download to a local temp file first, then read. Reading the raw
        # GitHub URL directly makes GDAL/pyogrio open it through /vsicurl,
        # which fails on some builds (the URL redirects and serves the
        # .topojson without a Content-Length, so the range reader bails with
        # "does not exist in the file system"). A plain requests download
        # follows the redirect and sidesteps /vsicurl entirely.
        resp = _session().get(_ENTWINE_INDEX_URL, timeout=60)
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".topojson", delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name
        try:
            idx = gpd.read_file(tmp_path)
        finally:
            os.unlink(tmp_path)
        return idx.set_crs("EPSG:4326", allow_override=True)

    def _assign_lidar_urls(self, footprints, index) -> gpd.GeoDataFrame:
        joined = gpd.overlay(
            footprints, index[["url", "count", "geometry"]], how="intersection"
        )
        if joined.empty:
            raise RuntimeError("No LiDAR tiles intersect the bridge footprints.")
        joined = joined.loc[joined.groupby("_bridge_id")["count"].idxmax()].reset_index(
            drop=True
        )
        return joined

    def _plan(self, footprints: gpd.GeoDataFrame) -> list:
        """Resolve every bridge to its EPT tiles, then order them for tile reuse.

        Doing the octree lookup up front costs nothing extra (the hierarchy is
        already cached per survey) and lets bridges be visited in tile order, so
        a shared tile stays hot in cache while the bridges that need it go by
        instead of being re-downloaded after it has been evicted.
        """
        jobs = []
        for _, row in footprints.iterrows():
            base, tiles, query = _bridge_tiles(
                row["url"], row.geometry.bounds, self.min_tile_depth
            )
            jobs.append(
                {
                    "bridge_id": row["_bridge_id"],
                    "base": base,
                    "tiles": tiles,
                    "query": query,
                }
            )
        jobs.sort(key=lambda j: (j["base"], j["tiles"]))
        requests_n = sum(len(j["tiles"]) for j in jobs)
        unique_n = len({(j["base"], t) for j in jobs for t in j["tiles"]})
        log.info(
            f"Tile plan: {requests_n} tile lookups over {unique_n} unique tiles "
            f"({requests_n / max(1, unique_n):.1f}x reuse), cache {self.tile_cache_mb:.0f} MB"
        )
        return jobs

    def _process_parallel(self, footprints: gpd.GeoDataFrame):
        global _TILE_CACHE_MAX_BYTES, _TILE_REQUESTS, _TILE_DOWNLOADS
        _TILE_CACHE_MAX_BYTES = int(self.tile_cache_mb * 1024 * 1024)
        # Counters are per-run; the cache itself is kept, so a second AOI in the
        # same process still gets to reuse any tiles it shares with the first.
        with _TILE_CACHE_LOCK:
            _TILE_REQUESTS = _TILE_DOWNLOADS = 0

        tif_dir = str(self._tif_dir)
        skipped = 0
        if self.skip_existing:
            # Filter here as well as in the worker: a re-run shouldn't pay for the
            # tile lookup, or make the progress bar count work it never does.
            done = {f.stem for f in self._tif_dir.glob("*.tif")}
            n_before = len(footprints)
            footprints = footprints[~footprints["_bridge_id"].isin(done)]
            skipped = n_before - len(footprints)
            if skipped:
                log.info(f"{skipped} bridges already have rasters — skipping")

        jobs = self._plan(footprints)
        n = len(jobs)
        ok = failed = empty = 0

        # One download pool for the whole run, not one per bridge: creating ~3k
        # short-lived pools meant ~3k cold TLS connections to S3.
        pool = ThreadPoolExecutor(
            max_workers=max(1, min(32, self.n_workers * self.tile_workers)),
            thread_name_prefix="ept-tile",
        )
        try:
            with ThreadPoolExecutor(max_workers=self.n_workers) as executor:
                fmap = {
                    executor.submit(
                        _process_one_bridge,
                        bridge_id=job["bridge_id"],
                        base=job["base"],
                        tiles=job["tiles"],
                        query=job["query"],
                        tif_dir=tif_dir,
                        resolution=self.resolution,
                        pool=pool,
                        bridge_cls_threshold=self.bridge_cls_threshold,
                        skip_existing=self.skip_existing,
                    ): job["bridge_id"]
                    for job in jobs
                }
                with tqdm(
                    total=n, desc="Bridges", unit="bridge", dynamic_ncols=True
                ) as pbar:
                    for future in as_completed(fmap):
                        bid = fmap[future]
                        try:
                            result = future.result()
                            if result == "skipped":
                                skipped += 1
                            elif result == "empty":
                                empty += 1
                            else:
                                ok += 1
                        except Exception as exc:
                            log.warning(f"bridge {bid} failed: {exc}")
                            failed += 1
                        pbar.update(1)
                        pbar.set_postfix(
                            ok=ok, skip=skipped, none=empty, fail=failed, refresh=False
                        )
        finally:
            pool.shutdown(wait=True)

        log.info(
            f"Completed — {ok} processed, {skipped} skipped, {empty} without LiDAR, "
            f"{failed} failed; {_TILE_DOWNLOADS} tiles downloaded of "
            f"{_TILE_REQUESTS} requested"
        )


def _idw_rasterize(
    xy: np.ndarray,
    z: np.ndarray,
    bounds: tuple,
    resolution: float,
    weight: float = 2.0,
) -> tuple:
    """
    IDW-interpolate scattered (x, y, z) LiDAR points onto a regular grid.

    Every output pixel is always filled — no radius cutoff.  The k nearest
    points are used regardless of distance, which guarantees full coverage
    even when point density is lower than the output resolution.

    Returns (grid, transform, nodata) where grid is float32 and transform is
    an affine rasterio transform.  Pure numpy/scipy — no external binaries.
    """
    import rasterio.transform as _rt

    xmin, ymin, xmax, ymax = bounds
    cols = max(1, int(np.ceil((xmax - xmin) / resolution)))
    rows = max(1, int(np.ceil((ymax - ymin) / resolution)))

    # pixel centres
    cx = xmin + (np.arange(cols) + 0.5) * resolution
    cy = ymax - (np.arange(rows) + 0.5) * resolution
    gx, gy = np.meshgrid(cx, cy)
    grid_pts = np.column_stack([gx.ravel(), gy.ravel()])

    # Use up to 12 neighbours — no distance_upper_bound so every pixel always gets a value
    k = min(12, len(xy))
    tree = KDTree(xy)
    dists, idxs = tree.query(grid_pts, k=k)
    # When k=1 scipy returns 1-D arrays; normalise to (n_pixels, k) for uniform logic
    if k == 1:
        dists = dists[:, np.newaxis]
        idxs = idxs[:, np.newaxis]

    nodata = np.float32(-9999.0)

    z_vals = z[idxs]  # (n_pixels, k)

    # Exact hits (distance == 0): use the point elevation directly
    exact_mask = dists == 0.0  # (n_pixels, k)
    has_exact = exact_mask.any(axis=1)

    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(~exact_mask, 1.0 / np.power(dists, weight), 0.0)

    w_sum = w.sum(axis=1)
    idw_vals = np.where(w_sum > 0, (w * z_vals).sum(axis=1) / w_sum, nodata)

    first_exact_idx = np.argmax(exact_mask, axis=1)
    exact_z = z_vals[np.arange(len(grid_pts)), first_exact_idx]

    out = np.where(has_exact, exact_z, idw_vals)

    grid = out.reshape(rows, cols).astype(np.float32)
    transform = _rt.from_bounds(xmin, ymin, xmax, ymax, cols, rows)
    return grid, transform, nodata


def _process_one_bridge(
    bridge_id: str,
    base: str,
    tiles: tuple,
    query: tuple,
    tif_dir: str,
    resolution: float,
    pool: ThreadPoolExecutor,
    bridge_cls_threshold: float = 0.05,
    skip_existing: bool = True,
):
    import rasterio
    from rasterio.crs import CRS as _CRS

    out_crs = "EPSG:5070"
    tif_path = os.path.join(tif_dir, f"{bridge_id}.tif")

    if skip_existing and os.path.exists(tif_path):
        return "skipped"

    pts = _fetch_ept_points(base, tiles, query, out_crs, pool)
    if pts is None or len(pts) == 0:
        return "empty"

    xy = pts[:, :2]
    z = pts[:, 2].copy()
    cls = pts[:, 3].astype(int)

    bridge_mask = np.isin(cls, list(_BRIDGE_CLASSES))
    if bridge_mask.sum() / len(cls) >= bridge_cls_threshold:
        # survey has bridge-deck classifications — replace non-bridge z with nearest bridge z
        if (~bridge_mask).any():
            n_bridge = bridge_mask.sum()
            k = min(2, n_bridge)
            tree = KDTree(xy[bridge_mask])
            _, idx = tree.query(xy[~bridge_mask], k=k)
            if k == 1:
                idx = idx.reshape(-1, 1)
            z[~bridge_mask] = z[bridge_mask][idx].mean(axis=1)

    # else: survey doesn't classify bridge decks — use all last-return points as-is

    # Compute grid bounds from point cloud extent (not EPT query bbox)
    xmin, ymin = xy[:, 0].min(), xy[:, 1].min()
    xmax, ymax = xy[:, 0].max(), xy[:, 1].max()
    # Ensure at least one pixel
    if xmax <= xmin:
        xmax = xmin + resolution
    if ymax <= ymin:
        ymax = ymin + resolution

    grid, transform, nodata = _idw_rasterize(xy, z, (xmin, ymin, xmax, ymax), resolution)

    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        height=grid.shape[0],
        width=grid.shape[1],
        count=1,
        dtype="float32",
        crs=_CRS.from_string(out_crs),
        transform=transform,
        nodata=nodata,
        compress="lzw",
    ) as dst:
        dst.write(grid, 1)


# CLI
# Usage:
#   python bridge_lidar_raster.py \
#       --bridge_gpkg /path/to/bridges.gpkg \
#       --out_dir     /path/to/output \
#       --resolution  10.0 --buffer_m 1.5 --n_workers 8 --tile_workers 8
if __name__ == "__main__":
    import argparse

    from ...logging_utils import configure_cli_logging

    configure_cli_logging()

    p = argparse.ArgumentParser(
        description=(
            "Stream USGS 3DEP LiDAR last-return points for each bridge and "
            "IDW-rasterize into per-bridge .tif files.\n"
            "LiDAR source: https://github.com/hobuinc/usgs-lidar"
        )
    )
    p.add_argument("--bridge_gpkg", required=True)
    p.add_argument("--out_dir", required=True)
    p.add_argument("--resolution", type=float, default=10.0)
    p.add_argument("--buffer_m", type=float, default=10.0)
    p.add_argument("--n_workers", type=int, default=None)
    p.add_argument("--tile_workers", type=int, default=8)
    p.add_argument("--min_tile_depth", type=int, default=6)
    p.add_argument("--bridge_cls_threshold", type=float, default=0.05)
    p.add_argument("--tile_cache_mb", type=float, default=1024.0)
    p.add_argument("--id_col", default=None)
    p.add_argument("--skip_ids", nargs="*", default=["229091666"])
    args = p.parse_args()

    kwargs = dict(
        bridge_gpkg=args.bridge_gpkg,
        out_dir=args.out_dir,
        resolution=args.resolution,
        buffer_m=args.buffer_m,
        tile_workers=args.tile_workers,
        min_tile_depth=args.min_tile_depth,
        bridge_cls_threshold=args.bridge_cls_threshold,
        tile_cache_mb=args.tile_cache_mb,
        id_col=args.id_col,
        skip_ids=args.skip_ids,
    )
    if args.n_workers is not None:
        kwargs["n_workers"] = args.n_workers

    out = generateBridgeRaster(**kwargs).run()
    log.info(f"Per-bridge LiDAR tifs --> {out}")
