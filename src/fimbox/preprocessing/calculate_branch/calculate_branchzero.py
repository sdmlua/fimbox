"""
Author: Supath Dhital
Branch Zero preprocessing

Steps performed:
  1. Clip 3DEP DEM to HUC boundary  → dem_meters.tif
  2. Clip bridge_elev_diff raster   → bridge_elev_diff_meters.tif  (optional)
  3. Burn levees into DEM           → overwrites dem_meters_{id}.tif  (optional)
  4. Rasterize NWM streams          → flows_grid_boolean_{id}.tif
  5. AGREE DEM conditioning         → dem_burned_{id}.tif
  6. Fill depressions (pit removal) → dem_burned_filled_{id}.tif
  7. D8 flow directions             → flowdir_d8_burned_filled_{id}.tif
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np
import rasterio

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
    levee_raster_path     : 3D NLD levee raster with elevations (optional)
    target_crs            : EPSG string e.g. "EPSG:5070"; defaults to DEM CRS
    resolution            : output pixel size in metres; defaults to DEM resolution
    agree_buffer_m        : AGREE stream buffer distance in metres (default 15)
    agree_smooth_drop     : AGREE smooth drop in metres (default 10)
    agree_sharp_drop      : AGREE sharp drop in metres (default 1000)
    branch_zero_id        : suffix used in output filenames (default "0")
    """

    dem_path: Union[str, Path]
    streams_gpkg: Union[str, Path]
    boundary_gpkg: Union[str, Path]
    out_dir: Union[str, Path]
    bridge_elev_diff_path: Optional[Union[str, Path]] = None
    levee_raster_path: Optional[Union[str, Path]] = None
    target_crs: Optional[str] = None
    resolution: Optional[float] = None
    agree_buffer_m: float = 15.0
    agree_smooth_drop: float = 10.0
    agree_sharp_drop: float = 1000.0
    branch_zero_id: str = "0"

    def __post_init__(self):
        self.dem_path = Path(self.dem_path)
        self.streams_gpkg = Path(self.streams_gpkg)
        self.boundary_gpkg = Path(self.boundary_gpkg)
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.bridge_elev_diff_path:
            self.bridge_elev_diff_path = Path(self.bridge_elev_diff_path)
        if self.levee_raster_path:
            self.levee_raster_path = Path(self.levee_raster_path)

    def run(self) -> dict:
        """Run all Phase-2 steps. Returns dict of output Path objects."""
        log_path = self.out_dir / "preprocess.log"
        fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
        )
        log.setLevel(logging.DEBUG)
        log.addHandler(fh)
        try:
            log.info("=" * 60)
            log.info(
                "BranchZero START  out_dir=%s  branch_id=%s",
                self.out_dir,
                self.branch_zero_id,
            )
            log.info("  dem        : %s", self.dem_path)
            log.info("  streams    : %s", self.streams_gpkg)
            log.info("  boundary   : %s", self.boundary_gpkg)
            log.info("  bridge_diff: %s", self.bridge_elev_diff_path or "not provided")
            log.info("  levees     : %s", self.levee_raster_path or "not provided")
            log.info(
                "  AGREE      : buffer=%.0fm  smooth=%.0fm  sharp=%.0fm",
                self.agree_buffer_m,
                self.agree_smooth_drop,
                self.agree_sharp_drop,
            )
            result = self._run()
            log.info("BranchZero DONE — %d output files written", len(result))
            log.info("=" * 60)
            return result
        finally:
            log.removeHandler(fh)
            fh.close()

    def _run(self) -> dict:
        bid = self.branch_zero_id
        branch_dir = self.out_dir / "branches" / bid
        branch_dir.mkdir(parents=True, exist_ok=True)

        crs, res = self._resolve_crs_and_res()

        # clip DEM to HUC boundary once, then copy into branch subdirectory
        dem_clipped = self.out_dir / "dem_meters.tif"
        _gdalwarp_clip(self.dem_path, self.boundary_gpkg, dem_clipped, crs=crs, res=res)
        log.info("DEM clipped → %s", dem_clipped.name)

        dem_branch = branch_dir / f"dem_meters_{bid}.tif"
        shutil.copy2(dem_clipped, dem_branch)

        # clip bridge elev diff once, then copy into branch subdirectory
        bridge_clipped: Optional[Path] = None
        bridge_branch: Optional[Path] = None
        if self.bridge_elev_diff_path and self.bridge_elev_diff_path.exists():
            bridge_clipped = self.out_dir / "bridge_elev_diff_meters.tif"
            _gdalwarp_clip(
                self.bridge_elev_diff_path,
                self.boundary_gpkg,
                bridge_clipped,
                crs=crs,
                res=res,
            )
            log.info("Bridge elev diff clipped → %s", bridge_clipped.name)
            bridge_branch = branch_dir / f"bridge_elev_diff_meters_{bid}.tif"
            shutil.copy2(bridge_clipped, bridge_branch)

        # burn levees into DEM if provided
        if self.levee_raster_path and self.levee_raster_path.exists():
            _burn_levees(dem_branch, self.levee_raster_path, dem_branch)
            log.info("Levees burned into DEM")

        # read DEM extent for rasterization
        with rasterio.open(dem_branch) as src:
            ncols, nrows = src.width, src.height
            left, bottom, right, top = src.bounds

        # rasterize streams to boolean grid
        flows_bool = branch_dir / f"flows_grid_boolean_{bid}.tif"
        _rasterize_streams(
            self.streams_gpkg,
            flows_bool,
            crs=crs,
            xmin=left,
            ymin=bottom,
            xmax=right,
            ymax=top,
            ncols=ncols,
            nrows=nrows,
        )
        log.info("Stream boolean grid → %s", flows_bool.name)

        # AGREE DEM conditioning
        dem_burned = branch_dir / f"dem_burned_{bid}.tif"
        _agreedem(
            rivers_raster=flows_bool,
            dem=dem_branch,
            output_raster=dem_burned,
            workspace=branch_dir,
            buffer_dist=self.agree_buffer_m,
            smooth_drop=self.agree_smooth_drop,
            sharp_drop=self.agree_sharp_drop,
        )
        log.info("AGREE DEM → %s", dem_burned.name)

        # fill depressions
        dem_filled = branch_dir / f"dem_burned_filled_{bid}.tif"
        _fill_depressions(dem_burned, dem_filled)
        log.info("Pit-filled DEM → %s", dem_filled.name)

        # D8 flow directions
        flowdir = branch_dir / f"flowdir_d8_burned_filled_{bid}.tif"
        _d8_flow_dir(dem_filled, flowdir)
        log.info("D8 flow directions → %s", flowdir.name)

        outputs = {
            "dem_meters": dem_clipped,
            "dem_branch": dem_branch,
            "flows_grid_boolean": flows_bool,
            "dem_burned": dem_burned,
            "dem_burned_filled": dem_filled,
            "flowdir_d8": flowdir,
        }
        if bridge_clipped:
            outputs["bridge_elev_diff_meters"] = bridge_clipped
        if bridge_branch:
            outputs["bridge_elev_diff_branch"] = bridge_branch

        log.info("=== BRANCH ZERO COMPLETE ===")
        return outputs

    def _resolve_crs_and_res(self) -> tuple[str, float]:
        with rasterio.open(self.dem_path) as src:
            epsg = src.crs.to_epsg()
            crs = self.target_crs or (f"EPSG:{epsg}" if epsg else src.crs.to_wkt())
            res = self.resolution or src.res[0]
        return str(crs), float(res)


def _gdalwarp_clip(
    src: Path, boundary: Path, dst: Path, *, crs: str, res: float
) -> None:
    """Clip and reproject a raster to a polygon boundary."""
    subprocess.run(
        [
            "gdalwarp",
            "-cutline",
            str(boundary),
            "-crop_to_cutline",
            "-ot",
            "Float32",
            "-r",
            "near",
            "-of",
            "GTiff",
            "-overwrite",
            "-co",
            "BLOCKXSIZE=512",
            "-co",
            "BLOCKYSIZE=512",
            "-co",
            "TILED=YES",
            "-co",
            "COMPRESS=LZW",
            "-co",
            "BIGTIFF=YES",
            "-t_srs",
            crs,
            "-tr",
            str(res),
            str(res),
            "-tap",
            str(src),
            str(dst),
        ],
        check=True,
        capture_output=True,
    )


def _burn_levees(dem_path: Path, levee_path: Path, out_path: Path) -> None:
    """Raise DEM pixels to levee elevation where levee is present."""
    with rasterio.open(dem_path) as dem, rasterio.open(levee_path) as nld:
        dem_data = dem.read(1)
        nld_data = nld.read(1).astype(np.float32)
        nodata = nld.nodata
        nld_masked = np.where(nld_data == nodata, nodata, nld_data)
        burned = np.maximum(dem_data, nld_masked)
        profile = dem.profile.copy()
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(burned, 1)


def _rasterize_streams(
    gpkg: Path,
    out: Path,
    *,
    crs: str,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    ncols: int,
    nrows: int,
) -> None:
    """Rasterize stream lines to a binary 0/1 Int32 grid."""
    subprocess.run(
        [
            "gdal_rasterize",
            "-q",
            "-ot",
            "Int32",
            "-burn",
            "1",
            "-init",
            "0",
            "-co",
            "COMPRESS=LZW",
            "-co",
            "BIGTIFF=YES",
            "-co",
            "TILED=YES",
            "-a_srs",
            crs,
            "-te",
            str(xmin),
            str(ymin),
            str(xmax),
            str(ymax),
            "-ts",
            str(ncols),
            str(nrows),
            str(gpkg),
            str(out),
        ],
        check=True,
        capture_output=True,
    )


def _agreedem(
    rivers_raster: Path,
    dem: Path,
    output_raster: Path,
    workspace: Path,
    buffer_dist: float,
    smooth_drop: float,
    sharp_drop: float,
) -> None:
    """
    AGREE DEM hydrological conditioning (Hellweger 1997).
    Matches inundation-mapping agreedem.py logic exactly using windowed I/O.
    """
    import whitebox

    wbt = whitebox.WhiteboxTools()
    wbt.verbose = False

    ws = str(workspace)
    smo_out = os.path.join(ws, "agree_smogrid.tif")
    smo_zerod = os.path.join(ws, "agree_smogrid_zerod.tif")
    vectdist = os.path.join(ws, "agree_smogrid_dist.tif")
    vectallo = os.path.join(ws, "agree_smogrid_allo.tif")
    buf_out = os.path.join(ws, "agree_bufgrid.tif")
    buf_zerod = os.path.join(ws, "agree_bufgrid_zerod.tif")
    bin_buf = os.path.join(ws, "agree_binary_bufgrid.tif")
    bufdist = os.path.join(ws, "agree_bufgrid_dist.tif")
    bufallo = os.path.join(ws, "agree_bufgrid_allo.tif")

    with rasterio.open(str(dem)) as elev, rasterio.open(str(rivers_raster)) as rivers:
        dem_profile = elev.profile
        half_res = elev.res[0] / 2
        final_buffer = buffer_dist - half_res

        # smogrid: stream cells lowered by smooth_drop, non-stream cells = 0 (nodata)
        smo_profile = dem_profile.copy()
        smo_profile.update(nodata=0, dtype="float32")
        smooth_dist = -1 * smooth_drop

        smogrid_valid = False
        with rasterio.open(smo_out, "w", **smo_profile) as raster:
            for ji, window in elev.block_windows(1):
                elev_data = elev.read(1, window=window)
                elev_mask = elev.read_masks(1, window=window).astype(bool)
                river_raw = rivers.read(1, window=window)
                river_data = np.where(elev_mask, river_raw, 0)
                smogrid_window = river_data * (elev_data + smooth_dist)
                raster.write(smogrid_window.astype("float32"), indexes=1, window=window)
                if len(smogrid_window[smogrid_window != 0]) > 0:
                    smogrid_valid = True

        if not smogrid_valid:
            raise RuntimeError(
                "No stream cells found overlapping the DEM extent. "
                "Check that streams CRS matches the DEM CRS. "
                f"dem={dem}, rivers={rivers_raster}"
            )

        # euclidean distance and allocation from stream pixels
        wbt.euclidean_distance(str(rivers_raster), vectdist)
        wbt.convert_nodata_to_zero(smo_out, smo_zerod)
        wbt.euclidean_allocation(smo_zerod, vectallo)

        # bufgrid: DEM elevation outside buffer zone, nodata inside
        buf_profile = dem_profile.copy()
        buf_profile.update(dtype="float32")

        with rasterio.open(vectdist) as vd:
            with rasterio.open(buf_out, "w", **buf_profile) as raster:
                for ji, window in elev.block_windows(1):
                    vd_data = vd.read(1, window=window)
                    elev_data = elev.read(1, window=window)
                    bufgrid = np.where(
                        vd_data > final_buffer, elev_data, dem_profile["nodata"]
                    )
                    raster.write(bufgrid.astype("float32"), indexes=1, window=window)

        # binary buffer grid: 1 where bufgrid has valid data, 0 elsewhere
        with rasterio.open(buf_out) as agree_bufgrid:
            bin_profile = agree_bufgrid.profile.copy()
            bin_profile.update(dtype="float32")
            with rasterio.open(bin_buf, "w", **bin_profile) as raster:
                for ji, window in agree_bufgrid.block_windows(1):
                    data = agree_bufgrid.read(1, window=window)
                    data = np.where(data > -10000, 1.0, 0.0)
                    raster.write(data.astype("float32"), indexes=1, window=window)

        # euclidean distance and allocation from buffer edge
        wbt.euclidean_distance(bin_buf, bufdist)
        wbt.convert_nodata_to_zero(buf_out, buf_zerod)
        wbt.euclidean_allocation(buf_zerod, bufallo)

        # compute final AGREE DEM using windowed I/O
        agree_profile = dem_profile.copy()
        agree_profile.update(dtype="float32")

        with (
            rasterio.open(vectdist) as vd,
            rasterio.open(vectallo) as va,
            rasterio.open(bufdist) as bd,
            rasterio.open(bufallo) as ba,
        ):
            with rasterio.open(str(output_raster), "w", **agree_profile) as raster:
                for ji, window in elev.block_windows(1):
                    elev_data = elev.read(1, window=window)
                    elev_mask = elev.read_masks(1, window=window).astype(bool)
                    vd_data = vd.read(1, window=window)
                    va_data = va.read(1, window=window)
                    bd_data = bd.read(1, window=window)
                    ba_data = ba.read(1, window=window)
                    river_raw = rivers.read(1, window=window).astype(np.float32)

                    ba_data = np.where(ba_data == -32768.0, elev_data, ba_data)
                    va_data = np.where(va_data == -32768.0, elev_data - 10, va_data)
                    river_data = np.where(elev_mask, river_raw, -20.0)

                    smoelev = (
                        va_data + ((ba_data - va_data) / (bd_data + vd_data)) * vd_data
                    )
                    shagrid = (smoelev + (-1 * sharp_drop)) * river_data
                    elevgrid = np.where(river_data == 0, smoelev, shagrid)
                    agree_dem = np.where(elev_mask, elevgrid, dem_profile["nodata"])

                    raster.write(agree_dem.astype("float32"), indexes=1, window=window)

    # clean up intermediates
    for f in [
        smo_out,
        smo_zerod,
        vectdist,
        vectallo,
        buf_out,
        buf_zerod,
        bin_buf,
        bufdist,
        bufallo,
    ]:
        try:
            os.remove(f)
        except FileNotFoundError:
            pass


# fill depressions in the burned DEM
def _fill_depressions(dem: Path, out: Path) -> None:
    import whitebox

    wbt = whitebox.WhiteboxTools()
    wbt.verbose = False
    wbt.fill_depressions(str(dem), str(out), fix_flats=True)


# D8 flow directions
def _d8_flow_dir(dem: Path, out: Path) -> None:
    import whitebox

    wbt = whitebox.WhiteboxTools()
    wbt.verbose = False
    wbt.d8_pointer(str(dem), str(out))
