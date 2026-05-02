"""
Author: Supath Dhital
Updated = May 2026
---------------------
Downloads USGS 3DEP LiDAR points for each bridge in a GeoPackage
and IDW-interpolates them into per-bridge elevation rasters.

LiDAR source : USGS 3DEP via Entwine Point Tiles (EPT) on AWS S3
Tile index   : https://github.com/hobuinc/usgs-lidar  (boundaries.topojson)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
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

log = logging.getLogger(__name__)

# LAS classification 13=bridge deck, 17=bridge deck
_BRIDGE_CLASSES = {13, 17}
_ENTWINE_INDEX_URL = "https://raw.githubusercontent.com/hobuinc/usgs-lidar/master/boundaries/boundaries.topojson"

# Per-thread session — avoids connection pool exhaustion when many dask threads run concurrently.
import threading as _threading

_thread_local = _threading.local()


def _session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
        s.mount("https://", adapter)
        _thread_local.session = s
    return _thread_local.session


# EPT manifest + hierarchy cache: fetch once per unique EPT URL, reuse for all bridges
_EPT_CACHE: dict = {}
_EPT_CACHE_LOCK = Lock()

# Serialize WhiteboxTools binary download — prevents multiple threads racing to unzip it.
_WBT_READY = False
_WBT_LOCK = Lock()
_WBT_EXE: str = ""  # absolute path to whitebox_tools binary, set by _ensure_wbt_ready


def _ensure_wbt_ready() -> None:
    """Download the WBT binary once in the calling thread and record its absolute path.

    WBT's run_tool() calls os.chdir() which is not thread-safe — multiple threads
    clobbering the cwd causes 'No such file or directory: ./whitebox_tools'. We bypass
    run_tool entirely and invoke the binary by absolute path via subprocess.
    """
    global _WBT_READY, _WBT_EXE
    if _WBT_READY:
        return
    with _WBT_LOCK:
        if not _WBT_READY:
            import whitebox as _wbt_mod

            _inst = _wbt_mod.WhiteboxTools()
            _inst.verbose = False
            _WBT_EXE = os.path.join(_inst.exe_path, _inst.exe_name)
            _WBT_READY = True


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


def _fetch_one_tile(args) -> Optional[np.ndarray]:
    """Download + decode one EPT .laz tile. Returns (N,4) [x,y,z,cls] in EPSG:3857 or None."""
    import laspy

    tile_key, base, qxmin, qymin, qxmax, qymax = args
    url = f"{base}/ept-data/{tile_key}.laz"
    resp = _session().get(url, timeout=60)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".laz", delete=False) as f:
        f.write(resp.content)
        tmp = f.name
    try:
        las = laspy.read(tmp)
        x = np.array(las.x)
        y = np.array(las.y)
        z = np.array(las.z)
        cls = np.array(las.classification)
        ret = np.array(las.return_number)
        nr = np.array(las.number_of_returns)
        last = ret == nr
        inbox = (x >= qxmin) & (x <= qxmax) & (y >= qymin) & (y <= qymax)
        mask = last & inbox
        if not mask.any():
            return None
        return np.column_stack([x[mask], y[mask], z[mask], cls[mask].astype(float)])
    finally:
        os.unlink(tmp)


def _fetch_ept_points(
    ept_url: str,
    bounds: tuple,
    out_crs: str,
    tile_workers: int = 8,
    min_depth: int = 6,
) -> Optional[np.ndarray]:
    """
    Fetch last-return LiDAR points from EPT within `bounds` (EPSG:4326).
    Returns (N,4) [x,y,z,cls] reprojected to out_crs, or None.

    `min_depth` skips coarse octree tiles (depth < min_depth) that contain
    almost no points inside a tiny bridge bbox, saving several tile downloads.
    """
    from pyproj import Transformer

    base = ept_url.rstrip("/").replace("/ept.json", "").rstrip("/")
    manifest, hierarchy = _ept_meta(base)
    ept_bounds = manifest["bounds"]  # [xmin,ymin,zmin,xmax,ymax,zmax] in EPSG:3857

    tr = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    qxmin, qymin = tr.transform(bounds[0], bounds[1])
    qxmax, qymax = tr.transform(bounds[2], bounds[3])

    tiles = _intersecting_tiles(
        hierarchy, ept_bounds, (qxmin, qymin, qxmax, qymax), min_depth
    )
    if not tiles:
        return None

    args = [(t, base, qxmin, qymin, qxmax, qymax) for t in tiles]
    all_pts = []
    with ThreadPoolExecutor(max_workers=min(tile_workers, len(tiles))) as pool:
        for arr in pool.map(_fetch_one_tile, args):
            if arr is not None:
                all_pts.append(arr)

    if not all_pts:
        return None

    pts_3857 = np.vstack(all_pts)
    tr_out = Transformer.from_crs("EPSG:3857", out_crs, always_xy=True)
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
                    ck = f"{d+1}-{x*2+dx}-{y*2+dy}-{z*2+dz}"
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
    tile_workers : threads for per-bridge EPT tile downloads (default 8)
    min_tile_depth: skip EPT octree tiles shallower than this depth (default 6).
                   Coarse tiles cover huge areas; almost zero bridge points fall in them.
    bridge_cls_threshold: fraction of points that must be class 13/17 to use only those;
                   if below threshold, uses ALL last-return points in bbox (handles surveys
                   that don't classify bridge decks).
    skip_existing: if True (default), skip bridges whose output .tif already exists so
                   re-runs only process new bridges instead of re-downloading everything.
    """

    bridge_gpkg: Union[str, Path]
    out_dir: Union[str, Path] = Path("bridge_dem_output")
    resolution: float = 10.0
    buffer_m: float = 10.0
    n_workers: int = field(default_factory=lambda: os.cpu_count() or 4)
    tile_workers: int = 8
    min_tile_depth: int = 6
    bridge_cls_threshold: float = 0.05
    skip_existing: bool = True
    id_col: Optional[str] = None
    skip_ids: list = field(default_factory=lambda: ["229091666"])

    def __post_init__(self):
        self.bridge_gpkg = Path(self.bridge_gpkg)
        self._log_dir = Path(self.out_dir)  # log lives in the user-supplied root
        self.out_dir = self._log_dir / "bridge_dem"
        self._point_dir = self.out_dir / "point_files"
        self._tif_dir = self.out_dir / "lidar_osm_rasters"
        self._point_dir.mkdir(parents=True, exist_ok=True)
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

        print(f"\nBridge raster status: {self._tif_dir}")
        print(f"  Total   : {len(all_ids)}")
        print(f"  Done    : {len(done_ids)}  (will be skipped on re-run)")
        print(f"  Pending : {len(pending_ids)}  (will be processed)")
        if pending_ids:
            preview = pending_ids[:5]
            more = f" … +{len(pending_ids)-5} more" if len(pending_ids) > 5 else ""
            print(f"  Pending IDs: {preview}{more}")
        print()
        return {
            "total": len(all_ids),
            "done": len(done_ids),
            "pending": len(pending_ids),
            "done_ids": done_ids,
            "pending_ids": pending_ids,
        }

    def run(self) -> Path:
        log_path = self._log_dir / "preprocess.log"
        fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
        )
        log.addHandler(fh)
        try:
            bridges = self._load_bridges()
            footprints = self._make_footprints(bridges)
            index = self._load_entwine_index()
            footprints = self._assign_lidar_urls(footprints, index)
            log.info(
                f"Processing {len(footprints)} bridges — {self.n_workers} workers, "
                f"{self.tile_workers} tile-threads, min_tile_depth={self.min_tile_depth}, "
                f"skip_existing={self.skip_existing}"
            )
            self._process_parallel(footprints)
            shutil.rmtree(self._point_dir, ignore_errors=True)
            log.info(f"Removed temporary point_files: {self._point_dir}")
            n_out = len(list(self._tif_dir.glob("*.tif")))
            log.info(f"Done — {n_out} rasters written to {self._tif_dir}")
        finally:
            log.removeHandler(fh)
            fh.close()
        return self._tif_dir

    def _load_bridges(self) -> gpd.GeoDataFrame:
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
        idx = gpd.read_file(_ENTWINE_INDEX_URL)
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

    def _process_parallel(self, footprints: gpd.GeoDataFrame):
        _ensure_wbt_ready()

        point_dir = str(self._point_dir)
        tif_dir = str(self._tif_dir)
        n = len(footprints)
        ok = failed = skipped = 0

        with ThreadPoolExecutor(max_workers=self.n_workers) as executor:
            fmap = {
                executor.submit(
                    _process_one_bridge,
                    bridge_id=row["_bridge_id"],
                    bounds=row.geometry.bounds,
                    lidar_url=row["url"],
                    point_dir=point_dir,
                    tif_dir=tif_dir,
                    resolution=self.resolution,
                    tile_workers=self.tile_workers,
                    min_tile_depth=self.min_tile_depth,
                    bridge_cls_threshold=self.bridge_cls_threshold,
                    skip_existing=self.skip_existing,
                ): row["_bridge_id"]
                for _, row in footprints.iterrows()
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
                        else:
                            ok += 1
                    except Exception as exc:
                        log.warning(f"bridge {bid} failed: {exc}")
                        failed += 1
                    pbar.update(1)
                    pbar.set_postfix(ok=ok, skip=skipped, fail=failed, refresh=False)

        log.info(f"Completed — {ok} processed, {skipped} skipped, {failed} failed")


def _process_one_bridge(
    bridge_id: str,
    bounds: tuple,
    lidar_url: str,
    point_dir: str,
    tif_dir: str,
    resolution: float,
    tile_workers: int = 8,
    min_tile_depth: int = 6,
    bridge_cls_threshold: float = 0.05,
    skip_existing: bool = True,
):
    import laspy

    out_crs = "EPSG:5070"
    las_path = os.path.join(point_dir, f"{bridge_id}.las")
    tif_path = os.path.join(tif_dir, f"{bridge_id}.tif")

    if skip_existing and os.path.exists(tif_path):
        return "skipped"

    try:
        pts = _fetch_ept_points(
            lidar_url,
            bounds,
            out_crs,
            tile_workers=tile_workers,
            min_depth=min_tile_depth,
        )
        if pts is None or len(pts) == 0:
            return

        xy = pts[:, :2]
        z = pts[:, 2].copy()
        cls = pts[:, 3].astype(int)

        bridge_mask = np.isin(cls, list(_BRIDGE_CLASSES))
        if bridge_mask.sum() / len(cls) >= bridge_cls_threshold:
            # survey has bridge-deck classifications — denoise non-bridge points
            if (~bridge_mask).any():
                n_bridge = bridge_mask.sum()
                k = min(2, n_bridge)
                tree = KDTree(xy[bridge_mask])
                _, idx = tree.query(xy[~bridge_mask], k=k)
                # k=1 → idx shape (N,); k=2 → (N,2). Normalise to (N,k) for mean(axis=1)
                if k == 1:
                    idx = idx.reshape(-1, 1)
                z[~bridge_mask] = z[bridge_mask][idx].mean(axis=1)

        # else: survey doesn't classify bridge decks (class 1/2 only) — use all last-return points
        hdr = laspy.LasHeader(version="1.2", point_format=0)
        hdr.offsets = np.array([xy[:, 0].min(), xy[:, 1].min(), z.min()])
        hdr.scales = np.array([0.001, 0.001, 0.001])
        las_out = laspy.LasData(header=hdr)
        las_out.x = xy[:, 0]
        las_out.y = xy[:, 1]
        las_out.z = z
        las_out.write(las_path)

        # Run WBT by absolute path so os.chdir() inside run_tool can't corrupt other
        # threads' cwd. Use returns=all because we already filtered last-returns in
        # Python; passing returns=last on a LAS with unset return-number fields gives
        # WBT 0 points and causes it to hang indefinitely on readline().
        proc = subprocess.Popen(
            [
                _WBT_EXE,
                "--run=LidarIdwInterpolation",
                f"--i={las_path}",
                f"--output={tif_path}",
                "--parameter=elevation",
                "--returns=all",
                f"--resolution={resolution}",
                "--weight=2.0",
                "--radius=5.0",
                "-v=false",
                "--compress_rasters=False",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(_WBT_EXE),
        )
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            log.warning(f"bridge {bridge_id}: WBT IDW timed out after 30s, skipping")
            return

        # WBT IDW doesn't embed CRS — stamp it explicitly.
        if os.path.exists(tif_path):
            import rasterio
            from rasterio.crs import CRS as _CRS

            with rasterio.open(tif_path, "r+") as _dst:
                _dst.crs = _CRS.from_string(out_crs)
    except Exception as exc:
        log.warning(f"bridge {bridge_id} failed: {exc}")
    finally:
        if os.path.exists(las_path):
            os.remove(las_path)


# CLI
# Usage:
#   python bridge_lidar_raster.py \
#       --bridge_gpkg /path/to/bridges.gpkg \
#       --out_dir     /path/to/output \
#       --resolution  10.0 --buffer_m 1.5 --n_workers 8 --tile_workers 8
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

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
        id_col=args.id_col,
        skip_ids=args.skip_ids,
    )
    if args.n_workers is not None:
        kwargs["n_workers"] = args.n_workers

    print(f"Per-bridge LiDAR tifs saved to: {generateBridgeRaster(**kwargs).run()}")
