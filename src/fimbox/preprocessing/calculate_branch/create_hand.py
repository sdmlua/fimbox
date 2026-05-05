"""
Author: Supath Dhital
Date updated : May 2026

--------
HAND (Height Above Nearest Drainage) production pipeline.

Geoprocessing steps for one branch:
-------------------------------
levee mask DEM         -> levee_rasterize.mask_levee_dem()
D8 flow accumulation   -> flowacc_dem.FlowAccDEM
thalweg adjustment     -> thalweg_adjustment.ThalwegAdjustment
D8 slopes              -> flowdir_dem.D8SlopeDEM
streamnet              -> streamnet_reaches.StreamNetReaches
split reaches          -> split_reaches.split_derived_reaches

Required inputs
---------------
aoi_dir   : area output directory
branch_dir: branch output sub-dir
branch_id : "0" for branch-zero, otherwise level-path ID string

Auto-resolved from aoi_dir / branch_dir when not passed
--------------------------------------------------------
dem_path        : branch_dir/dem_{id}.tif
flowdir_path    : branch_dir/flowdir_d8_burned_filled_{id}.tif
headwaters_path : branch_dir/headwaters_{id}.tif
streams_gpkg    : aoi_dir/nwm_subset_streams.gpkg
catchments_gpkg : aoi_dir/nwm_catchments_proj_subset.gpkg

Optional inputs (skipped when absent)
--------------------------------------
levee_protected_areas_gpkg : LeveeProtectedAreas_subset.gpkg
levee_levelpaths_csv       : levee_levelpaths.csv
lakes_gpkg                 : nwm_lakes_proj_subset.gpkg
wbd8_clp_gpkg              : wbd8_clp.gpkg

Parameters
-------------------------------------------------------------------------
cost_distance_tolerance     : 50.0 m
lateral_elevation_threshold : 3    m
max_split_distance_m        : 2000 m
slope_min                   : 0.0001
lakes_buffer_dist_m         : 100  m
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class CreateHAND:
    """
    Orchestrate Phase-3 HAND preprocessing for one branch.

    Call .run() to execute all steps in sequence.  Returns a dict of output
    Path objects keyed by descriptive names.  Steps whose outputs already
    exist on disk are automatically skipped.
    """

    # required: directory + identity
    aoi_dir: Path
    branch_dir: Path
    branch_id: str

    # paths default to standard names derived from aoi_dir / branch_dir / branch_id
    dem_path: Optional[Path] = None            # dem_{id}.tif
    flowdir_path: Optional[Path] = None        # flowdir_d8_burned_filled_{id}.tif
    headwaters_path: Optional[Path] = None     # headwaters_{id}.tif
    streams_gpkg: Optional[Path] = None        # nwm_subset_streams.gpkg
    catchments_gpkg: Optional[Path] = None     # nwm_catchments_proj_subset.gpkg

    # optional files (skipped when absent)
    levee_protected_areas_gpkg: Optional[Path] = None
    levee_levelpaths_csv: Optional[Path] = None
    lakes_gpkg: Optional[Path] = None
    wbd8_clp_gpkg: Optional[Path] = None

    # tuning parameters
    cost_distance_tolerance: float = 50.0
    lateral_elevation_threshold: int = 3
    max_split_distance_m: float = 2000.0
    slope_min: float = 0.0001
    lakes_buffer_dist_m: float = 100.0
    wbt_path: Optional[str] = None

    def __post_init__(self):
        self.aoi_dir = Path(self.aoi_dir)
        self.branch_dir = Path(self.branch_dir)
        bid = self.branch_id

        if self.dem_path is None:
            self.dem_path = self.branch_dir / f"dem_{bid}.tif"
        if self.flowdir_path is None:
            self.flowdir_path = self.branch_dir / f"flowdir_d8_burned_filled_{bid}.tif"
        if self.headwaters_path is None:
            self.headwaters_path = self.branch_dir / f"headwaters_{bid}.tif"
        if self.streams_gpkg is None:
            self.streams_gpkg = self.aoi_dir / "nwm_subset_streams.gpkg"
        if self.catchments_gpkg is None:
            self.catchments_gpkg = self.aoi_dir / "nwm_catchments_proj_subset.gpkg"

        for attr in ("dem_path", "flowdir_path", "headwaters_path",
                     "streams_gpkg", "catchments_gpkg"):
            setattr(self, attr, Path(getattr(self, attr)))
        for attr in ("levee_protected_areas_gpkg", "levee_levelpaths_csv",
                     "lakes_gpkg", "wbd8_clp_gpkg"):
            val = getattr(self, attr)
            if val is not None:
                setattr(self, attr, Path(val))
        self.branch_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict[str, Path]:
        """Execute all HAND preprocessing steps and return output paths."""
        log_path = self.aoi_dir / "preprocess.log"
        fh = logging.FileHandler(str(log_path), mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        fimbox_log = logging.getLogger("fimbox")
        fimbox_log.setLevel(logging.DEBUG)
        fimbox_log.addHandler(fh)

        try:
            log.info("=" * 60)
            log.info(
                "CreateHAND START  branch_id=%s  branch_dir=%s",
                self.branch_id, self.branch_dir,
            )
            result = self._run()
            log.info("CreateHAND DONE -- %d outputs", len(result))
            log.info("=" * 60)
            return result
        except Exception:
            log.exception("CreateHAND FAILED for branch %s", self.branch_id)
            raise
        finally:
            fimbox_log.removeHandler(fh)
            fh.close()

    def _run(self) -> dict[str, Path]:
        from .flowacc_dem import FlowAccDEM
        from .flowdir_dem import D8SlopeDEM
        from .levee_rasterize import mask_levee_dem
        from .split_reaches import split_derived_reaches
        from .streamnet_reaches import StreamNetReaches
        from .thalweg_adjustment import ThalwegAdjustment

        bid = self.branch_id
        bd  = self.branch_dir
        outputs: dict[str, Path] = {}
        _TOTAL = 6

        def _progress(n: int, label: str, skipped: bool = False) -> None:
            tag = "skip" if skipped else "run "
            print(f"  [{n}/{_TOTAL}] {tag}  {label}", flush=True)

        # 1. Levee mask
        _progress(1, "levee mask")
        dem_working = bd / f"dem_{bid}.tif"
        _exists_warn(self.dem_path, "dem_path")
        if not dem_working.exists():
            import shutil
            shutil.copy2(self.dem_path, dem_working)
        if self.levee_protected_areas_gpkg and self.levee_protected_areas_gpkg.exists():
            log.info("Masking levee-protected areas from DEM")
            mask_levee_dem(
                dem_path=dem_working,
                nld_path=self.levee_protected_areas_gpkg,
                catchments_path=self.catchments_gpkg,
                out_path=dem_working,
                branch_id=int(bid) if bid.isdigit() else 0,
                branch_zero_id=0,
                levee_levelpaths_csv=self.levee_levelpaths_csv,
            )
        outputs["dem"] = dem_working

        # 2. Flow accumulation
        flowaccum     = bd / f"flowaccum_d8_burned_filled_{bid}.tif"
        stream_pixels = bd / f"demDerived_streamPixels_{bid}.tif"
        if flowaccum.exists() and stream_pixels.exists():
            _progress(2, "flow accumulation", skipped=True)
            log.info("Flow accumulation: outputs exist, skipping")
            outputs["flowaccum"]     = flowaccum
            outputs["stream_pixels"] = stream_pixels
        elif self.headwaters_path and self.headwaters_path.exists():
            _progress(2, "flow accumulation")
            log.info("D8 flow accumulation")
            FlowAccDEM(
                flowdir=self.flowdir_path,
                headwaters=self.headwaters_path,
                out_flowaccum=flowaccum,
                out_stream_pixels=stream_pixels,
            ).run()
            outputs["flowaccum"]     = flowaccum
            outputs["stream_pixels"] = stream_pixels
        else:
            log.warning("headwaters raster not found -- skipping flow accumulation")

        # 3. Thalweg adjustment
        thalweg_adj    = bd / f"dem_lateral_thalweg_adj_{bid}.tif"
        flowdir_streams = bd / f"flowdir_d8_burned_filled_flows_{bid}.tif"
        thalweg_cond   = bd / f"dem_thalwegCond_{bid}.tif"
        if thalweg_adj.exists() and flowdir_streams.exists() and thalweg_cond.exists():
            _progress(3, "thalweg adjustment", skipped=True)
            log.info("Thalweg adjustment: outputs exist, skipping")
            outputs["thalweg_adj"]     = thalweg_adj
            outputs["flowdir_streams"] = flowdir_streams
            outputs["thalweg_cond"]    = thalweg_cond
        elif stream_pixels.exists():
            _progress(3, "thalweg adjustment")
            log.info("Thalweg adjustment + flow conditioning")
            ThalwegAdjustment(
                dem=dem_working,
                stream_pixels=stream_pixels,
                flowdir=self.flowdir_path,
                out_thalweg_adj=thalweg_adj,
                out_flowdir_streams=flowdir_streams,
                out_thalweg_cond=thalweg_cond,
                cost_distance_tolerance=self.cost_distance_tolerance,
                lateral_elevation_threshold=self.lateral_elevation_threshold,
                wbt_path=self.wbt_path,
            ).run()
            outputs["thalweg_adj"]     = thalweg_adj
            outputs["flowdir_streams"] = flowdir_streams
            outputs["thalweg_cond"]    = thalweg_cond
        else:
            log.warning("stream_pixels not found -- skipping thalweg adjustment")

        # 4. D8 slopes
        slopes_d8 = bd / f"slopes_d8_dem_{bid}.tif"
        if slopes_d8.exists():
            _progress(4, "D8 slopes", skipped=True)
            log.info("D8 slopes: output exists, skipping")
            outputs["slopes_d8"] = slopes_d8
        elif thalweg_adj.exists():
            _progress(4, "D8 slopes")
            log.info("D8 slopes")
            D8SlopeDEM(
                dem=thalweg_adj,
                flowdir=self.flowdir_path,
                out_path=slopes_d8,
                slope_min=self.slope_min,
            ).run()
            outputs["slopes_d8"] = slopes_d8
        else:
            log.warning("thalweg_adj not found -- skipping D8 slopes")

        # 5. Stream network
        reaches_gpkg  = bd / f"demDerived_reaches_{bid}.gpkg"
        stream_order  = bd / f"streamOrder_{bid}.tif"
        sn_catchments = bd / f"sn_catchments_reaches_{bid}.tif"
        if stream_order.exists() and sn_catchments.exists() and reaches_gpkg.exists():
            _progress(5, "stream network", skipped=True)
            log.info("Stream network: outputs exist, skipping")
            outputs.update({
                "stream_order": stream_order,
                "sn_catchments_reaches": sn_catchments,
                "demDerived_reaches": reaches_gpkg,
            })
        elif stream_pixels.exists() and thalweg_cond.exists() and flowaccum.exists():
            _progress(5, "stream network")
            log.info("Stream network delineation")
            sn_out = StreamNetReaches(
                flowdir=self.flowdir_path,
                dem_thalweg_cond=thalweg_cond,
                flowaccum=flowaccum,
                stream_pixels=stream_pixels,
                out_dir=bd,
                branch_id=bid,
                wbt_path=self.wbt_path,
            ).run()
            outputs.update(sn_out)
        else:
            log.warning("Skipping streamnet -- missing stream_pixels / thalweg_cond / flowaccum")

        # 6. Split reaches
        out_split = bd / f"demDerived_reaches_split_{bid}.gpkg"
        out_pts   = bd / f"demDerived_reaches_split_points_{bid}.gpkg"
        if out_split.exists() and out_pts.exists():
            _progress(6, "split reaches", skipped=True)
            log.info("Split reaches: outputs exist, skipping")
            outputs["split_reaches"] = out_split
            outputs["split_points"]  = out_pts
        elif reaches_gpkg.exists() and thalweg_cond.exists():
            _progress(6, "split reaches")
            log.info("Split derived reaches")
            split_derived_reaches(
                reaches_gpkg=reaches_gpkg,
                dem_thalweg_cond=thalweg_cond,
                nwm_streams_gpkg=self.streams_gpkg,
                out_split_gpkg=out_split,
                out_points_gpkg=out_pts,
                wbd8_clp_gpkg=self.wbd8_clp_gpkg,
                lakes_gpkg=(
                    self.lakes_gpkg
                    if (self.lakes_gpkg and self.lakes_gpkg.exists())
                    else None
                ),
                max_length=self.max_split_distance_m,
                slope_min=self.slope_min,
                lakes_buffer_dist=self.lakes_buffer_dist_m,
            )
            outputs["split_reaches"] = out_split
            outputs["split_points"]  = out_pts
        else:
            log.warning("Skipping split_reaches -- missing demDerived_reaches or thalweg_cond")

        log.info("CreateHAND outputs: %s", list(outputs.keys()))
        return outputs


def _exists_warn(path: Optional[Path], name: str) -> None:
    if path is None or not path.exists():
        log.warning("Expected file not found: %s = %s", name, path)
