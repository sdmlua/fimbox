"""
Author: Supath Dhital
Branch Zero preprocessing

Steps performed:
  1. Clip 3DEP DEM to HUC boundary  --> dem.tif
  2. Clip bridge_elev_diff raster   --> bridge_elev_diff.tif  (optional)
  3. Burn levees into DEM           --> overwrites dem_{id}.tif  (optional)
  4. Rasterize NWM streams          --> flows_grid_boolean_{id}.tif
  5. AGREE DEM conditioning         --> dem_burned_{id}.tif
  6. Fill depressions (pit removal) --> dem_burned_filled_{id}.tif
  7. D8 flow directions             --> flowdir_d8_burned_filled_{id}.tif
"""

from __future__ import annotations

import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.warp import Resampling, reproject as warp_reproject

from .flowdir_dem import FlowdirDEM
from .hydroenforce_dem import HydroenforceDEM
from .levee_rasterize import burn_levee_elevations, rasterize_3d_levee_lines
from .reach_rasterize import (
    HeadwaterRasterizer,
    LevelPathBooleanRasterizer,
    StreamBooleanRasterizer,
)

log = logging.getLogger(__name__)


@dataclass
class BranchZero:
    """
    Preprocesses the base raster stack for an area.

    Parameters
    ----------
    dem_path              : 3DEP DEM covering the HUC area
    streams_gpkg          : NWM subset streams GeoPackage
    boundary_gpkg         : WBD buffered boundary used for clipping
    out_dir               : directory to write all outputs
    bridge_elev_diff_path : bridge_elev_diff.tif from BridgeDEMDiff (optional)
    levee_gpkg_path       : 3D NLD levee GeoPackage with Z-elevation vertices (optional)
    levee_raster_path     : pre-rasterized NLD levee elevation raster (optional, used when
                            levee_gpkg_path is not provided)
    headwaters_gpkg       : NWM headwater points GeoPackage (optional)
    levelpaths_extended_gpkg : extended level path streams GeoPackage (optional)
    target_crs            : EPSG string e.g. "EPSG:5070"; defaults to DEM CRS
    resolution            : output pixel size in metres; defaults to DEM resolution
    agree_buffer_m        : AGREE stream buffer distance in metres (default 15)
    agree_smooth_drop     : AGREE smooth drop in metres (default 10)
    agree_sharp_drop      : AGREE sharp drop in metres (default 1000)
    stream_value          : pixel value in flows_grid_boolean identifying stream cells (default 1);
                            set this if your flowline raster uses a different burn value
    wbt_path              : WhiteboxTools executable directory; falls back to WBT_PATH env var
    keep_agree_intermediates : keep AGREE workspace files after run (useful for debugging)
    branch_zero_id        : suffix used in output filenames (default "0")
    """

    dem_path: Union[str, Path]
    streams_gpkg: Union[str, Path]
    boundary_gpkg: Union[str, Path]
    out_dir: Union[str, Path]
    bridge_elev_diff_path: Optional[Union[str, Path]] = None
    levee_gpkg_path: Optional[Union[str, Path]] = None
    levee_raster_path: Optional[Union[str, Path]] = None
    headwaters_gpkg: Optional[Union[str, Path]] = None
    levelpaths_extended_gpkg: Optional[Union[str, Path]] = None
    target_crs: Optional[str] = None
    resolution: Optional[float] = None
    agree_buffer_m: float = 15.0
    agree_smooth_drop: float = 10.0
    agree_sharp_drop: float = 1000.0
    stream_value: int = 1
    wbt_path: Optional[str] = None
    keep_agree_intermediates: bool = False
    branch_zero_id: str = "0"

    def __post_init__(self):
        self.dem_path = Path(self.dem_path)
        self.streams_gpkg = Path(self.streams_gpkg)
        self.boundary_gpkg = Path(self.boundary_gpkg)
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.bridge_elev_diff_path:
            self.bridge_elev_diff_path = Path(self.bridge_elev_diff_path)
        if self.levee_gpkg_path:
            self.levee_gpkg_path = Path(self.levee_gpkg_path)
        if self.levee_raster_path:
            self.levee_raster_path = Path(self.levee_raster_path)
        if self.headwaters_gpkg:
            self.headwaters_gpkg = Path(self.headwaters_gpkg)
        if self.levelpaths_extended_gpkg:
            self.levelpaths_extended_gpkg = Path(self.levelpaths_extended_gpkg)

    def run(self) -> dict:
        """Run all Phase-2 steps. Returns dict of output Path objects."""
        from ...logging_utils import attach_case_log

        attach_case_log(self.out_dir)
        try:
            log.info(f"--- BranchZero: branch_id={self.branch_zero_id} ---")
            log.info(f"out_dir: {self.out_dir}")
            log.info(f"dem: {self.dem_path}")
            log.info(f"streams: {self.streams_gpkg}")
            log.info(f"boundary: {self.boundary_gpkg}")
            log.info(f"bridge_diff: {self.bridge_elev_diff_path or 'not provided'}")
            log.info(f"levees: {self.levee_raster_path or 'not provided'}")
            log.info(
                f"AGREE: buffer={self.agree_buffer_m:.0f}m  "
                f"smooth={self.agree_smooth_drop:.0f}m  "
                f"sharp={self.agree_sharp_drop:.0f}m"
            )
            result = self._run()
            log.info(f"BranchZero complete: {len(result)} output files written")
            return result
        except Exception:
            log.exception("BranchZero failed")
            raise

    def _run(self) -> dict:
        bid = self.branch_zero_id
        branch_dir = self.out_dir / "branches" / bid
        branch_dir.mkdir(parents=True, exist_ok=True)

        crs, res = self._resolve_crs_and_res()

        # Branch 0 publishes its clipped DEM at the AOI root (downstream
        # tools expect <aoi_dir>/dem.tif). Non-zero branches must NOT
        # overwrite that file — every branch runs in parallel and racing
        # on the AOI-root dem.tif corrupts the shared input. Non-zero
        # branches write their per-branch clip directly into branch_dir.
        is_branch_zero = bid == "0"
        if is_branch_zero:
            dem_clipped = self.out_dir / "dem.tif"
        else:
            dem_clipped = branch_dir / f"dem_{bid}.tif"

        _rasterio_clip_reproject(
            self.dem_path, self.boundary_gpkg, dem_clipped, crs=crs, res=res
        )
        log.info("DEM clipped --> %s", dem_clipped.name)

        dem_branch = branch_dir / f"dem_{bid}.tif"
        if dem_clipped != dem_branch:
            shutil.copy2(dem_clipped, dem_branch)

        # clip bridge elev diff once, then copy into branch subdirectory
        bridge_clipped: Optional[Path] = None
        bridge_branch: Optional[Path] = None
        if self.bridge_elev_diff_path and self.bridge_elev_diff_path.exists():
            if is_branch_zero:
                bridge_clipped = self.out_dir / "bridge_elev_diff.tif"
            else:
                bridge_clipped = branch_dir / f"bridge_elev_diff_{bid}.tif"
            _rasterio_clip_reproject(
                self.bridge_elev_diff_path,
                self.boundary_gpkg,
                bridge_clipped,
                crs=crs,
                res=res,
            )
            log.info("Bridge elev diff clipped --> %s", bridge_clipped.name)
            bridge_branch = branch_dir / f"bridge_elev_diff_{bid}.tif"
            if bridge_clipped != bridge_branch:
                shutil.copy2(bridge_clipped, bridge_branch)

        # rasterize 3D levee lines if GeoPackage provided, then burn into DEM
        if self.levee_gpkg_path and self.levee_gpkg_path.exists():
            levee_elev_raster = branch_dir / f"nld_rasterized_elev_{bid}.tif"
            rasterize_3d_levee_lines(
                self.levee_gpkg_path, dem_branch, levee_elev_raster
            )
            log.info("Levee lines rasterized --> %s", levee_elev_raster.name)
            burn_levee_elevations(dem_branch, levee_elev_raster, dem_branch)
            log.info("Levees burned into DEM")
        elif self.levee_raster_path and self.levee_raster_path.exists():
            burn_levee_elevations(dem_branch, self.levee_raster_path, dem_branch)
            log.info("Levees burned into DEM")

        # Boolean grid AGREE burns: branch 0 uses all NWM streams; non-zero
        # branches use their extended level path.
        flows_bool = branch_dir / f"flows_grid_boolean_{bid}.tif"
        use_levelpaths = (
            not is_branch_zero
            and self.levelpaths_extended_gpkg
            and self.levelpaths_extended_gpkg.exists()
        )
        if use_levelpaths:
            LevelPathBooleanRasterizer(
                self.levelpaths_extended_gpkg, dem_branch, flows_bool
            ).run()
            log.info("Level path boolean grid --> %s", flows_bool.name)
        else:
            StreamBooleanRasterizer(self.streams_gpkg, dem_branch, flows_bool).run()
            log.info("Stream boolean grid --> %s", flows_bool.name)

        # rasterize headwater points if provided
        headwaters_bool = None
        if self.headwaters_gpkg and self.headwaters_gpkg.exists():
            headwaters_bool = branch_dir / f"headwaters_{bid}.tif"
            HeadwaterRasterizer(self.headwaters_gpkg, dem_branch, headwaters_bool).run()
            log.info("Headwaters boolean grid --> %s", headwaters_bool.name)

        # AGREE DEM conditioning
        dem_burned = branch_dir / f"dem_burned_{bid}.tif"
        HydroenforceDEM(
            rivers_raster=flows_bool,
            dem=dem_branch,
            output_raster=dem_burned,
            workspace=branch_dir,
            buffer_dist=self.agree_buffer_m,
            smooth_drop=self.agree_smooth_drop,
            sharp_drop=self.agree_sharp_drop,
            stream_value=self.stream_value,
            wbt_path=self.wbt_path,
            keep_intermediates=self.keep_agree_intermediates,
        ).run()
        log.info("AGREE DEM --> %s", dem_burned.name)

        # fill depressions
        dem_filled = branch_dir / f"dem_burned_filled_{bid}.tif"
        _fill_depressions(dem_burned, dem_filled, wbt_path=self.wbt_path)
        log.info("Pit-filled DEM --> %s", dem_filled.name)

        # D8 flow directions
        flowdir = branch_dir / f"flowdir_d8_burned_filled_{bid}.tif"
        FlowdirDEM(dem_filled, flowdir, wbt_path=self.wbt_path).run()
        log.info("D8 flow directions --> %s", flowdir.name)

        outputs = {
            "dem": dem_clipped,
            "dem_branch": dem_branch,
            "flows_grid_boolean": flows_bool,
            "dem_burned": dem_burned,
            "dem_burned_filled": dem_filled,
            "flowdir_d8": flowdir,
        }
        if bridge_clipped:
            outputs["bridge_elev_diff"] = bridge_clipped
        if bridge_branch:
            outputs["bridge_elev_diff_branch"] = bridge_branch
        if headwaters_bool:
            outputs["headwaters"] = headwaters_bool

        log.info("=== BRANCH ZERO COMPLETE ===")
        return outputs

    def _resolve_crs_and_res(self) -> tuple[str, float]:
        with rasterio.open(self.dem_path) as src:
            epsg = src.crs.to_epsg()
            crs = self.target_crs or (f"EPSG:{epsg}" if epsg else src.crs.to_wkt())
            res = self.resolution or src.res[0]
        return str(crs), float(res)


def _rasterio_clip_reproject(
    src: Path, boundary: Path, dst: Path, *, crs: str, res: float
) -> None:
    """Clip and reproject a raster to a polygon boundary using rasterio."""
    import geopandas as gpd

    src, dst = Path(src), Path(dst)

    # same source and destination — file is already at the right location, nothing to do
    if src.resolve() == dst.resolve():
        log.info("Clip skipped — already at destination: %s", dst.name)
        return

    target_crs = CRS.from_string(crs)

    # load boundary, reproject to target CRS, dissolve to single geometry
    gdf = gpd.read_file(str(boundary))
    if gdf.crs is not None and gdf.crs != target_crs:
        gdf = gdf.to_crs(target_crs)
    boundary_geom = gdf.geometry.unary_union

    # target-aligned pixel grid (matches gdalwarp -tap -tr)
    minx, miny, maxx, maxy = boundary_geom.bounds
    minx_tap = math.floor(minx / res) * res
    miny_tap = math.floor(miny / res) * res
    maxx_tap = math.ceil(maxx / res) * res
    maxy_tap = math.ceil(maxy / res) * res
    ncols = max(1, int(round((maxx_tap - minx_tap) / res)))
    nrows = max(1, int(round((maxy_tap - miny_tap) / res)))
    dst_transform = rasterio.transform.from_bounds(
        minx_tap, miny_tap, maxx_tap, maxy_tap, ncols, nrows
    )

    with rasterio.open(str(src)) as src_ds:
        nodata_val = float(src_ds.nodata) if src_ds.nodata is not None else -9999.0
        dst_arr = np.full((nrows, ncols), nodata_val, dtype=np.float32)
        warp_reproject(
            source=rasterio.band(src_ds, 1),
            destination=dst_arr,
            src_transform=src_ds.transform,
            src_crs=src_ds.crs,
            dst_transform=dst_transform,
            dst_crs=target_crs,
            resampling=Resampling.nearest,
            src_nodata=nodata_val,
            dst_nodata=nodata_val,
        )

    # mask pixels outside boundary polygon to nodata
    outside = geometry_mask(
        [boundary_geom],
        out_shape=(nrows, ncols),
        transform=dst_transform,
        invert=False,
    )
    dst_arr[outside] = nodata_val

    dst.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        str(dst),
        "w",
        driver="GTiff",
        dtype="float32",
        width=ncols,
        height=nrows,
        count=1,
        crs=target_crs,
        transform=dst_transform,
        nodata=nodata_val,
        compress="lzw",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        BIGTIFF="YES",
    ) as dst_ds:
        dst_ds.write(dst_arr, 1)

    log.debug("Clip written --> %s  (%dx%d  res=%.2fm)", dst.name, ncols, nrows, res)


def _fill_depressions(dem: Path, out: Path, wbt_path: Optional[str] = None) -> None:
    """Fill sinks and remove flat areas with WhiteboxTools.

    Processed in float64 so flat-routing increments stay representable at the
    burned-channel depth, with a fixed flat increment instead of WBT's default.
    A FillSingleCellPits pass precedes the fill: FillDepressions otherwise leaves
    isolated 1-cell pits, which become D8 sinks with no downslope neighbour.
    Afterwards the result is checked for any remaining interior pits (cells at
    the nodata boundary are legitimately unroutable and excluded).
    """
    import shutil
    import rasterio
    import numpy as np

    from ._wbt_safe import run_wbt_tool

    # Explicit flat-routing increment: safely above float64 ULP at -1010 m depth
    # while small enough to not lift flat interiors above surrounding terrain.
    flat_increment = 1e-7

    # Write a float64 copy so WBT's flat-routing increments are representable
    dem_f64 = dem.with_suffix(".f64_tmp.tif")
    pits_f64 = dem.with_suffix(".pits_tmp.tif")
    try:
        with rasterio.open(str(dem)) as src:
            prof64 = src.profile.copy()
            data = src.read(1)
        prof64.update(dtype="float64")
        with rasterio.open(str(dem_f64), "w", **prof64) as dst:
            dst.write(data.astype(np.float64), 1)

        # Remove isolated single-cell pits first, then fill depressions. Both
        # run via the concurrency-safe runner (no global chdir, verified output).
        run_wbt_tool(
            "FillSingleCellPits",
            [
                f"--dem={Path(dem_f64).resolve()}",
                f"--output={Path(pits_f64).resolve()}",
            ],
            out_path=Path(pits_f64),
            wbt_path=wbt_path,
        )
        run_wbt_tool(
            "FillDepressions",
            [
                f"--dem={Path(pits_f64).resolve()}",
                f"--output={Path(out).resolve()}",
                "--fix_flats",
                f"--flat_increment={flat_increment}",
            ],
            out_path=Path(out),
            wbt_path=wbt_path,
        )

        # Post-check: an interior cell whose 8 neighbours are all strictly higher
        # has no D8 descent. Cells touching nodata sit at the DEM boundary and
        # legitimately have nowhere to drain, so they are excluded.
        with rasterio.open(str(out)) as src:
            filled = src.read(1).astype(np.float64)
            nodata = src.nodata
        valid = np.ones(filled.shape, dtype=bool) if nodata is None else (filled != nodata)
        higher = np.ones(filled.shape, dtype=bool)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                shifted = np.full(filled.shape, -np.inf)
                rs = slice(max(dr, 0), filled.shape[0] + min(dr, 0))
                cs = slice(max(dc, 0), filled.shape[1] + min(dc, 0))
                rd = slice(max(-dr, 0), filled.shape[0] + min(-dr, 0))
                cd = slice(max(-dc, 0), filled.shape[1] + min(-dc, 0))
                shifted[rd, cd] = np.where(valid[rs, cs], filled[rs, cs], -np.inf)
                higher &= shifted > filled
        from scipy import ndimage  # noqa: PLC0415 — local import; optional dep

        nd_adj = ndimage.binary_dilation(~valid, iterations=1)
        pits = valid & higher & ~nd_adj
        n_pits = int(pits.sum())
        if n_pits > 0:
            log.warning(
                "FillDepressions left %d interior pit(s) in %s; "
                "their upstream cells will be unrouted in D8.",
                n_pits,
                out.name,
            )
    finally:
        if pits_f64.exists():
            pits_f64.unlink()
        if dem_f64.exists():
            dem_f64.unlink()

    # Recompress output with LZW; keep float64 so D8 sees the gradients
    tmp = out.with_suffix(".tmp.tif")
    try:
        with rasterio.open(str(out)) as src:
            profile = src.profile.copy()
            data = src.read(1)
        profile.update(
            compress="lzw",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="YES",
        )
        with rasterio.open(str(tmp), "w", **profile) as dst:
            dst.write(data, 1)
        shutil.move(str(tmp), str(out))
    except Exception:
        if tmp.exists():
            tmp.unlink()
