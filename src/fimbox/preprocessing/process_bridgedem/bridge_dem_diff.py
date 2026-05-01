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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import dask
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.merge import merge
from rasterio.transform import from_bounds
from shapely.geometry import Point, box

log = logging.getLogger(__name__)


@dataclass
class BridgeDEMDiff:
    """
    Computes bridge elevation-difference raster from per-bridge LiDAR tifs and a DEM.

    Parameters
    ----------
    dem_path        : path to the base DEM .tif (3DEP or project DEM)
    lidar_tif_dir   : directory containing per-bridge .tif files (output of BridgeLiDARRasterizer)
    bridge_gpkg     : bridge lines GeoPackage (OSM or custom)
    out_dir         : output directory; saves bridge_elev_diff.tif here
    out_name        : output filename (default bridge_elev_diff.tif)
    bridge_buffer_m : buffer around bridge lines to clip LiDAR points (default 2 m)
    id_col          : column used as unique bridge ID to match tif filenames.
                      If None or column missing, row indices are used automatically.
    """

    dem_path: Union[str, Path]
    lidar_tif_dir: Union[str, Path]
    bridge_gpkg: Union[str, Path]
    out_dir: Union[str, Path] = Path("bridge_dem_output")
    out_name: str = "bridge_elev_diff.tif"
    bridge_buffer_m: float = 2.0
    n_workers: int = field(default_factory=lambda: min(os.cpu_count() or 4, 8))
    id_col: Optional[str] = "osmid"

    def __post_init__(self):
        self.dem_path = Path(self.dem_path)
        self.lidar_tif_dir = Path(self.lidar_tif_dir)
        self.bridge_gpkg = Path(self.bridge_gpkg)
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # API
    def run(self) -> Path:
        bridges = self._load_bridges()
        tif_files = list(self.lidar_tif_dir.glob("*.tif"))
        if not tif_files:
            raise FileNotFoundError(f"No LiDAR tifs found in {self.lidar_tif_dir}")

        tif_map = {f.stem: f for f in tif_files}
        bridges["has_lidar"] = bridges["_bridge_id"].isin(tif_map)
        log.info(f"{bridges['has_lidar'].sum()}/{len(bridges)} bridges have LiDAR tifs")

        with rasterio.open(self.dem_path) as dem_src:
            dem_meta = dem_src.meta.copy()
            dem_transform = dem_src.transform
            dem_shape = (dem_src.height, dem_src.width)
            dem_nodata = dem_src.nodata
            dem_crs = dem_src.crs

        # Reproject bridges to DEM CRS for sampling
        bridges_dem_crs = bridges.to_crs(dem_crs)
        bridges_dem_crs["geometry_buffered"] = bridges_dem_crs.geometry.buffer(self.bridge_buffer_m)

        # Build per-bridge diff arrays in parallel with dask
        delayed_tasks = [
            dask.delayed(_compute_bridge_diff)(
                bridge_id=row["_bridge_id"],
                tif_path=str(tif_map[row["_bridge_id"]]) if row["has_lidar"] else None,
                bridge_line=row["geometry_buffered"],
                dem_path=str(self.dem_path),
                dem_shape=dem_shape,
                dem_transform=dem_transform,
                dem_nodata=dem_nodata,
                dem_crs=str(dem_crs),
            )
            for _, row in bridges_dem_crs.iterrows()
        ]

        log.info(f"Computing diffs for {len(delayed_tasks)} bridges ({self.n_workers} threads)...")
        results = dask.compute(*delayed_tasks, scheduler="threads", num_workers=self.n_workers)

        # Accumulate: sum all per-bridge diff arrays (fill=0 outside bridges)
        diff_array = np.zeros(dem_shape, dtype=np.float32)
        for arr in results:
            if arr is not None:
                diff_array += arr

        # Apply DEM nodata mask
        with rasterio.open(self.dem_path) as dem_src:
            dem_data = dem_src.read(1)
        if dem_nodata is not None:
            diff_array[dem_data == dem_nodata] = dem_nodata

        out_path = self.out_dir / self.out_name
        dem_meta.update({"dtype": "float32", "compress": "lzw", "count": 1})
        with rasterio.open(out_path, "w", **dem_meta) as dst:
            dst.write(diff_array, 1)

        log.info(f"Saved bridge_elev_diff raster --> {out_path}")
        return out_path

    def _load_bridges(self) -> gpd.GeoDataFrame:
        gdf = gpd.read_file(self.bridge_gpkg)
        if "osmid" in gdf.columns:
            col = "osmid"
        elif self.id_col and self.id_col in gdf.columns:
            col = self.id_col
        else:
            if self.id_col:
                log.warning(f"id_col='{self.id_col}' not found; using row index as bridge ID")
            gdf["_bridge_id"] = [f"bridge_{i}" for i in range(len(gdf))]
            return gdf
        gdf["_bridge_id"] = gdf[col].astype(str)
        return gdf

# Dask worker — must be module-level to be serialisable
def _compute_bridge_diff(
    bridge_id: str,
    tif_path: Optional[str],
    bridge_line,
    dem_path: str,
    dem_shape: tuple,
    dem_transform,
    dem_nodata,
    dem_crs: str,
) -> Optional[np.ndarray]:
    """
    Returns a float32 array of shape dem_shape with elev_diff values at bridge
    pixels and 0 elsewhere, or None on failure.
    """
    try:
        diff = np.zeros(dem_shape, dtype=np.float32)

        if tif_path is None or not os.path.exists(tif_path):
            return diff 

        # Convert per-bridge tif pixels --> points with lidar_elev
        points, lidar_vals = _tif_to_points(tif_path, dem_crs)
        if len(points) == 0:
            return diff

        # Keep only points inside the buffered bridge polygon
        import geopandas as gpd
        pts_gdf = gpd.GeoDataFrame({"lidar_elev": lidar_vals}, geometry=points, crs=dem_crs)
        mask = pts_gdf.within(bridge_line)
        pts_gdf = pts_gdf[mask]
        if pts_gdf.empty:
            return diff

        # Sample base DEM at each point
        coords = [(g.x, g.y) for g in pts_gdf.geometry]
        with rasterio.open(dem_path) as src:
            sampled = [v[0] for v in src.sample(coords)]
        pts_gdf["dem_elev"] = sampled
        pts_gdf["elev_diff"] = pts_gdf["lidar_elev"] - pts_gdf["dem_elev"]

        # Rasterize elev_diff onto DEM grid
        shapes = (
            (geom.__geo_interface__, val)
            for geom, val in zip(pts_gdf.geometry, pts_gdf["elev_diff"])
        )
        diff = rasterize(
            shapes=shapes,
            out_shape=dem_shape,
            transform=dem_transform,
            fill=0.0,
            merge_alg=rasterio.enums.MergeAlg.replace,
            dtype="float32",
        )
        return diff

    except Exception as exc:
        log.warning(f"bridge {bridge_id}: diff computation failed — {exc}")
        return None


def _tif_to_points(tif_path: str, target_crs: str):
    """Read a raster and return (list[Point], list[float]) for valid pixels, reprojected to target_crs."""
    with rasterio.open(tif_path) as src:
        data = src.read(1)
        nodata = src.nodata
        transform = src.transform
        src_crs = src.crs

        valid = data != nodata if nodata is not None else np.ones(data.shape, bool)
        rows, cols = np.where(valid)
        xs, ys = rasterio.transform.xy(transform, rows, cols, offset="center")
        vals = data[rows, cols].tolist()

    points = [Point(x, y) for x, y in zip(xs, ys)]

    if str(src_crs) != str(target_crs):
        import geopandas as gpd
        tmp = gpd.GeoDataFrame(geometry=points, crs=src_crs).to_crs(target_crs)
        points = list(tmp.geometry)

    return points, vals

# CLI
# Usage:
#   python bridge_dem_diff.py \
#       --dem_path      /path/to/dem.tif \
#       --lidar_tif_dir /path/to/lidar_osm_rasters \
#       --bridge_gpkg   /path/to/osm_bridges.gpkg \
#       --out_dir       /path/to/output \
#       --out_name      bridge_elev_diff.tif \
#       --bridge_buffer_m 2.0 \
#       --n_workers     4

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
    p.add_argument("--dem_path", required=True,
                   help="Path to base DEM .tif (3DEP or project DEM)")
    p.add_argument("--lidar_tif_dir", required=True,
                   help="Directory containing per-bridge LiDAR .tif files")
    p.add_argument("--bridge_gpkg", required=True,
                   help="Path to bridge lines GeoPackage (OSM or custom)")
    p.add_argument("--id_col", default="osmid",
                   help="Column used as unique bridge ID to match tif filenames (default: osmid). If missing, row indices are used.")
    p.add_argument("--out_dir", required=True,
                   help="Output directory for bridge_elev_diff.tif")
    p.add_argument("--out_name", default="bridge_elev_diff.tif",
                   help="Output filename (default: bridge_elev_diff.tif)")
    p.add_argument("--bridge_buffer_m", type=float, default=2.0,
                   help="Buffer around bridge lines to clip LiDAR points in metres (default: 2.0)")
    p.add_argument("--n_workers", type=int, default=None,
                   help="Dask thread workers (default: min(cpu_count, 8))")
    args = p.parse_args()

    kwargs = dict(
        dem_path=args.dem_path,
        lidar_tif_dir=args.lidar_tif_dir,
        bridge_gpkg=args.bridge_gpkg,
        out_dir=args.out_dir,
        out_name=args.out_name,
        bridge_buffer_m=args.bridge_buffer_m,
        id_col=args.id_col,
    )
    if args.n_workers is not None:
        kwargs["n_workers"] = args.n_workers

    out_path = BridgeDEMDiff(**kwargs).run()
    print(f"Bridge diff raster saved to: {out_path}")
