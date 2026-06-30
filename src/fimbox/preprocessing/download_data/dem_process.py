"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date updated: June 2026

3DEP DEM fetch with conditioning. The single DEM entry point ``DEMProcessor``
either downloads a 3DEP DEM from cloud-optimized GeoTIFFs, or
conditions a bring-your-own ``dem_file`` with the same steps (reproject ->
hole-fill -> clip).

Resolutions (default 10 m): 10/30 m are seamless nationwide (3dep-seamless);
1/3 m come from 2 m lidar-dtm where project lidar covers the AOI; 60 m is
Alaska-only (USGS 2 arc-second VRT). A resolution with no data for the AOI
raises :class:`DEMResolutionUnavailable` after logging, so the caller can fall
back to 10 m.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import List, Optional, Union

import geopandas as gpd
import numpy as np
import rioxarray 
import xarray as xr
from affine import Affine
from rasterio.enums import Resampling
from shapely.geometry import box

from ..._skip_if_valid import should_skip


# Planetary Computer STAC endpoint.
PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

_SEAMLESS = "3dep-seamless"
_LIDAR_DTM = "3dep-lidar-dtm"  # bare-earth, gsd 2 m, project coverage

# USGS national seamless VRTs (legacy origin), used only where PC lacks coverage.
_USGS_BASE = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation"
_USGS_VRT = {
    10: f"{_USGS_BASE}/13/TIFF/USGS_Seamless_DEM_13.vrt",
    30: f"{_USGS_BASE}/1/TIFF/USGS_Seamless_DEM_1.vrt",
    60: f"{_USGS_BASE}/2/TIFF/USGS_Seamless_DEM_2.vrt",  # Alaska-only
}

# Default resolution when the caller asks for nothing.
DEFAULT_RESOLUTION = 10

# How each requested resolution is served.
#   "usgs_vrt"   : USGS national seamless VRT via /vsicurl/ (10/30/60 m). Read
#                  windowed, this is markedly faster from most US networks than
#                  the PC COG endpoint (measured ~4.6 vs ~0.8 MB/s), so it is the
#                  primary source for the seamless tiers. (60 m is Alaska-only.)
#   "stac_lidar" : PC 3dep-lidar-dtm (2 m), AOI-dependent (serves 1/3 m best-effort)
# If a USGS VRT read fails (e.g. S3 hiccup), the seamless tiers fall back to PC
# 3dep-seamless automatically in _fetch(). Third tuple element is source gsd.
RESOLUTION_PLAN = {
    1: ("stac_lidar", _LIDAR_DTM, 2),
    3: ("stac_lidar", _LIDAR_DTM, 2),
    10: ("usgs_vrt", _SEAMLESS, 10),
    30: ("usgs_vrt", _SEAMLESS, 30),
    60: ("usgs_vrt", None, 60),
}

SUPPORTED_RESOLUTIONS = tuple(sorted(RESOLUTION_PLAN))


class DEMResolutionUnavailable(RuntimeError):
    """Raised when the requested resolution has no data covering the AOI.

    The message is human-readable (e.g. "1 m DEM is not available for your
    AOI."); the caller may catch it, log, and fall back to 10 m.
    """


_GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",  # skip per-open S3 dir listing
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.vrt",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "100000000",  # 100 MB per-file read cache
    "GDAL_CACHEMAX": "512",  # MB block cache
    "GDAL_NUM_THREADS": "ALL_CPUS",  # scales block transfer to the machine
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


class DEMProcessor:
    """Fetch (or condition a local) 3DEP DEM for an AOI and write a GeoTIFF.

    Parameters
    ----------
    boundary : GeoDataFrame or path
        AOI polygon(s). Any CRS; reprojected internally.
    layer : str, optional
        Layer name when ``boundary`` is a multi-layer file.
    output_dir : str or Path, optional
        Where the GeoTIFF is written. Defaults to the package output dir.
    out_name : str, optional
        Output filename. Defaults to ``3dep_dem_<res>m.tif`` (download) or
        ``processed_local_dem.tif`` (when ``dem_file`` is given).
    dem_file : str, optional
        Bring-your-own DEM. When set, it is reprojected/hole-filled/clipped with
        the same conditioning a downloaded DEM gets, instead of fetching 3DEP.
    resolution : int
        Target resolution in metres. One of 1, 3, 10, 30, 60. Default 10.
    epsg : int, optional
        Output CRS. Defaults to an auto-estimated UTM zone.
    use_dask : bool
        Chunk through Dask for the reproject/heal stage. Default True. If Dask
        is missing or errors at runtime it silently falls back to numpy, so this
        never breaks a fetch.
    chunksize : int, optional
        Dask chunk edge in pixels. Default None -> auto from array size + CPU
        count (~a few chunks per core, clamped 512-4096 px). An int forces it.
    heal_seams : bool
        Fill thin (<=3 cell) interior nodata seams left at tile joins. Default
        True.
    fallback_to_10m : bool
        When the requested resolution has no data for the AOI, retry at 10 m
        instead of raising — i.e. "give me 1 m, else just 10 m". Default False.
    run : bool
        Execute immediately on construction (legacy behaviour). Default True.
    """

    def __init__(
        self,
        boundary: Union[str, gpd.GeoDataFrame],
        layer: Optional[str] = None,
        output_dir: Optional[Union[str, Path]] = None,
        out_name: Optional[str] = None,
        dem_file: Optional[str] = None,
        resolution: int = DEFAULT_RESOLUTION,
        epsg: Optional[int] = None,
        use_dask: bool = True,
        chunksize: Optional[int] = None,
        heal_seams: bool = True,
        fallback_to_10m: bool = False,
        run: bool = True,
        tile_size_deg: Optional[float] = None,
        max_workers: Optional[int] = None,
    ):
        self.boundary_input = boundary
        self.layer = layer
        self.dem_file = dem_file
        self.resolution = int(resolution)
        self.use_dask = use_dask
        # None -> auto (see _auto_chunksize); an int is used verbatim.
        self.chunksize = int(chunksize) if chunksize else None
        self.heal_seams_flag = heal_seams
        self.fallback_to_10m = fallback_to_10m

        from ...logging_utils import default_output_dir

        self.out_name = out_name
        self.output_dir = Path(output_dir) if output_dir else default_output_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # "DEMProcessor" logger name links into the pipeline log.
        self.logger = logging.getLogger("DEMProcessor")

        if not self.dem_file and self.resolution not in RESOLUTION_PLAN:
            raise ValueError(
                f"Unsupported resolution {self.resolution}; "
                f"choose from {list(SUPPORTED_RESOLUTIONS)}"
            )

        self.gdf_wgs84 = self._load_gdf().to_crs(epsg=4326)
        self.boundary_geom = self.gdf_wgs84.union_all()
        self.target_crs = epsg if epsg else self._estimate_utm_crs()

        # Fraction of AOI covered by the fetched tiles (set during a STAC fetch):
        # 1.0 for seamless, <1.0 for partial lidar, -1.0 when not applicable.
        self.coverage_fraction: float = -1.0

        self.result_path: Optional[str] = None
        if run:
            self.result_path = self.run()

    # boundary helpers
    def _load_gdf(self) -> gpd.GeoDataFrame:
        if isinstance(self.boundary_input, gpd.GeoDataFrame):
            if self.boundary_input.empty:
                raise ValueError("Boundary GeoDataFrame is empty.")
            return self.boundary_input
        gdf = gpd.read_file(self.boundary_input, layer=self.layer)
        if gdf.empty:
            raise ValueError("The provided boundary file is empty.")
        return gdf

    def _estimate_utm_crs(self) -> int:
        b = self.gdf_wgs84.total_bounds
        lon = (b[0] + b[2]) / 2
        lat = (b[1] + b[3]) / 2
        zone = int((lon + 180) / 6) + 1
        return (32600 if lat >= 0 else 32700) + zone

    @staticmethod
    def _apply_gdal_env() -> None:
        """Set the GDAL /vsicurl tuning vars without clobbering user overrides."""
        for k, v in _GDAL_ENV.items():
            os.environ.setdefault(k, v)

    @staticmethod
    def _has_valid_data(arr: np.ndarray, nodata) -> bool:
        """True if ``arr`` has at least one finite, non-nodata pixel."""
        finite = np.isfinite(arr)
        if nodata is not None and np.isfinite(nodata):
            finite &= arr != nodata
        return bool(finite.any())

    def _aoi_bbox_in(self, crs) -> tuple:
        """AOI bbox (minx, miny, maxx, maxy) expressed in ``crs``."""
        minx, miny, maxx, maxy = self.boundary_geom.bounds
        return (
            gpd.GeoSeries([box(minx, miny, maxx, maxy)], crs=4326)
            .to_crs(crs)
            .total_bounds
        )

    # STAC search
    def _stac_items(self, collection: str, gsd: Optional[int]):
        """Signed STAC items intersecting the AOI; empty list if none."""
        import planetary_computer as pc
        from pystac_client import Client

        minx, miny, maxx, maxy = self.boundary_geom.bounds
        catalog = Client.open(PC_STAC_URL, modifier=pc.sign_inplace)
        query = {"gsd": {"eq": gsd}} if gsd is not None else None
        search = catalog.search(
            collections=[collection], bbox=[minx, miny, maxx, maxy], query=query
        )
        items = list(search.items())
        self.logger.info(f"STAC: {len(items)} item(s) in '{collection}' intersect AOI")
        return items

    @staticmethod
    def _data_asset_key(item) -> str:
        """Pick the elevation/data raster asset key for a 3DEP STAC item."""
        for key in ("data", "elevation", "dem", "dtm", "dsm"):
            if key in item.assets:
                return key
        for key, asset in item.assets.items():
            mt = (asset.media_type or "").lower()
            href = str(asset.href).lower()
            if "tiff" in mt or href.endswith((".tif", ".tiff")):
                return key
        raise KeyError(f"No raster asset found on item {item.id}")

    def _log_coverage(self, items) -> float:
        """Log what fraction of the AOI the STAC item footprints cover.

        Seamless tiers return ~100 %; lidar tiers (1/2/3 m) are project-based, this reports the 
        overlapping and warns if the AOI is only partially covered. Returns the fraction (0.0-1.0) 
        or -1.0 if the footprints are unavailable (e.g. a STAC item has no geometry).
        """
        from shapely.geometry import shape
        from shapely.ops import unary_union

        try:
            geoms = [shape(it.geometry) for it in items if it.geometry]
            if not geoms:
                self.logger.info("Coverage: item footprints unavailable; continuing.")
                return -1.0
            covered = unary_union(geoms).intersection(self.boundary_geom)
            aoi_area = self.boundary_geom.area
            frac = (covered.area / aoi_area) if aoi_area > 0 else 0.0
            pct = frac * 100.0
            if frac >= 0.999:
                self.logger.info(f"Coverage: ~{pct:.0f}% of AOI covered.")
            else:
                self.logger.warning(
                    f"Coverage: only ~{pct:.1f}% of the AOI is covered by "
                    f"{self.resolution} m data — the rest will be nodata (holes). "
                    f"Continuing with the available tiles."
                )
            return frac
        except Exception as exc:  # noqa: BLE001 - coverage note must never break the fetch
            self.logger.info(f"Coverage: could not compute ({exc}); continuing.")
            return -1.0

    # windowed read across the intersecting tiles
    def _auto_chunksize(self, ny: int, nx: int) -> int:
        """Pick a dask chunk edge so there are ~a few chunks per CPU worker.

        Parallelism in dask = how many chunks run at once, so we want chunk
        count >= worker count or cores sit idle. Target ~4 chunks per core, then
        clamp the edge to [512, 4096] px (too small = task overhead, too large =
        too few chunks). An explicit ``chunksize=`` skips this.
        """
        if self.chunksize:  # explicit override
            return self.chunksize
        workers = max(1, os.cpu_count() or 1)
        target_chunks = workers * 4
        longest = max(ny, nx)
        edge = int(longest / max(1, target_chunks**0.5))
        return max(512, min(4096, edge))

    def _read_window(self, hrefs: List[str]) -> xr.DataArray:
        """Read only the AOI window from the tiles in one windowed call.

        ``rasterio.merge(bounds=...)`` range-reads just the overlapping window of
        each COG and stitches adjacent tiles in a single pass (no separate
        mosaic stage). GDAL_NUM_THREADS=ALL_CPUS parallelises block transfer.
        """
        import rasterio
        from rasterio.merge import merge

        self._apply_gdal_env()
        vsi = [h if h.startswith("/vsicurl/") else "/vsicurl/" + h for h in hrefs]

        srcs = []
        for v in vsi:
            try:
                srcs.append(rasterio.open(v))
            except Exception as exc:
                self.logger.warning(f"Skipping unreadable tile {v}: {exc}")
        if not srcs:
            # A read/open failure (network, bad URL) is NOT genuine no-data — a
            # plain error lets _fetch fall back to the alternate source.
            raise RuntimeError(
                f"{self.resolution} m DEM tiles could not be opened for your AOI."
            )

        try:
            crs0 = srcs[0].crs  # CRS of the first drives the output grid
            tb = self._aoi_bbox_in(crs0)
            arr, transform = merge(srcs, bounds=tuple(tb))
            nodata = srcs[0].nodata
        finally:
            for s in srcs:
                s.close()

        if arr.ndim == 3:
            arr = arr[0]

        # All-nodata window -> the source does not actually cover the AOI (e.g.
        # the 60 m VRT is Alaska-only). Report unavailable instead of writing a
        # blank DEM, so the caller can fall back to 10 m.
        if not self._has_valid_data(arr, nodata):
            detail = (
                " (the 60 m product is Alaska-only; no 60 m DEM exists for the "
                "lower 48)"
                if self.resolution == 60
                else " (source does not cover this area)"
            )
            raise DEMResolutionUnavailable(
                f"{self.resolution} m DEM has no valid data over your AOI{detail}."
            )

        ny, nx = arr.shape
        xs = transform.c + transform.a * (np.arange(nx) + 0.5)
        ys = transform.f + transform.e * (np.arange(ny) + 0.5)
        data = arr
        if self.use_dask:
            # Dask on by default; if missing/failing, fall back to numpy.
            try:
                import dask.array as dskarr

                cs = self._auto_chunksize(ny, nx)
                data = dskarr.from_array(arr, chunks=(cs, cs))
                self.logger.info(f"Dask chunking: edge={cs} px on {os.cpu_count()} CPUs")
            except Exception as exc: 
                self.logger.warning(
                    f"Dask unavailable/failed ({exc}); using normal numpy path."
                )
                data = arr

        da = xr.DataArray(
            data, coords={"y": ys, "x": xs}, dims=("y", "x"), name="elevation"
        )
        da = da.rio.write_crs(crs0)
        if nodata is not None:
            da = da.rio.write_nodata(nodata)
        return da

    # source backends -> a single (unreprojected) DataArray over the AOI
    def _from_stac(self, collection: str, gsd: Optional[int]) -> xr.DataArray:
        items = self._stac_items(collection, gsd)
        if not items:
            raise DEMResolutionUnavailable(
                f"{self.resolution} m DEM is not available for your AOI."
            )
        self.coverage_fraction = self._log_coverage(items)
        hrefs = [it.assets[self._data_asset_key(it)].href for it in items]
        return self._read_window(hrefs)

    def _from_usgs_vrt(self) -> xr.DataArray:
        """Read the AOI window from a USGS national seamless VRT (10/30/60 m)."""
        url = _USGS_VRT.get(self.resolution)
        if url is None:
            raise DEMResolutionUnavailable(
                f"No USGS seamless VRT for {self.resolution} m."
            )
        return self._read_window([url])

    def _from_local_file(self) -> xr.DataArray:
        """Open a bring-your-own DEM file as the source array (native grid)."""
        self.logger.info(f"Processing local DEM file: {self.dem_file}")
        da = rioxarray.open_rasterio(self.dem_file, masked=True)
        if "band" in da.dims:
            da = da.squeeze("band", drop=True)
        return da

    # conditioning (shared by download + BYO paths)
    def _condition(self, da: xr.DataArray) -> xr.DataArray:
        """Reproject (snapped grid), heal seams, set nodata, clip to AOI polygon.

        Reprojection pins the exact target resolution and snaps the grid origin
        to a multiple of it; otherwise ``rio.reproject`` picks its own grid
        (e.g. 9.215 m cells at an arbitrary origin) and DEMs of the same AOI
        land on slightly offset grids. A snapped integer grid is reproducible
        and overlay-aligned. Single resample (no double warp).
        """
        from rasterio.warp import calculate_default_transform

        res = float(self.resolution)
        src_crs = da.rio.crs
        dst_crs = f"EPSG:{self.target_crs}"

        left, bottom, right, top = da.rio.bounds()
        transform, width, height = calculate_default_transform(
            src_crs, dst_crs, da.rio.width, da.rio.height,
            left=left, bottom=bottom, right=right, top=top, resolution=res,
        )
        # Snap origin outward to a whole multiple of the resolution, extend the
        # grid so the snapped extent still covers everything, rebuild transform.
        snap_x = math.floor(transform.c / res) * res
        snap_y = math.ceil(transform.f / res) * res
        width = int(width + math.ceil(abs(transform.c - snap_x) / res))
        height = int(height + math.ceil(abs(transform.f - snap_y) / res))
        snapped = Affine(res, 0.0, snap_x, 0.0, -res, snap_y)

        da = da.rio.reproject(
            dst_crs, transform=snapped, shape=(height, width),
            resampling=Resampling.bilinear,
        )

        if self.heal_seams_flag:
            da = self._heal_seams(da)

        da = da.where(da > -90000, -999999)
        da.rio.write_nodata(-999999, inplace=True)

        gdf_proj = self.gdf_wgs84.to_crs(epsg=self.target_crs)
        da = da.rio.clip(gdf_proj.geometry, gdf_proj.crs, drop=True, all_touched=True)
        return da

    def _heal_seams(self, dem: xr.DataArray) -> xr.DataArray:
        """Fill thin (<=3 cell) interior nodata seams left at tile joins.

        ``fillnodata`` is numpy, so this materialises the lazy Dask graph — i.e.
        the streamed read/mosaic/clip is actually computed here.
        """
        from rasterio.fill import fillnodata

        nodata = dem.rio.nodata
        arr = dem.squeeze().to_numpy()
        mask = (
            np.isfinite(arr) if nodata is None else (arr != nodata) & np.isfinite(arr)
        )
        n_gap = int((~mask).sum())
        if n_gap == 0:
            return dem
        filled = fillnodata(
            arr.astype("float32"), mask=mask.astype(np.uint8), max_search_distance=3.0
        )
        n_left = (
            int(np.isnan(filled).sum())
            if nodata is None
            else int((filled == nodata).sum())
        )
        self.logger.info(
            f"Healed DEM seams: {n_gap - n_left} of {n_gap} nodata cells filled"
        )
        out = dem.copy()
        out.data = filled.reshape(dem.shape)
        return out

    # main
    def run(self) -> str:
        # Resolve the output path up front so skip-if-valid can short-circuit
        # before any network / reprojection work.
        save_path = self.output_dir / (
            self.out_name
            or (
                "processed_local_dem.tif"
                if self.dem_file
                else f"3dep_dem_{self.resolution}m.tif"
            )
        )
        if should_skip(save_path):
            self.logger.info(f"DEM output already valid, skipping: {save_path}")
            return str(save_path)

        export_kwargs = {
            "driver": "GTiff",
            "compress": "lzw",
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
        }

        # bring-your-own DEM: same conditioning, no network.
        if self.dem_file:
            self.logger.info(f"--- DEM (BYO {Path(self.dem_file).name}) ---")
            dem = self._condition(self._from_local_file())
            dem.rio.to_raster(save_path, **export_kwargs)
            self.logger.info(f"DEM successfully saved to {save_path}")
            return str(save_path)

        # download via STAC / VRT.
        try:
            da = self._fetch()
        except DEMResolutionUnavailable as exc:
            # "give me 1 m, else just 10 m": optionally retry at the default.
            if self.fallback_to_10m and self.resolution != DEFAULT_RESOLUTION:
                self.logger.warning(
                    f"{exc} Falling back to {DEFAULT_RESOLUTION} m."
                )
                self.resolution = DEFAULT_RESOLUTION
                da = self._fetch()
            else:
                self.logger.warning(str(exc))  # let the caller move on
                raise

        dem = self._condition(da)
        dem.rio.to_raster(save_path, **export_kwargs)
        self.logger.info(f"DEM successfully saved to {save_path}")
        return str(save_path)

    def _fetch(self) -> xr.DataArray:
        """Dispatch the current resolution to its backend -> AOI DataArray.

        Seamless tiers (10/30/60 m) read the USGS VRT first (faster from US
        networks); if that read fails for any reason other than genuine no-data,
        they fall back to the PC 3dep-seamless COGs. Lidar tiers (1/3 m) go
        straight to PC.
        """
        backend, collection, source_gsd = RESOLUTION_PLAN[self.resolution]
        self.logger.info(
            f"Fetching {self.resolution} m DEM via {backend} "
            f"(dask={'on' if self.use_dask else 'off'}) -> EPSG:{self.target_crs}"
        )
        if backend == "stac_lidar":
            return self._from_stac(collection, source_gsd)
        if backend == "usgs_vrt":
            try:
                return self._from_usgs_vrt()
            except DEMResolutionUnavailable:
                raise  # genuine no-data (e.g. CONUS 60 m) -> don't mask it
            except Exception as exc:  # noqa: BLE001 - VRT read failed; try PC instead
                if collection is None:  # 60 m has no PC equivalent
                    raise
                self.logger.warning(
                    f"USGS VRT read failed ({exc}); falling back to PC STAC."
                )
                return self._from_stac(collection, source_gsd)
        raise DEMResolutionUnavailable(f"No backend for {self.resolution} m.")  # pragma: no cover
