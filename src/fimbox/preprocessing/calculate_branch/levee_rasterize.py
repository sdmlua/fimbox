"""
Author: Supath Dhital
Date updated : May 2026

This module contains functions to rasterize 3D levee linestrings from a GeoPackage and burn them into a DEM. The main functions are:
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.transform

log = logging.getLogger(__name__)


def _line_cells(r0: int, c0: int, r1: int, c1: int) -> list[tuple[int, int]]:
    # Bresenham: all integer cells on the line from (r0,c0) to (r1,c1) inclusive.
    r0, c0, r1, c1 = int(r0), int(c0), int(r1), int(c1)
    dr, dc = abs(r1 - r0), abs(c1 - c0)
    sr, sc = (1 if r0 < r1 else -1), (1 if c0 < c1 else -1)
    err = dr - dc
    cells = []
    while True:
        cells.append((r0, c0))
        if r0 == r1 and c0 == c1:
            break
        e2 = 2 * err
        if e2 > -dc:
            err -= dc
            r0 += sr
        if e2 < dr:
            err += dr
            c0 += sc
    return cells


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

    # Mark every cell the line crosses (not just where samples land) so the
    # levee is a continuous ridge, not a dotted line. Max elevation wins per cell.
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
                n = max(2, int(np.ceil(seg_len / (res * 0.25)))) if seg_len > 0 else 1
                t = np.linspace(0, 1, n)
                xs = x0 + t * (x1 - x0)
                ys = y0 + t * (y1 - y0)
                zs = (z0 + t * (z1 - z0)).astype(np.float32)
                rows, cols = rasterio.transform.rowcol(transform, xs, ys)
                rows, cols = np.asarray(rows), np.asarray(cols)
                for k in range(len(rows)):
                    # connect consecutive samples to fill any gap between cells
                    cells = (
                        [(rows[k], cols[k])]
                        if k == 0
                        else _line_cells(rows[k - 1], cols[k - 1], rows[k], cols[k])
                    )
                    z = zs[k]
                    if z <= 0.0:  # NLD encodes missing crest elevation as 0
                        continue
                    for r, c in cells:
                        if 0 <= r < nrows and 0 <= c < ncols:
                            if out[r, c] == nodata or z > out[r, c]:
                                out[r, c] = z

    if skipped_2d:
        log.warning("  %d line segments skipped (no Z coordinates)", skipped_2d)

    burned_pixels = int((out != nodata).sum())
    log.info(
        "  levee elevation raster written --> %s  (%d pixels)",
        Path(out_path).name,
        burned_pixels,
    )
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
    with (
        rasterio.open(str(dem_path)) as dem,
        rasterio.open(str(levee_elev_raster)) as nld,
    ):
        dem_data = dem.read(1)
        nld_data = nld.read(1).astype(np.float32)
        nodata = float(nld.nodata) if nld.nodata is not None else -9999.0
        nld_masked = np.where(nld_data == nodata, nodata, nld_data)
        burned = np.maximum(dem_data, nld_masked)
        changed = int(np.sum(burned != dem_data))
        profile = dem.profile.copy()

        # Sanity check: if most levee cells sit below the underlying DEM, the
        # burn is a near no-op — usually a vertical datum/unit mismatch (e.g.
        # NLD in NAVD88 ft vs DEM in m). Warn rather than silently do nothing.
        lev = nld_data != nodata
        if lev.any():
            below = int(np.sum(nld_masked[lev] < dem_data[lev]))
            if below > 0.5 * int(lev.sum()):
                log.warning(
                    "  %d/%d levee cells are below the DEM — burn nearly a no-op; "
                    "check levee vertical datum/units vs DEM",
                    below,
                    int(lev.sum()),
                )
    with rasterio.open(str(out_path), "w", **profile) as dst:
        dst.write(burned, 1)
    log.info("  DEM burned --> %s  (%d cells raised)", Path(out_path).name, changed)


def mask_levee_dem(
    dem_path: Path,
    nld_path: Path,
    catchments_path: Path,
    out_path: Path,
    branch_id: int = 0,
    branch_zero_id: int = 0,
    levee_levelpaths_csv: "Optional[Path]" = None,
    levee_id_attribute: str = "levpa_id",
    branch_id_attribute: str = "levpa_id",
) -> None:
    """
    Port of inundation-mapping/src/mask_dem.py.

    Masks levee-protected areas from the DEM.  For branch-zero, masks all
    levee polygons.  For other branches, only masks areas associated with the
    level path (requires levee_levelpaths.csv) and areas from levee polygons
    whose catchments do not belong to the branch.

    Overwrites out_path in-place (caller should set out_path == dem_path to
    replicate the reference behaviour).
    """
    import pandas as pd
    from rasterio.mask import mask as rio_mask
    from shapely.geometry import box

    dem_path = Path(dem_path)
    nld_path = Path(nld_path)
    out_path = Path(out_path)

    if not dem_path.exists():
        log.warning("mask_levee_dem: DEM not found %s — skipping", dem_path)
        return
    if not nld_path.exists():
        log.warning("mask_levee_dem: NLD not found %s — skipping", nld_path)
        return

    log.info("Masking levee-protected areas: branch=%s", branch_id)

    with rasterio.open(str(dem_path)) as dem:
        dem_profile = dem.profile.copy()
        nodata = dem.nodata
        dem_crs = dem.crs
        dem_bounds = dem.bounds

    raster_box = box(
        dem_bounds.left, dem_bounds.bottom, dem_bounds.right, dem_bounds.top
    )

    def _clip_geoms(geoms):
        clipped = []
        for g in geoms:
            if g and g.is_valid and g.intersects(raster_box):
                c = g.intersection(raster_box)
                if not c.is_empty:
                    clipped.append(c)
        return clipped

    dem_masked = None
    levee_catchments_masked = None

    leveed = gpd.read_file(str(nld_path), engine="fiona")
    if leveed.crs != dem_crs:
        leveed = leveed.to_crs(dem_crs)

    with rasterio.open(str(dem_path)) as dem:
        if branch_id == branch_zero_id:
            geoms = _clip_geoms(list(leveed.geometry))
            if geoms:
                dem_masked, _ = rio_mask(dem, geoms, invert=True)

        elif levee_levelpaths_csv and Path(levee_levelpaths_csv).exists():
            catchments_path = Path(catchments_path)
            if catchments_path.exists():
                catchments = gpd.read_file(str(catchments_path), engine="fiona")
                if catchments.crs != dem_crs:
                    catchments = catchments.to_crs(dem_crs)
            else:
                catchments = gpd.GeoDataFrame()

            lp_df = pd.read_csv(str(levee_levelpaths_csv))
            lp_df = lp_df[lp_df[branch_id_attribute] == branch_id]
            levelpath_levees = list(lp_df[levee_id_attribute])

            if levelpath_levees:
                sel = leveed[leveed[levee_id_attribute].isin(levelpath_levees)]
                geoms = _clip_geoms(list(sel.geometry))
                if geoms:
                    dem_masked, _ = rio_mask(dem, geoms, invert=True)

            if not catchments.empty:
                leveed_area = gpd.overlay(catchments, leveed, how="union")
                to_mask = leveed_area.loc[
                    ~leveed_area[levee_id_attribute].isna()
                    & leveed_area.get("ID", pd.Series(dtype=object)).isna()
                ]
                geoms = _clip_geoms(list(to_mask.geometry))
                if geoms:
                    levee_catchments_masked, _ = rio_mask(dem, geoms, invert=True)

    out_masked = None
    if dem_masked is None:
        if levee_catchments_masked is not None:
            out_masked = levee_catchments_masked
    else:
        if levee_catchments_masked is None:
            out_masked = dem_masked
        else:
            out_masked = np.where(levee_catchments_masked == nodata, nodata, dem_masked)

    if out_masked is not None:
        dem_profile.update(
            BIGTIFF="YES", compress="lzw", tiled=True, blockxsize=512, blockysize=512
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(str(out_path), "w", **dem_profile) as dst:
            dst.write(out_masked[0, :, :], indexes=1)
        log.info("  levee mask written --> %s", out_path.name)
    else:
        log.info("  no levee masking applied for branch %s", branch_id)
