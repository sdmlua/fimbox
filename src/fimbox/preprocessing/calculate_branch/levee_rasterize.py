"""
Author: Supath Dhital
Date updated : May 2026

This module contains functions to rasterize 3D levee linestrings from a GeoPackage and burn them into a DEM. The main functions are:
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.transform

log = logging.getLogger(__name__)


def rasterize_3d_levee_lines(
    levees_gpkg: Path,
    dem_path: Path,
    out_path: Path,
    layer: str | None = None,
) -> None:
    # rasterize 3D levee linestrings using Z vertex elevations (all-touched, max-wins)
    # if layer is None, read the first layer in the GeoPackage
    import fiona

    if layer is None:
        layers = fiona.listlayers(str(levees_gpkg))
        if not layers:
            raise ValueError(f"No layers found in {levees_gpkg}")
        layer = layers[0]

    log.info("Rasterizing 3D levee lines: %s  layer=%s", Path(levees_gpkg).name, layer)

    levees = gpd.read_file(str(levees_gpkg), layer=layer, engine="pyogrio")
    log.debug("  %d levee features loaded", len(levees))

    with rasterio.open(str(dem_path)) as src:
        profile = src.profile.copy()
        nodata = float(src.nodata) if src.nodata is not None else -9999.0
        transform = src.transform
        nrows, ncols = src.height, src.width
        res = min(abs(transform.a), abs(transform.e))

    out = np.full((nrows, ncols), nodata, dtype=np.float32)
    skipped_2d = 0

    for geom in levees.geometry:
        if geom is None or geom.is_empty:
            continue
        lines = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for line in lines:
            coords = list(line.coords)
            if not coords or len(coords[0]) < 3:
                skipped_2d += 1
                continue
            for i in range(len(coords) - 1):
                x0, y0, z0 = coords[i][0], coords[i][1], coords[i][2]
                x1, y1, z1 = coords[i + 1][0], coords[i + 1][1], coords[i + 1][2]
                seg_len = np.hypot(x1 - x0, y1 - y0)
                n = max(2, int(np.ceil(seg_len / (res * 0.5)))) if seg_len > 0 else 1
                t = np.linspace(0, 1, n)
                xs = x0 + t * (x1 - x0)
                ys = y0 + t * (y1 - y0)
                zs = (z0 + t * (z1 - z0)).astype(np.float32)
                rows, cols = rasterio.transform.rowcol(transform, xs, ys)
                rows, cols = np.asarray(rows), np.asarray(cols)
                valid = (rows >= 0) & (rows < nrows) & (cols >= 0) & (cols < ncols)
                for r, c, z in zip(rows[valid], cols[valid], zs[valid]):
                    if out[r, c] == nodata or z > out[r, c]:
                        out[r, c] = z

    if skipped_2d:
        log.warning("  %d line segments skipped (no Z coordinates)", skipped_2d)

    burned_pixels = int((out != nodata).sum())
    log.info("  levee elevation raster written → %s  (%d pixels)", Path(out_path).name, burned_pixels)
    if burned_pixels == 0:
        log.warning("  levee raster has 0 valid pixels — check levee CRS and Z values")

    profile.update(dtype="float32", nodata=nodata)
    with rasterio.open(str(out_path), "w", **profile) as dst:
        dst.write(out, 1)


def burn_levee_elevations(
    dem_path: Path,
    levee_elev_raster: Path,
    out_path: Path,
) -> None:
    # raise DEM to levee elevation where levee raster has valid data
    log.info("Burning levee elevations into DEM: %s", Path(dem_path).name)
    with rasterio.open(str(dem_path)) as dem, rasterio.open(str(levee_elev_raster)) as nld:
        dem_data = dem.read(1)
        nld_data = nld.read(1).astype(np.float32)
        nodata = float(nld.nodata) if nld.nodata is not None else -9999.0
        nld_masked = np.where(nld_data == nodata, nodata, nld_data)
        burned = np.maximum(dem_data, nld_masked)
        changed = int(np.sum(burned != dem_data))
        profile = dem.profile.copy()
    with rasterio.open(str(out_path), "w", **profile) as dst:
        dst.write(burned, 1)
    log.info("  DEM burned → %s  (%d cells raised)", Path(out_path).name, changed)
