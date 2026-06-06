"""
Author: Supath Dhital
Date updated : May 2026

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
dem_path        : branch_dir/dem_{id}.tif
flowdir_path    : branch_dir/flowdir_d8_burned_filled_{id}.tif
headwaters_path : branch_dir/headwaters_{id}.tif
streams_gpkg    : aoi_dir/nwm_subset_streams.gpkg
catchments_gpkg : aoi_dir/nwm_catchments_proj_subset.gpkg

Optional inputs (skipped- when-absent)
--------------------------------------
levee_protected_areas_gpkg : LeveeProtectedAreas_subset.gpkg
levee_levelpaths_csv       : levee_levelpaths.csv
lakes_gpkg                 : nwm_lakes_proj_subset.gpkg
wbd8_clp_gpkg              : wbd8_clp.gpkg

Parameters
-------------------------------------------------------------------------
cost_distance_tolerance     : 50.0 m
lateral_elevation_threshold : 10   m
max_split_distance_m        : 1500 m
slope_min                   : 0.0001
lakes_buffer_dist_m         : 100  m
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..source_naming import resolve_source

log = logging.getLogger(__name__)


@dataclass
class CreateHAND:
    aoi_dir: Path
    branch_dir: Path
    branch_id: str

    # paths
    dem_path: Optional[Path] = None  # dem_{id}.tif
    flowdir_path: Optional[Path] = None  # flowdir_d8_burned_filled_{id}.tif
    headwaters_path: Optional[Path] = None  # headwaters_{id}.tif
    streams_gpkg: Optional[Path] = None  # nwm_subset_streams.gpkg
    catchments_gpkg: Optional[Path] = None  # nwm_catchments_proj_subset.gpkg

    # optional files
    levee_protected_areas_gpkg: Optional[Path] = None
    levee_levelpaths_csv: Optional[Path] = None
    lakes_gpkg: Optional[Path] = None
    boundary_gpkg: Optional[Path] = None  # AOI boundary file (e.g. wbd8_clp.gpkg)
    osm_bridges_gpkg: Optional[Path] = None  # osm_bridges_subset.gpkg
    osm_roads_gpkg: Optional[Path] = None  # osm_roads_subset.gpkg
    bridge_diff_raster: Optional[Path] = None  # bridge_elev_diff_meters_{id}.tif

    # AOI identifier. When unset, derived from the aoi_dir name as a fallback.
    aoi_code: Optional[str] = None

    # tuning parameters
    cost_distance_tolerance: float = 50.0
    lateral_elevation_threshold: int = 10
    max_split_distance_m: float = 1500.0
    slope_min: float = 0.0001
    lakes_buffer_dist_m: float = 100.0
    wbt_path: Optional[str] = None

    # SRC / crosswalk parameters
    mannings_n: float = 0.06
    stage_min_m: float = 0.0
    stage_interval_m: float = 0.3048
    stage_max_m: float = 25.2984
    min_catchment_area: float = 0.25
    min_stream_length: float = 0.5
    crosswalk_max_distance_m: float = 100.0

    # back-compat alias — older callers pass ``wbd8_clp_gpkg``. Resolved to
    # ``boundary_gpkg`` in __post_init__ if explicitly set.
    wbd8_clp_gpkg: Optional[Path] = None

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
            # resolve by stable suffix so a custom identifier prefix is honored
            self.streams_gpkg = resolve_source(self.aoi_dir, "streams")
        if self.catchments_gpkg is None:
            self.catchments_gpkg = resolve_source(self.aoi_dir, "catchments")

        for attr in (
            "dem_path",
            "flowdir_path",
            "headwaters_path",
            "streams_gpkg",
            "catchments_gpkg",
        ):
            setattr(self, attr, Path(getattr(self, attr)))
        # Resolve back-compat alias: if a caller still passes ``wbd8_clp_gpkg``
        # but not ``boundary_gpkg``, lift the value to the generic field.
        if self.boundary_gpkg is None and self.wbd8_clp_gpkg is not None:
            self.boundary_gpkg = self.wbd8_clp_gpkg
        for attr in (
            "levee_protected_areas_gpkg",
            "levee_levelpaths_csv",
            "lakes_gpkg",
            "boundary_gpkg",
            "wbd8_clp_gpkg",
            "osm_bridges_gpkg",
            "osm_roads_gpkg",
            "bridge_diff_raster",
        ):
            val = getattr(self, attr)
            if val is not None:
                setattr(self, attr, Path(val))
        # Defaults for the OSM helper inputs: they live next to the AOI inputs.
        if self.osm_bridges_gpkg is None:
            cand = self.aoi_dir / "osm_bridges_subset.gpkg"
            self.osm_bridges_gpkg = cand if cand.exists() else None
        if self.osm_roads_gpkg is None:
            cand = self.aoi_dir / "osm_roads_subset.gpkg"
            self.osm_roads_gpkg = cand if cand.exists() else None
        if self.bridge_diff_raster is None:
            cand = self.branch_dir / f"bridge_elev_diff_meters_{bid}.tif"
            self.bridge_diff_raster = cand if cand.exists() else None
        self.branch_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict[str, Path]:
        """Execute all HAND preprocessing steps and return output paths."""
        from ...logging_utils import attach_case_log

        attach_case_log(self.aoi_dir)
        try:
            log.info(f"--- CreateHAND: branch_id={self.branch_id} ---")
            log.info(f"branch_dir: {self.branch_dir}")
            result = self._run()
            log.info(f"CreateHAND complete: {len(result)} outputs")
            return result
        except Exception:
            log.exception(f"CreateHAND failed for branch {self.branch_id}")
            raise

    def _run(self) -> dict[str, Path]:
        from ..._skip_if_valid import should_skip
        from .add_crosswalk import add_crosswalk, NoCrosswalkError
        from .build_src import build_src_base
        from .filter_catchments import FilterCatchments, NoFlowlinesError
        from .flowacc_dem import FlowAccDEM
        from .flowdir_dem import D8SlopeDEM
        from .gage_catchments import (
            GageCatchments,
            OutletBackpoolMitigate,
            stream_pixel_points,
        )
        from .heal_bridges_osm import heal_bridges_osm
        from .levee_rasterize import mask_levee_dem
        from .make_rem import MakeREM
        from .mask_to_catchments import mask_slopes_to_catchments, rem_zeroed_masked
        from .process_roads_fimpact import process_roads_fimpact
        from .split_reaches import split_derived_reaches
        from .stages_catchlist import make_stages_and_catchlist
        from .streamnet_reaches import StreamNetReaches
        from .thalweg_adjustment import ThalwegAdjustment

        bid = self.branch_id
        bd = self.branch_dir
        outputs: dict[str, Path] = {}
        _TOTAL = 22

        def _progress(n: int, label: str, skipped: bool = False) -> None:
            # Per-step progress goes to the case log via the shared logger,
            # not to stdout (keeps the log readable and consistent).
            tag = "SKIP" if skipped else "RUN "
            log.info(f"[{n}/{_TOTAL}] {tag} {label}")

        # Levee mask
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

        # Flow accumulation
        flowaccum = bd / f"flowaccum_d8_burned_filled_{bid}.tif"
        stream_pixels = bd / f"demDerived_streamPixels_{bid}.tif"
        if should_skip(flowaccum, stream_pixels):
            _progress(2, "flow accumulation", skipped=True)
            log.info("Flow accumulation: outputs exist, skipping")
            outputs["flowaccum"] = flowaccum
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
            outputs["flowaccum"] = flowaccum
            outputs["stream_pixels"] = stream_pixels
        else:
            log.warning("headwaters raster not found -- skipping flow accumulation")

        # Thalweg adjustment
        thalweg_adj = bd / f"dem_lateral_thalweg_adj_{bid}.tif"
        flowdir_streams = bd / f"flowdir_d8_burned_filled_flows_{bid}.tif"
        thalweg_cond = bd / f"dem_thalwegCond_{bid}.tif"
        if should_skip(thalweg_adj, flowdir_streams, thalweg_cond):
            _progress(3, "thalweg adjustment", skipped=True)
            log.info("Thalweg adjustment: outputs exist, skipping")
            outputs["thalweg_adj"] = thalweg_adj
            outputs["flowdir_streams"] = flowdir_streams
            outputs["thalweg_cond"] = thalweg_cond
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
            outputs["thalweg_adj"] = thalweg_adj
            outputs["flowdir_streams"] = flowdir_streams
            outputs["thalweg_cond"] = thalweg_cond
        else:
            log.warning("stream_pixels not found -- skipping thalweg adjustment")

        # D8 slopes
        slopes_d8 = bd / f"slopes_d8_dem_{bid}.tif"
        if should_skip(slopes_d8):
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

        # Stream network
        reaches_gpkg = bd / f"demDerived_reaches_{bid}.gpkg"
        stream_order = bd / f"streamOrder_{bid}.tif"
        sn_catchments = bd / f"sn_catchments_reaches_{bid}.tif"
        if should_skip(stream_order, sn_catchments, reaches_gpkg):
            _progress(5, "stream network", skipped=True)
            log.info("Stream network: outputs exist, skipping")
            outputs.update(
                {
                    "stream_order": stream_order,
                    "sn_catchments_reaches": sn_catchments,
                    "demDerived_reaches": reaches_gpkg,
                }
            )
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
            log.warning(
                "Skipping streamnet -- missing stream_pixels / thalweg_cond / flowaccum"
            )

        # Split reaches
        out_split = bd / f"demDerived_reaches_split_{bid}.gpkg"
        out_pts = bd / f"demDerived_reaches_split_points_{bid}.gpkg"
        if should_skip(out_split, out_pts):
            _progress(6, "split reaches", skipped=True)
            log.info("Split reaches: outputs exist, skipping")
            outputs["split_reaches"] = out_split
            outputs["split_points"] = out_pts
        elif reaches_gpkg.exists() and thalweg_cond.exists():
            _progress(6, "split reaches")
            log.info("Split derived reaches")
            split_derived_reaches(
                reaches_gpkg=reaches_gpkg,
                dem_thalweg_cond=thalweg_cond,
                nwm_streams_gpkg=self.streams_gpkg,
                out_split_gpkg=out_split,
                out_points_gpkg=out_pts,
                wbd8_clp_gpkg=self.boundary_gpkg,
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
            outputs["split_points"] = out_pts
        else:
            log.warning(
                "Skipping split_reaches -- missing demDerived_reaches or thalweg_cond"
            )

        # Gage watershed for reaches
        gw_reaches = bd / f"gw_catchments_reaches_{bid}.tif"
        split_pts_gpkg = bd / f"demDerived_reaches_split_points_{bid}.gpkg"
        if should_skip(gw_reaches):
            _progress(7, "gage watershed (reaches)", skipped=True)
            log.info("Gage watershed reaches: output exists, skipping")
            outputs["gw_catchments_reaches"] = gw_reaches
        elif split_pts_gpkg.exists():
            _progress(7, "gage watershed (reaches)")
            log.info("Gage watershed for reaches")
            # Declutter only the reach catchments (they get polygonized); pixel
            # catchments below keep the default off.
            GageCatchments(
                flowdir=self.flowdir_path,
                outlet_points=split_pts_gpkg,
                out_path=gw_reaches,
                declutter=True,
            ).run()
            outputs["gw_catchments_reaches"] = gw_reaches
        else:
            log.warning("Skipping gage watershed reaches -- missing split points")

        # Vectorize stream pixel centroids
        pixel_pts_gpkg = bd / f"flows_points_pixels_{bid}.gpkg"
        if should_skip(pixel_pts_gpkg):
            _progress(8, "stream pixel points", skipped=True)
            log.info("Stream pixel points: output exists, skipping")
            outputs["flows_points_pixels"] = pixel_pts_gpkg
        elif stream_pixels.exists():
            _progress(8, "stream pixel points")
            log.info("Vectorize stream pixel centroids")
            stream_pixel_points(stream_pixels=stream_pixels, out_gpkg=pixel_pts_gpkg)
            outputs["flows_points_pixels"] = pixel_pts_gpkg
        else:
            log.warning("Skipping stream pixel points -- missing stream_pixels")

        # Gage watershed for pixels
        gw_pixels = bd / f"gw_catchments_pixels_{bid}.tif"
        if should_skip(gw_pixels):
            _progress(9, "gage watershed (pixels)", skipped=True)
            log.info("Gage watershed pixels: output exists, skipping")
            outputs["gw_catchments_pixels"] = gw_pixels
        elif pixel_pts_gpkg.exists():
            _progress(9, "gage watershed (pixels)")
            log.info("Gage watershed for pixels")
            # No declutter: pixel catchments are legitimately single-cell.
            GageCatchments(
                flowdir=self.flowdir_path,
                outlet_points=pixel_pts_gpkg,
                out_path=gw_pixels,
                declutter=False,
            ).run()
            outputs["gw_catchments_pixels"] = gw_pixels
        else:
            log.warning("Skipping gage watershed pixels -- missing pixel points")

        # Outlet backpool mitigation
        _progress(10, "outlet backpool mitigation")
        if out_split.exists() and gw_pixels.exists() and gw_reaches.exists():
            log.info("Outlet backpool mitigation")
            OutletBackpoolMitigate(
                branch_dir=bd,
                catchment_pixels_path=gw_pixels,
                catchment_reaches_path=gw_reaches,
                split_flows_gpkg=out_split,
                split_points_gpkg=split_pts_gpkg,
                nwm_streams_gpkg=self.streams_gpkg,
                dem_path=thalweg_cond,
                slope_min=self.slope_min,
            ).run()
        else:
            log.warning("Skipping backpool mitigation -- missing required inputs")

        # REM (Height Above Nearest Drainage)
        rem_path = bd / f"rem_{bid}.tif"
        if should_skip(rem_path):
            _progress(11, "REM", skipped=True)
            log.info("REM: output exists, skipping")
            outputs["rem"] = rem_path
        elif thalweg_cond.exists() and gw_pixels.exists() and stream_pixels.exists():
            _progress(11, "REM")
            log.info("REM computation")
            MakeREM(
                dem_thalweg_cond=thalweg_cond,
                gw_catchments_pixels=gw_pixels,
                stream_pixels=stream_pixels,
                out_rem=rem_path,
            ).run()
            outputs["rem"] = rem_path
        else:
            log.warning(
                "Skipping REM -- missing thalweg_cond / gw_catchments_pixels / stream_pixels"
            )

        # Zero/mask REM  (REM * (REM>=0) * (gw_catchments_reaches>0))
        rem_zeroed = bd / f"rem_zeroed_masked_{bid}.tif"
        if should_skip(rem_zeroed):
            _progress(12, "REM zeroed+masked", skipped=True)
            log.info("REM zeroed+masked: output exists, skipping")
            outputs["rem_zeroed_masked"] = rem_zeroed
        elif rem_path.exists() and gw_reaches.exists():
            _progress(12, "REM zeroed+masked")
            log.info("REM zero/mask")
            rem_zeroed_masked(rem_path, gw_reaches, rem_zeroed)
            outputs["rem_zeroed_masked"] = rem_zeroed
        else:
            log.warning(
                "Skipping REM zeroed+masked -- missing rem / gw_catchments_reaches"
            )

        # Polygonize gw_catchments_reaches raster --> GeoPackage
        catch_poly_gpkg = bd / f"gw_catchments_reaches_{bid}.gpkg"
        if should_skip(catch_poly_gpkg):
            _progress(13, "polygonize catchments", skipped=True)
            log.info("Polygonize catchments: output exists, skipping")
            outputs["gw_catchments_reaches_gpkg"] = catch_poly_gpkg
        elif gw_reaches.exists():
            _progress(13, "polygonize catchments")
            log.info("Polygonize gw_catchments_reaches")
            _polygonize_catchments(gw_reaches, catch_poly_gpkg)
            outputs["gw_catchments_reaches_gpkg"] = catch_poly_gpkg
        else:
            log.warning("Skipping polygonize -- missing gw_catchments_reaches")

        # Filter catchments + add flow attributes
        filtered_catchments = (
            bd / f"gw_catchments_reaches_filtered_addedAttributes_{bid}.gpkg"
        )
        filtered_flows = bd / f"demDerived_reaches_split_filtered_{bid}.gpkg"
        if should_skip(filtered_catchments, filtered_flows):
            _progress(14, "filter catchments", skipped=True)
            log.info("Filter catchments: outputs exist, skipping")
            outputs["filtered_catchments"] = filtered_catchments
            outputs["filtered_flows"] = filtered_flows
        elif catch_poly_gpkg.exists() and out_split.exists():
            _progress(14, "filter catchments")
            log.info("Filter catchments and add attributes")
            try:
                FilterCatchments(
                    catchments_gpkg=catch_poly_gpkg,
                    flows_gpkg=out_split,
                    out_catchments=filtered_catchments,
                    out_flows=filtered_flows,
                    aoi_code=self._resolve_aoi_code(),
                    boundary_gpkg=self.boundary_gpkg,
                ).run()
                outputs["filtered_catchments"] = filtered_catchments
                outputs["filtered_flows"] = filtered_flows
            except NoFlowlinesError as exc:
                log.warning("FilterCatchments: %s", exc)
        else:
            log.warning(
                "Skipping filter catchments -- missing polygonized catchments or split reaches"
            )

        # Rasterize filtered catchments --> GeoTIFF (HydroID burn)
        filtered_catch_tif = (
            bd / f"gw_catchments_reaches_filtered_addedAttributes_{bid}.tif"
        )
        if should_skip(filtered_catch_tif):
            _progress(15, "rasterize filtered catchments", skipped=True)
            log.info("Rasterize filtered catchments: output exists, skipping")
            outputs["filtered_catchments_tif"] = filtered_catch_tif
        elif filtered_catchments.exists() and gw_reaches.exists():
            _progress(15, "rasterize filtered catchments")
            log.info("Rasterize filtered catchments")
            _rasterize_catchments(filtered_catchments, gw_reaches, filtered_catch_tif)
            outputs["filtered_catchments_tif"] = filtered_catch_tif
        else:
            log.warning("Skipping rasterize filtered catchments -- missing inputs")

        # Mask slopes to filtered catchments
        slopes_masked = bd / f"slopes_d8_dem_meters_masked_{bid}.tif"
        if should_skip(slopes_masked):
            _progress(16, "mask slopes to catchments", skipped=True)
            log.info("Slopes mask: output exists, skipping")
            outputs["slopes_masked"] = slopes_masked
        elif slopes_d8.exists() and filtered_catch_tif.exists():
            _progress(16, "mask slopes to catchments")
            mask_slopes_to_catchments(slopes_d8, filtered_catch_tif, slopes_masked)
            outputs["slopes_masked"] = slopes_masked
        else:
            log.warning(
                "Skipping slopes mask -- missing slopes or filtered catchments raster"
            )

        # Stage ladder + catchment list (inputs to the SRC builder)
        stages_txt = bd / f"stage_{bid}.txt"
        catchlist_txt = bd / f"catch_list_{bid}.txt"
        if should_skip(stages_txt, catchlist_txt):
            _progress(17, "stages + catchlist", skipped=True)
            log.info("stages_catchlist: outputs exist, skipping")
            outputs["stages_txt"] = stages_txt
            outputs["catchlist_txt"] = catchlist_txt
        elif filtered_flows.exists() and filtered_catchments.exists():
            _progress(17, "stages + catchlist")
            make_stages_and_catchlist(
                flows_gpkg=filtered_flows,
                catchments_gpkg=filtered_catchments,
                out_stages=stages_txt,
                out_catchlist=catchlist_txt,
                stages_min=self.stage_min_m,
                stages_interval=self.stage_interval_m,
                stages_max=self.stage_max_m,
            )
            outputs["stages_txt"] = stages_txt
            outputs["catchlist_txt"] = catchlist_txt
        else:
            log.warning(
                "Skipping stages_catchlist -- missing filtered flows or catchments"
            )

        # SRC base (Python port of TauDEM catchhydrogeo)
        src_base_csv = bd / f"src_base_{bid}.csv"
        if should_skip(src_base_csv):
            _progress(18, "synthetic rating curve base", skipped=True)
            log.info("build_src: output exists, skipping")
            outputs["src_base_csv"] = src_base_csv
        elif (
            rem_zeroed.exists()
            and filtered_catch_tif.exists()
            and slopes_masked.exists()
            and stages_txt.exists()
            and catchlist_txt.exists()
        ):
            _progress(18, "synthetic rating curve base")
            build_src_base(
                hand_raster=rem_zeroed,
                catch_raster=filtered_catch_tif,
                slope_raster=slopes_masked,
                catchlist_txt=catchlist_txt,
                stages_txt=stages_txt,
                out_csv=src_base_csv,
            )
            outputs["src_base_csv"] = src_base_csv
        else:
            log.warning(
                "Skipping SRC base -- missing HAND / catchments raster / slopes / stage files"
            )

        # Crosswalk to NWM feature_ids + hydroTable
        xwalk_catch = (
            bd
            / f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{bid}.gpkg"
        )
        xwalk_flows = (
            bd
            / f"demDerived_reaches_split_filtered_addedAttributes_crosswalked_{bid}.gpkg"
        )
        src_full_csv = bd / f"src_full_crosswalked_{bid}.csv"
        src_json = bd / f"src_{bid}.json"
        crosswalk_csv = bd / f"crosswalk_table_{bid}.csv"
        hydro_table_csv = bd / f"hydroTable_{bid}.csv"
        sml_seg_csv = bd / f"small_segments_{bid}.csv"

        if should_skip(hydro_table_csv):
            _progress(19, "crosswalk + hydroTable", skipped=True)
            log.info("add_crosswalk: outputs exist, skipping")
            outputs["hydro_table"] = hydro_table_csv
            outputs["crosswalked_catchments"] = xwalk_catch
            outputs["crosswalked_flows"] = xwalk_flows
        elif (
            filtered_catchments.exists()
            and filtered_flows.exists()
            and src_base_csv.exists()
            and self.streams_gpkg.exists()
        ):
            _progress(19, "crosswalk + hydroTable")
            try:
                xwalk_out = add_crosswalk(
                    catchments_gpkg=filtered_catchments,
                    flows_gpkg=filtered_flows,
                    src_base_csv=src_base_csv,
                    boundary_gpkg=self.boundary_gpkg,
                    nwm_streams_gpkg=self.streams_gpkg,
                    out_catchments_gpkg=xwalk_catch,
                    out_flows_gpkg=xwalk_flows,
                    out_src_csv=src_full_csv,
                    out_src_json=src_json,
                    out_crosswalk_csv=crosswalk_csv,
                    out_hydro_csv=hydro_table_csv,
                    aoi_code=self._resolve_aoi_code(),
                    mannings_n=self.mannings_n,
                    min_catchment_area=self.min_catchment_area,
                    min_stream_length=self.min_stream_length,
                    max_distance_m=self.crosswalk_max_distance_m,
                    small_segments_csv=sml_seg_csv,
                )
                outputs.update(xwalk_out)
            except NoCrosswalkError as exc:
                log.warning("add_crosswalk: %s", exc)
        else:
            log.warning(
                "Skipping crosswalk -- missing filtered catchments, SRC base, or NWM streams"
            )

        # Heal HAND for OSM bridges (in-place update of rem_zeroed_masked)
        bridges_gpkg = self.osm_bridges_gpkg
        bridge_centroids = bd / f"osm_bridge_centroids_{bid}.gpkg"
        if should_skip(bridge_centroids):
            _progress(20, "heal HAND bridges", skipped=True)
            log.info("heal_bridges_osm: output exists, skipping")
            outputs["osm_bridge_centroids"] = bridge_centroids
        elif (
            bridges_gpkg is not None
            and bridges_gpkg.exists()
            and rem_zeroed.exists()
            and xwalk_catch.exists()
        ):
            _progress(20, "heal HAND bridges")
            try:
                heal_out = heal_bridges_osm(
                    hand_raster=rem_zeroed,
                    bridges_gpkg=bridges_gpkg,
                    catchments_gpkg=xwalk_catch,
                    out_centroids_gpkg=bridge_centroids,
                    bridge_diff_raster=self.bridge_diff_raster,
                )
                if heal_out is not None:
                    outputs["osm_bridge_centroids"] = bridge_centroids
            except ModuleNotFoundError as exc:
                log.warning("Bridge healing skipped: %s", exc)
        else:
            log.info(
                "Skipping bridge healing -- no OSM bridges or crosswalked catchments"
            )

        # OSM road FIMpact
        roads_gpkg = self.osm_roads_gpkg
        roads_fimpact_csv = bd / f"osm_roads_fimpact_{bid}.csv"
        if should_skip(roads_fimpact_csv):
            _progress(21, "OSM road FIMpact", skipped=True)
            log.info("process_roads_fimpact: output exists, skipping")
            outputs["osm_roads_fimpact_csv"] = roads_fimpact_csv
        elif (
            roads_gpkg is not None
            and roads_gpkg.exists()
            and rem_zeroed.exists()
            and xwalk_catch.exists()
        ):
            _progress(21, "OSM road FIMpact")
            try:
                roads_out = process_roads_fimpact(
                    hand_raster=rem_zeroed,
                    roads_gpkg=roads_gpkg,
                    catchments_gpkg=xwalk_catch,
                    out_csv=roads_fimpact_csv,
                )
                if roads_out is not None:
                    outputs["osm_roads_fimpact_csv"] = roads_fimpact_csv
            except ModuleNotFoundError as exc:
                log.warning("Road FIMpact skipped: %s", exc)
        else:
            log.info("Skipping road FIMpact -- no OSM roads or crosswalked catchments")

        # Final summary line — counts the produced outputs for quick diff inspection.
        _progress(22, "summary")
        log.info("CreateHAND outputs: %s", list(outputs.keys()))
        return outputs

    def _resolve_aoi_code(self) -> str:
        """
        Return the user-supplied ``aoi_code`` when set, otherwise derive a
        fallback from the AOI directory name. The fallback strips an optional
        ``HUC`` prefix and then keeps the trailing digits — convenient for
        HUC-named workdirs but works for any directory name that ends in a
        digit-only identifier.
        """
        if self.aoi_code:
            return self.aoi_code
        name = self.aoi_dir.name
        if name.upper().startswith("HUC"):
            stripped = name[3:]
            if stripped:
                return stripped
        digits = "".join(c for c in name if c.isdigit())
        return digits if digits else name


def _exists_warn(path: Optional[Path], name: str) -> None:
    if path is None or not path.exists():
        log.warning("Expected file not found: %s = %s", name, path)


def _polygonize_catchments(catchments_raster: Path, out_gpkg: Path) -> None:
    """
    Polygonize the catchment ID raster into a GeoPackage (one polygon per HydroID).
    Uses 8-connectivity, matching gdal_polygonize -8.  No smoothing is applied —
    the pixel boundaries are exact and shared between adjacent catchments, so
    any simplification would break topology and create slivers/overlaps.
    """
    import numpy as np
    import rasterio
    from rasterio.features import shapes
    import geopandas as gpd
    from shapely.geometry import shape

    if out_gpkg.exists():
        out_gpkg.unlink()

    geoms = []
    values = []

    with rasterio.open(str(catchments_raster)) as src:
        data = src.read(1).astype(np.int32)
        crs = src.crs
        transform = src.transform

    mask = data > 0
    for geom, val in shapes(data, mask=mask, transform=transform, connectivity=8):
        geoms.append(shape(geom))
        values.append(int(val))

    gdf = gpd.GeoDataFrame({"HydroID": values}, geometry=geoms, crs=crs)
    gdf.to_file(
        str(out_gpkg), driver="GPKG", layer="catchments", index=False, engine="fiona"
    )
    log.info("Polygonize: %d catchment polygons --> %s", len(gdf), out_gpkg.name)


def _rasterize_catchments(
    catchments_gpkg: Path,
    reference_raster: Path,
    out_tif: Path,
) -> None:
    """
    Replicates FIM:
        gdal_rasterize -a HydroID -a_nodata 0
                       gw_catchments_filtered.gpkg gw_catchments_filtered.tif
    Burns HydroID attribute onto the reference raster grid.
    """
    import numpy as np
    import rasterio
    from rasterio.features import rasterize as rio_rasterize
    import geopandas as gpd

    gdf = gpd.read_file(str(catchments_gpkg), engine="fiona")

    with rasterio.open(str(reference_raster)) as ref:
        meta = ref.meta.copy()
        transform = ref.transform
        height = ref.height
        width = ref.width

    meta.update(
        dtype="int32",
        nodata=0,
        compress="lzw",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        BIGTIFF="YES",
    )

    shapes_iter = (
        (geom, int(hid))
        for geom, hid in zip(gdf.geometry, gdf["HydroID"])
        if geom is not None and not geom.is_empty
    )

    burned = rio_rasterize(
        shapes_iter,
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype=np.int32,
        all_touched=False,
    )

    if out_tif.exists():
        out_tif.unlink()

    with rasterio.open(str(out_tif), "w", **meta) as dst:
        dst.write(burned, 1)

    log.info(
        "Rasterize filtered catchments --> %s  (%d features)", out_tif.name, len(gdf)
    )
