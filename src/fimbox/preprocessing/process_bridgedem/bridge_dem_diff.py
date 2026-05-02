"""
Author: Supath Dhital
Code Updated = May 2026
-------------
Computes per-pixel (lidar_elev - dem_elev) difference rasters from
per-bridge LiDAR tifs and a base DEM,
then mosaics all into a single bridge_elev_diff.tif covering the full area.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import rowcol
from shapely.geometry import Point
from tqdm import tqdm

log = logging.getLogger(__name__)


@dataclass
class BridgeDEMDiff:
    """
    Computes bridge elevation-difference raster from per-bridge LiDAR tifs and a DEM.

    Parameters
    ----------
    dem_path        : path to the base DEM .tif (3DEP or project DEM)
    lidar_tif_dir   : directory containing per-bridge .tif files (output of generateBridgeRaster)
    bridge_gpkg     : bridge lines GeoPackage (OSM or custom)
    out_dir         : output directory; saves bridge_elev_diff.tif here
    out_name        : output filename (default bridge_elev_diff.tif)
    id_col          : column used as unique bridge ID to match tif filenames.
                      Auto-detects 'osmid'; falls back to row index if missing.
    n_workers       : parallel worker threads (default: min(cpu_count, 8))
    """

    dem_path: Union[str, Path]
    lidar_tif_dir: Union[str, Path]
    bridge_gpkg: Union[str, Path]
    out_dir: Union[str, Path] = Path("bridge_dem_output")
    out_name: str = "bridge_elev_diff.tif"
    n_workers: int = field(default_factory=lambda: min(os.cpu_count() or 4, 8))
    id_col: Optional[str] = "osmid"

    def __post_init__(self):
        self.dem_path = Path(self.dem_path)
        self.lidar_tif_dir = Path(self.lidar_tif_dir)
        self.bridge_gpkg = Path(self.bridge_gpkg)
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> Path:
        log_path = self.out_dir / "preprocess.log"
        fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
        )
        log.addHandler(fh)
        try:
            return self._run()
        finally:
            log.removeHandler(fh)
            fh.close()

    def _run(self) -> Path:
        bridges = self._load_bridges()
        tif_files = list(self.lidar_tif_dir.glob("*.tif"))
        if not tif_files:
            raise FileNotFoundError(f"No LiDAR tifs found in {self.lidar_tif_dir}")

        tif_map = {f.stem: f for f in tif_files}
        bridges["has_lidar"] = bridges["_bridge_id"].isin(tif_map)
        n_with = bridges["has_lidar"].sum()
        log.info(f"{n_with}/{len(bridges)} bridges have LiDAR tifs")

        with rasterio.open(self.dem_path) as dem_src:
            dem_meta = dem_src.meta.copy()
            dem_transform = dem_src.transform
            dem_shape = (dem_src.height, dem_src.width)
            dem_nodata = dem_src.nodata
            dem_crs = dem_src.crs

        bridges_dem_crs = bridges.to_crs(dem_crs)

        # One shared accumulator — float32, same footprint as the DEM.
        # Workers return sparse (rows, cols, vals) so only one full array lives in memory.
        diff_array = np.zeros(dem_shape, dtype=np.float32)

        with ThreadPoolExecutor(max_workers=self.n_workers) as executor:
            fmap = {
                executor.submit(
                    _compute_bridge_diff,
                    bridge_id=row["_bridge_id"],
                    tif_path=(
                        str(tif_map[row["_bridge_id"]]) if row["has_lidar"] else None
                    ),
                    dem_path=str(self.dem_path),
                    dem_transform=dem_transform,
                    dem_shape=dem_shape,
                    dem_nodata=dem_nodata,
                    dem_crs=str(dem_crs),
                ): row["_bridge_id"]
                for _, row in bridges_dem_crs.iterrows()
            }

            ok = failed = 0
            with tqdm(
                total=len(fmap), desc="Bridge diffs", unit="bridge", dynamic_ncols=True
            ) as pbar:
                for future in as_completed(fmap):
                    bid = fmap[future]
                    try:
                        result = future.result()
                        if result is not None:
                            rows, cols, vals = result
                            diff_array[rows, cols] = vals.astype(np.float32)
                        ok += 1
                    except Exception as exc:
                        log.warning(f"bridge {bid}: diff failed — {exc}")
                        failed += 1
                    pbar.update(1)
                    pbar.set_postfix(ok=ok, fail=failed, refresh=False)

        log.info(f"Diff accumulation done — {ok} ok, {failed} failed")

        # Apply DEM nodata mask using uint8 dataset_mask (4× less RAM than read(1)).
        if dem_nodata is not None:
            with rasterio.open(self.dem_path) as dem_src:
                mask = dem_src.dataset_mask()  # 255=valid, 0=nodata
            diff_array[mask == 0] = dem_nodata
            del mask

        out_path = self.out_dir / self.out_name
        dem_meta.update({"dtype": "float32", "compress": "lzw", "count": 1})
        with rasterio.open(out_path, "w", **dem_meta) as dst:
            dst.write(diff_array, 1)

        log.info(f"Saved bridge_elev_diff raster → {out_path}")
        return out_path

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
        return gdf


# Module-level worker — sparse return keeps memory flat regardless of DEM size.
def _compute_bridge_diff(
    bridge_id: str,
    tif_path: Optional[str],
    dem_path: str,
    dem_transform,
    dem_shape: tuple,
    dem_nodata,
    dem_crs: str,
) -> Optional[tuple]:
    """
    Returns (rows, cols, vals) sparse arrays for this bridge's elevation diff,
    or None if no valid data. Sparse return means the caller never holds more than
    one full DEM array in memory regardless of how many bridges run in parallel.
    """
    try:
        if tif_path is None or not os.path.exists(tif_path):
            return None

        points, lidar_vals = _tif_to_points(tif_path, dem_crs)
        if not points:
            return None

        pts_gdf = gpd.GeoDataFrame(
            {"lidar_elev": lidar_vals}, geometry=points, crs=dem_crs
        )

        # Sample base DEM elevation at each LiDAR point
        coords = [(g.x, g.y) for g in pts_gdf.geometry]
        with rasterio.open(dem_path) as src:
            sampled = np.array([v[0] for v in src.sample(coords)], dtype=np.float32)

        # Drop points where the DEM has nodata
        if dem_nodata is not None:
            valid_dem = sampled != dem_nodata
            if not valid_dem.any():
                return None
            pts_gdf = pts_gdf.iloc[valid_dem]
            sampled = sampled[valid_dem]
            lidar_vals = np.array(lidar_vals)[valid_dem]

        elev_diff = np.array(lidar_vals, dtype=np.float32) - sampled

        # Convert point coordinates to DEM pixel indices
        xs = np.array([g.x for g in pts_gdf.geometry])
        ys = np.array([g.y for g in pts_gdf.geometry])
        rows, cols = rowcol(dem_transform, xs, ys)
        rows = np.array(rows)
        cols = np.array(cols)

        # Keep only pixels that fall inside the DEM extent
        in_bounds = (
            (rows >= 0) & (rows < dem_shape[0]) & (cols >= 0) & (cols < dem_shape[1])
        )
        if not in_bounds.any():
            return None

        return rows[in_bounds], cols[in_bounds], elev_diff[in_bounds]

    except Exception as exc:
        log.warning(f"bridge {bridge_id}: diff computation failed — {exc}")
        return None


def _tif_to_points(tif_path: str, target_crs: str):
    """Read a per-bridge raster and return (list[Point], list[float]) for valid pixels."""
    with rasterio.open(tif_path) as src:
        data = src.read(1)
        nodata = src.nodata
        transform = src.transform
        src_crs = src.crs

        valid = data != nodata if nodata is not None else np.ones(data.shape, bool)
        rows, cols = np.where(valid)
        if len(rows) == 0:
            return [], []
        xs, ys = rasterio.transform.xy(transform, rows, cols, offset="center")
        vals = data[rows, cols].tolist()

    points = [Point(x, y) for x, y in zip(xs, ys)]

    if src_crs and str(src_crs) != str(target_crs):
        tmp = gpd.GeoDataFrame(geometry=points, crs=src_crs).to_crs(target_crs)
        points = list(tmp.geometry)

    return points, vals


# CLI
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    p = argparse.ArgumentParser(
        description=(
            "Compute bridge elevation-difference raster (lidar_elev - dem_elev) "
            "from per-bridge LiDAR tifs and a base DEM. "
            "Saves bridge_elev_diff.tif aligned to the input DEM grid."
        )
    )
    p.add_argument("--dem_path", required=True)
    p.add_argument("--lidar_tif_dir", required=True)
    p.add_argument("--bridge_gpkg", required=True)
    p.add_argument("--id_col", default="osmid")
    p.add_argument("--out_dir", required=True)
    p.add_argument("--out_name", default="bridge_elev_diff.tif")
    p.add_argument("--n_workers", type=int, default=None)
    args = p.parse_args()

    kwargs = dict(
        dem_path=args.dem_path,
        lidar_tif_dir=args.lidar_tif_dir,
        bridge_gpkg=args.bridge_gpkg,
        out_dir=args.out_dir,
        out_name=args.out_name,
        id_col=args.id_col,
    )
    if args.n_workers is not None:
        kwargs["n_workers"] = args.n_workers

    out_path = BridgeDEMDiff(**kwargs).run()
    print(f"Bridge diff raster saved to: {out_path}")
