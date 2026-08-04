"""
Author: Supath Dhital
Date Updated : May 2026

branch-derivation helpers for area inputs.
1. standardise the staged stream network (geometry, connectivity, ids)
2. derive level paths from staged streams/catchments/lakes
3. buffer dissolved branches into processing polygons
4. write a branch list file

Step 1 is what makes the rest source-agnostic. NWM, NHDPlus HR and the NextGen
hydrofabric each stage the same information differently — single- vs multi-part
flowlines, coordinate- vs nexus-based connectivity, ``ID`` vs ``divide_id`` on
the catchments — so every input is folded onto one shape before any branch
logic runs. Nothing downstream needs to know which source it was handed.
"""

from __future__ import annotations

import logging
import re
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point

from ..source_naming import detect_identifier, resolve_source, source_name

gpd.options.io_engine = "pyogrio"

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cross-source conventions
#
# Hydrography sources disagree on the two things the level-path walk depends
# on. Geometry: NWM and NHDPlus stage plain LineStrings, the NextGen
# hydrofabric stages MultiLineStrings (shapely refuses ``.coords`` on those).
# Connectivity: NWM reaches share exact endpoint coordinates, NextGen
# flowpaths route through nexus points and leave a gap between neighbours, so
# coordinate matching silently shreds the network into single-reach branches.
# Everything below normalises both before any branch logic runs.
# ---------------------------------------------------------------------------

# Downstream-reach id columns, in preference order. NWM uses ``to_``; NextGen
# uses ``toid`` and points at a nexus (``nex-270518``) whose numeric stem is
# the downstream flowpath's id.
_TO_ID_CANDIDATES = (
    "to_",
    "toid",
    "to_id",
    "tocomid",
    "dscomid",
    "ds_id",
    "downstream",
)

# Explicit node-id pairs (NHDPlus ``FromNode``/``ToNode``) — already node keys,
# so they are used verbatim.
_NODE_PAIR_CANDIDATES = (("fromnode", "tonode"), ("from_node", "to_node"))

# Catchment columns that may carry the reach id, tried when the requested one
# is absent or joins nothing.
_CATCHMENT_ID_CANDIDATES = ("ID", "divide_id", "wb_id", "feature_id", "COMID", "comid")

# "no downstream" sentinels seen across sources.
_NULL_IDS = {"", "0", "-1", "none", "nan", "<na>", "null"}

_DIGITS = re.compile(r"\d+")


@dataclass(slots=True)
class BranchDerivationResult:
    """Concrete file outputs from the branch-derivation stage."""

    output_dir: Path
    levelpaths: Path
    dissolved_levelpaths: Path
    extended_levelpaths: Path
    catchments_levelpaths: Path
    headwaters: Path
    dissolved_headwaters: Path
    branch_polygons: Path
    branch_list: Path
    branch_dataframe: pd.DataFrame
    levee_levelpaths: Optional[Path] = field(default=None)


@dataclass(slots=True)
class AreaInputPaths:
    """Auto-discovered staged-input paths for one area folder."""

    area_id: str
    staged_dir: Path
    boundary: Path
    buffered_boundary: Path
    buffered_stream_boundary: Path
    stream_network: Path
    catchments: Path
    lakes: Optional[Path]
    headwaters: Optional[Path]
    dem: Optional[Path]
    levees: Optional[Path]
    leveed_areas: Optional[Path]


def discover_area_inputs(
    staged_dir: str | Path,
    *,
    area_id: Optional[str] = None,
) -> AreaInputPaths:
    """Discover standard staged inputs from a folder like ``out/test_smallB``."""

    root = Path(staged_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Staged input directory not found: {root}")

    def must_find(*names: str) -> Path:
        for name in names:
            candidate = root / name
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"Could not find any of {names!r} in {root}")

    def may_find(*names: str) -> Optional[Path]:
        for name in names:
            candidate = root / name
            if candidate.exists():
                return candidate
        return None

    # Source-derived files carry an identifier prefix (default "nwm"); resolve
    # them by their stable suffix so any prefix is found.
    def must_source(kind: str) -> Path:
        path = resolve_source(root, kind)
        if not path.exists():
            raise FileNotFoundError(f"Could not find a *{kind} file in {root}")
        return path

    def may_source(kind: str) -> Optional[Path]:
        path = resolve_source(root, kind)
        return path if path.exists() else None

    return AreaInputPaths(
        area_id=area_id or root.name,
        staged_dir=root,
        boundary=must_find("wbd.gpkg"),
        buffered_boundary=must_find("wbd_buffered.gpkg", "wbd.gpkg"),
        buffered_stream_boundary=must_find(
            "wbd_buffered_streams.gpkg",
            "wbd_buffered.gpkg",
            "wbd.gpkg",
        ),
        stream_network=must_source("streams"),
        catchments=must_source("catchments"),
        lakes=may_source("lakes"),
        headwaters=may_source("headwaters_points") or may_source("headwaters"),
        dem=may_find("dem.tif"),
        levees=may_find("nld_subset_levees.gpkg"),
        leveed_areas=may_find("LeveeProtectedAreas_subset.gpkg"),
    )


class BranchDerivation:
    """Derive levelpaths, branch polygons, and branch lists for a staged area."""

    def __init__(
        self,
        out_dir: str | Path,
        *,
        area_id: Optional[str] = None,
        branch_id_attribute: str = "levpa_id",
        reach_id_attribute: str = "ID",
        catchment_reach_id_attribute: str = "ID",
        stream_order_attribute: str = "order_",
        to_id_attribute: Optional[str] = None,
        branch_buffer_distance_meters: float = 7000.0,
        excluded_stream_orders: tuple[int, ...] = (1, 2),
        min_stream_order: Optional[int] = None,
        max_stream_order: Optional[int] = None,
        stream_layer: Optional[str] = None,
        catchments_layer: Optional[str] = None,
        waterbodies_layer: Optional[str] = None,
        boundary_layer: Optional[str] = None,
        buffered_boundary_layer: Optional[str] = None,
        buffered_stream_boundary_layer: Optional[str] = None,
        headwaters_layer: Optional[str] = None,
        stream_network: Optional[str | Path] = None,
        catchments: Optional[str | Path] = None,
        lakes: Optional[str | Path] = None,
        boundary: Optional[str | Path] = None,
        buffered_boundary: Optional[str | Path] = None,
        buffered_stream_boundary: Optional[str | Path] = None,
        headwaters: Optional[str | Path] = None,
        levees: Optional[str | Path] = None,
        leveed_areas: Optional[str | Path] = None,
        levee_id_attribute: str = "SYSTEM_ID",
        levee_buffer: float = 1000.0,
        apply_levees: bool = True,
        single_levelpath_branch_zero_only: bool = True,
    ) -> None:
        self.single_levelpath_branch_zero_only = single_levelpath_branch_zero_only
        self.out_dir = Path(out_dir).expanduser().resolve()
        self.area_id = area_id
        self.branch_id_attribute = branch_id_attribute
        self.reach_id_attribute = reach_id_attribute
        self.catchment_reach_id_attribute = catchment_reach_id_attribute
        self.stream_order_attribute = stream_order_attribute
        self.to_id_attribute = to_id_attribute
        self.branch_buffer_distance_meters = branch_buffer_distance_meters
        self.excluded_stream_orders = excluded_stream_orders
        self.min_stream_order = min_stream_order
        self.max_stream_order = max_stream_order
        self.stream_layer = stream_layer
        self.catchments_layer = catchments_layer
        self.waterbodies_layer = waterbodies_layer
        self.boundary_layer = boundary_layer
        self.buffered_boundary_layer = buffered_boundary_layer
        self.buffered_stream_boundary_layer = buffered_stream_boundary_layer
        self.headwaters_layer = headwaters_layer
        self.stream_network_override = stream_network
        self.catchments_override = catchments
        self.lakes_override = lakes
        self.boundary_override = boundary
        self.buffered_boundary_override = buffered_boundary
        self.buffered_stream_boundary_override = buffered_stream_boundary
        self.headwaters_override = headwaters
        self.levees_override = levees
        self.leveed_areas_override = leveed_areas
        self.levee_id_attribute = levee_id_attribute
        self.levee_buffer = levee_buffer
        self.apply_levees = bool(apply_levees)
        self.logger = self._setup_logger()

    @staticmethod
    def discover(
        out_dir: str | Path, *, area_id: Optional[str] = None
    ) -> AreaInputPaths:
        return discover_area_inputs(out_dir, area_id=area_id)

    def run(self) -> BranchDerivationResult:
        self._announce("Branch derivation started")
        self.logger.info("=== BRANCH DERIVATION START ===")
        discovered = None
        if not all(
            [
                self.stream_network_override,
                self.catchments_override,
                self.boundary_override,
                self.buffered_boundary_override,
            ]
        ):
            discovered = discover_area_inputs(self.out_dir, area_id=self.area_id)
            self.logger.info("Staged hydro folder --> %s", self.out_dir)
        else:
            self.logger.info("Using explicit input dataset overrides")

        # Source-file prefix (default "nwm"); detected from the staged streams
        # file so derived level-path outputs keep the same identifier.
        identifier = detect_identifier(self.out_dir)

        stream_path = (
            Path(self.stream_network_override).resolve()
            if self.stream_network_override
            else discovered.stream_network
        )
        catchment_path = (
            Path(self.catchments_override).resolve()
            if self.catchments_override
            else discovered.catchments
        )
        lake_path = (
            Path(self.lakes_override).resolve()
            if self.lakes_override
            else (discovered.lakes if discovered is not None else None)
        )
        boundary_path = (
            Path(self.boundary_override).resolve()
            if self.boundary_override
            else discovered.boundary
        )
        buffered_boundary_path = (
            Path(self.buffered_boundary_override).resolve()
            if self.buffered_boundary_override
            else discovered.buffered_boundary
        )
        buffered_stream_boundary_path = (
            Path(self.buffered_stream_boundary_override).resolve()
            if self.buffered_stream_boundary_override
            else (
                discovered.buffered_stream_boundary
                if discovered is not None
                else boundary_path
            )
        )
        headwater_path = (
            Path(self.headwaters_override).resolve()
            if self.headwaters_override
            else (discovered.headwaters if discovered is not None else None)
        )

        stream_path = self._localize_vector_override(
            stream_path,
            source_name("streams", identifier),
            self.stream_network_override is not None,
            self.stream_layer,
        )
        catchment_path = self._localize_vector_override(
            catchment_path,
            source_name("catchments", identifier),
            self.catchments_override is not None,
            self.catchments_layer,
        )
        boundary_path = self._localize_vector_override(
            boundary_path,
            "wbd.gpkg",
            self.boundary_override is not None,
            self.boundary_layer,
        )
        buffered_boundary_path = self._localize_vector_override(
            buffered_boundary_path,
            "wbd_buffered.gpkg",
            self.buffered_boundary_override is not None,
            self.buffered_boundary_layer,
        )
        buffered_stream_boundary_path = self._localize_vector_override(
            buffered_stream_boundary_path,
            "wbd_buffered_streams.gpkg",
            self.buffered_stream_boundary_override is not None,
            self.buffered_stream_boundary_layer,
        )
        if lake_path is not None:
            lake_path = self._localize_vector_override(
                lake_path,
                source_name("lakes", identifier),
                self.lakes_override is not None,
                self.waterbodies_layer,
            )
        if headwater_path is not None:
            headwater_path = self._localize_vector_override(
                headwater_path,
                source_name("headwaters_points", identifier),
                self.headwaters_override is not None,
                self.headwaters_layer,
            )

        # apply_levees=False skips levee_levelpaths.csv, which is what the
        # per-branch protected-area mask keys off — so the whole levee path
        # through branch processing goes quiet with it.
        levees_path: Optional[Path] = None
        leveed_areas_path: Optional[Path] = None
        if self.apply_levees:
            levees_path = (
                Path(self.levees_override).resolve()
                if self.levees_override
                else (discovered.levees if discovered is not None else None)
            )
            leveed_areas_path = (
                Path(self.leveed_areas_override).resolve()
                if self.leveed_areas_override
                else (discovered.leveed_areas if discovered is not None else None)
            )

        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info("--- Required Inputs ---")
        self.logger.info("Streams --> %s", stream_path.name)
        self.logger.info("Catchments --> %s", catchment_path.name)
        self.logger.info("Boundary --> %s", boundary_path.name)
        self.logger.info("Buffered boundary --> %s", buffered_boundary_path.name)
        if lake_path is not None:
            self.logger.info("Lakes --> %s", lake_path.name)
        else:
            self.logger.warning("Lakes --> not provided; skipping lake filtering")
        if headwater_path is not None:
            self.logger.info("Headwaters --> %s", headwater_path.name)
        else:
            self.logger.warning(
                "Headwaters --> not provided; deriving from upstream-most stream starts"
            )

        # load and filter streams, then assign levelpaths
        self._announce("Reading staged hydro inputs")
        streams = _read_vector(stream_path, self.stream_layer)
        streams = _filter_stream_orders(
            streams,
            self.stream_order_attribute,
            self.excluded_stream_orders,
            min_stream_order=self.min_stream_order,
            max_stream_order=self.max_stream_order,
        )
        if streams.empty:
            self.logger.warning("No stream reaches remain after stream-order filtering")
            raise ValueError("No stream reaches remain after stream-order filtering.")

        streams = _ensure_reach_id(streams, self.reach_id_attribute)
        streams = _standardize_streams(streams, self.reach_id_attribute)
        streams = _derive_network_fields(
            streams,
            reach_id_attribute=self.reach_id_attribute,
            to_id_attribute=self.to_id_attribute,
        )
        streams = _assign_levelpaths(
            streams,
            reach_id_attribute=self.reach_id_attribute,
            stream_order_attribute=self.stream_order_attribute,
            branch_id_attribute=self.branch_id_attribute,
        )
        self._announce("Levelpaths derived")

        # Branches exist to split a network into level paths. When there is only
        # one, the branch would re-derive what branch zero already covers for the
        # whole AOI — the same reaches, twice — and it would still need a headwater
        # seed landing exactly on the line. Hand the AOI to branch zero instead.
        n_levelpaths = streams[self.branch_id_attribute].nunique()
        if self.single_levelpath_branch_zero_only and n_levelpaths <= 1:
            return self._branch_zero_only_result(streams, n_levelpaths)

        # load supporting layers and align CRS
        catchments_gdf = _read_vector(catchment_path, self.catchments_layer)
        lakes_gdf = (
            _read_vector(lake_path, self.waterbodies_layer)
            if lake_path is not None
            else gpd.GeoDataFrame(geometry=[], crs=streams.crs)
        )
        boundary_gdf = _read_vector(boundary_path, self.boundary_layer)
        buffered_boundary_gdf = _read_vector(
            buffered_boundary_path, self.buffered_boundary_layer
        )
        buffered_stream_boundary_gdf = _read_vector(
            buffered_stream_boundary_path,
            self.buffered_stream_boundary_layer,
        )

        catchments_gdf = _align_crs(catchments_gdf, streams.crs)
        catchments_gdf = _standardize_catchments(
            catchments_gdf,
            streams[self.reach_id_attribute],
            self.catchment_reach_id_attribute,
        )
        lakes_gdf = _align_crs(lakes_gdf, streams.crs)
        boundary_gdf = _align_crs(boundary_gdf, streams.crs)
        buffered_boundary_gdf = _align_crs(buffered_boundary_gdf, streams.crs)
        buffered_stream_boundary_gdf = _align_crs(
            buffered_stream_boundary_gdf, streams.crs
        )

        # Drop level paths with no catchment, before
        # dissolve/headwaters/polygons so a catchment-less branch never reaches
        # any output and never seeds an empty per-branch DEM.
        streams = _remove_branches_without_catchments(
            streams,
            catchments_gdf,
            reach_id_attribute=self.reach_id_attribute,
            branch_id_attribute=self.branch_id_attribute,
        )
        if streams.empty:
            self.logger.warning("No stream branches remain after catchment filtering")
            raise ValueError(
                "No stream branches remain after removing branches without catchments."
            )

        # attach branch IDs to catchments, dissolve, build polygons and headwaters
        catchments_levelpaths = _attach_branch_ids_to_catchments(
            catchments_gdf,
            streams,
            reach_id_attribute=self.reach_id_attribute,
            branch_id_attribute=self.branch_id_attribute,
        )

        dissolved_levelpaths = _dissolve_levelpaths(
            streams,
            self.branch_id_attribute,
            clip_boundary=buffered_stream_boundary_gdf,
            waterbodies=lakes_gdf,
            huc_boundary=boundary_gdf,
        )
        extended_levelpaths = streams.copy()

        headwaters_gdf = _build_headwaters(
            streams,
            branch_id_attribute=self.branch_id_attribute,
            reach_id_attribute=self.reach_id_attribute,
            provided_headwaters=(
                _read_vector(headwater_path, self.headwaters_layer)
                if headwater_path
                else None
            ),
        )
        dissolved_headwaters = headwaters_gdf.drop_duplicates(
            subset=[self.branch_id_attribute]
        ).copy()

        branch_polygons = _build_branch_polygons(
            dissolved_levelpaths,
            self.branch_id_attribute,
            self.branch_buffer_distance_meters,
            buffered_boundary_gdf,
        )
        self._announce("Branch polygons generated")

        branch_df = (
            dissolved_levelpaths[[self.branch_id_attribute]]
            .drop_duplicates()
            .sort_values(self.branch_id_attribute)
            .reset_index(drop=True)
        )

        # write all outputs (level-path derivatives keep the source identifier)
        result = BranchDerivationResult(
            output_dir=self.out_dir,
            levelpaths=self.out_dir / source_name("lp_streams", identifier),
            dissolved_levelpaths=self.out_dir
            / source_name("lp_streams_dissolved", identifier),
            extended_levelpaths=self.out_dir
            / source_name("lp_streams_extended", identifier),
            catchments_levelpaths=self.out_dir
            / source_name("lp_catchments", identifier),
            headwaters=self.out_dir / source_name("headwaters", identifier),
            dissolved_headwaters=self.out_dir
            / source_name("lp_streams_dissolved_headwaters", identifier),
            branch_polygons=self.out_dir / "branch_polygons.gpkg",
            branch_list=self.out_dir / "branch_ids.lst",
            branch_dataframe=branch_df,
        )

        _write_gpkg(streams, result.levelpaths)
        self.logger.info("Levelpaths --> %s", result.levelpaths.name)
        _write_gpkg(dissolved_levelpaths, result.dissolved_levelpaths)
        self.logger.info(
            "Dissolved levelpaths --> %s", result.dissolved_levelpaths.name
        )
        _write_gpkg(extended_levelpaths, result.extended_levelpaths)
        self.logger.info("Levelpaths extended --> %s", result.extended_levelpaths.name)
        _write_gpkg(catchments_levelpaths, result.catchments_levelpaths)
        self.logger.info(
            "Catchments levelpaths --> %s", result.catchments_levelpaths.name
        )
        _write_gpkg(headwaters_gdf, result.headwaters)
        self.logger.info("Headwaters --> %s", result.headwaters.name)
        _write_gpkg(dissolved_headwaters, result.dissolved_headwaters)
        self.logger.info(
            "Dissolved headwaters --> %s", result.dissolved_headwaters.name
        )
        _write_gpkg(branch_polygons, result.branch_polygons)
        self.logger.info("Branch polygons --> %s", result.branch_polygons.name)
        branch_df.to_csv(result.branch_list, sep=" ", index=False, header=False)
        self.logger.info("Branch list --> %s", result.branch_list.name)

        # associate level paths with levees if levee data is present
        if levees_path is not None and levees_path.exists():
            if leveed_areas_path is not None and leveed_areas_path.exists():
                self._announce("Associating level paths with levees")
                levee_levelpaths_path = self.out_dir / "levee_levelpaths.csv"
                written = _associate_levelpaths_with_levees(
                    levees_path=levees_path,
                    leveed_areas_path=leveed_areas_path,
                    dissolved_levelpaths=dissolved_levelpaths,
                    branch_id_attribute=self.branch_id_attribute,
                    levee_id_attribute=self.levee_id_attribute,
                    levee_buffer=self.levee_buffer,
                    out_path=levee_levelpaths_path,
                )
                if written:
                    result.levee_levelpaths = levee_levelpaths_path
                    self.logger.info(
                        "Levee levelpaths --> %s", levee_levelpaths_path.name
                    )
                else:
                    self.logger.info(
                        "Levee levelpaths --> no associations found, skipped"
                    )
            else:
                self.logger.warning(
                    "Levees provided but leveed-areas file not found; skipping levee association"
                )
        elif not self.apply_levees:
            self.logger.info("apply_levees=False --> skipping levee association")
        else:
            self.logger.info("Levees --> not provided; skipping levee association")

        self.logger.info("=== BRANCH DERIVATION COMPLETE ===")
        self._announce("Branch derivation complete")

        return result

    def _branch_zero_only_result(
        self, streams: gpd.GeoDataFrame, n_levelpaths: int
    ) -> BranchDerivationResult:
        """Outputs for an AOI that needs no branches: streams, empty branch list.

        Only the levelpath layer is written, since branch zero reads the staged
        streams/catchments directly and treats levelpaths and headwaters as
        optional. The empty branch list is what makes the branch loop a no-op.
        """
        self.logger.info(
            "%d reach(es) on %d level path — branch zero covers the AOI, "
            "no levelpath branches",
            len(streams),
            n_levelpaths,
        )

        identifier = detect_identifier(self.out_dir)
        if self.branch_id_attribute not in streams.columns:
            streams[self.branch_id_attribute] = str(
                streams[self.reach_id_attribute].iloc[0]
            )

        result = BranchDerivationResult(
            output_dir=self.out_dir,
            levelpaths=self.out_dir / source_name("lp_streams", identifier),
            dissolved_levelpaths=self.out_dir
            / source_name("lp_streams_dissolved", identifier),
            extended_levelpaths=self.out_dir
            / source_name("lp_streams_extended", identifier),
            catchments_levelpaths=self.out_dir
            / source_name("lp_catchments", identifier),
            headwaters=self.out_dir / source_name("headwaters", identifier),
            dissolved_headwaters=self.out_dir
            / source_name("lp_streams_dissolved_headwaters", identifier),
            branch_polygons=self.out_dir / "branch_polygons.gpkg",
            branch_list=self.out_dir / "branch_ids.lst",
            branch_dataframe=pd.DataFrame({self.branch_id_attribute: []}),
        )

        _write_gpkg(streams, result.levelpaths)
        self.logger.info("Levelpaths --> %s", result.levelpaths.name)
        result.branch_list.write_text("")
        self.logger.info("Branch list --> %s (empty)", result.branch_list.name)
        return result

    def _setup_logger(self) -> logging.Logger:
        # Attach the shared file+stream handlers to the fimbox root so this
        # stage's logs appear in the same processing.log as the rest of the
        # pipeline, then return a child logger for branch derivation.
        from ...logging_utils import attach_case_log, get_logger

        self.out_dir.mkdir(parents=True, exist_ok=True)
        attach_case_log(self.out_dir)
        return get_logger(f"fimbox.branch_derivation.{self.out_dir.name}")

    @staticmethod
    def _announce(message: str) -> None:
        logging.getLogger("fimbox").info(message)

    def _localize_vector_override(
        self,
        source_path: Path,
        canonical_name: str,
        should_copy: bool,
        layer: Optional[str],
    ) -> Path:
        if not should_copy:
            return source_path

        target_path = self.out_dir / canonical_name
        if source_path.resolve() == target_path.resolve():
            return source_path

        self.logger.info("Normalizing %s --> %s", source_path.name, canonical_name)
        gdf = _read_vector(source_path, layer)
        if target_path.exists():
            target_path.unlink()
        _write_gpkg(gdf, target_path)
        return target_path


def derive_area_branches(
    out_dir: str | Path,
    *,
    area_id: Optional[str] = None,
    stream_network: Optional[str | Path] = None,
    catchments: Optional[str | Path] = None,
    lakes: Optional[str | Path] = None,
    boundary: Optional[str | Path] = None,
    buffered_boundary: Optional[str | Path] = None,
    buffered_stream_boundary: Optional[str | Path] = None,
    headwaters: Optional[str | Path] = None,
    levees: Optional[str | Path] = None,
    leveed_areas: Optional[str | Path] = None,
    levee_id_attribute: str = "SYSTEM_ID",
    levee_buffer: float = 1000.0,
    apply_levees: bool = True,
    branch_id_attribute: str = "levpa_id",
    reach_id_attribute: str = "ID",
    catchment_reach_id_attribute: str = "ID",
    stream_order_attribute: str = "order_",
    to_id_attribute: Optional[str] = None,
    branch_buffer_distance_meters: float = 7000.0,
    excluded_stream_orders: tuple[int, ...] = (1, 2),
    min_stream_order: Optional[int] = None,
    max_stream_order: Optional[int] = None,
    stream_layer: Optional[str] = None,
    catchments_layer: Optional[str] = None,
    waterbodies_layer: Optional[str] = None,
    boundary_layer: Optional[str] = None,
    buffered_boundary_layer: Optional[str] = None,
    buffered_stream_boundary_layer: Optional[str] = None,
    headwaters_layer: Optional[str] = None,
) -> BranchDerivationResult:
    """Compatibility wrapper around :class:`BranchDerivation`."""

    return BranchDerivation(
        out_dir=out_dir,
        area_id=area_id,
        branch_id_attribute=branch_id_attribute,
        reach_id_attribute=reach_id_attribute,
        catchment_reach_id_attribute=catchment_reach_id_attribute,
        stream_order_attribute=stream_order_attribute,
        to_id_attribute=to_id_attribute,
        branch_buffer_distance_meters=branch_buffer_distance_meters,
        excluded_stream_orders=excluded_stream_orders,
        min_stream_order=min_stream_order,
        max_stream_order=max_stream_order,
        stream_layer=stream_layer,
        catchments_layer=catchments_layer,
        waterbodies_layer=waterbodies_layer,
        boundary_layer=boundary_layer,
        buffered_boundary_layer=buffered_boundary_layer,
        buffered_stream_boundary_layer=buffered_stream_boundary_layer,
        headwaters_layer=headwaters_layer,
        stream_network=stream_network,
        catchments=catchments,
        lakes=lakes,
        boundary=boundary,
        buffered_boundary=buffered_boundary,
        buffered_stream_boundary=buffered_stream_boundary,
        headwaters=headwaters,
        levees=levees,
        leveed_areas=leveed_areas,
        levee_id_attribute=levee_id_attribute,
        levee_buffer=levee_buffer,
        apply_levees=apply_levees,
    ).run()


def _read_vector(path: Path, layer: Optional[str]) -> gpd.GeoDataFrame:
    read_kwargs = {"engine": "pyogrio"}
    if layer:
        read_kwargs["layer"] = layer
    return gpd.read_file(path, **read_kwargs)


def _write_gpkg(gdf: gpd.GeoDataFrame, path: Path) -> None:
    gdf.to_file(path, driver="GPKG", engine="pyogrio")


def _normalise_sjoin_col(gdf: gpd.GeoDataFrame, col: str) -> gpd.GeoDataFrame:
    """Rename col_left or col_1 back to col when geopandas sjoin adds a suffix."""
    if col not in gdf.columns:
        for suffix in ("_left", "_1"):
            candidate = f"{col}{suffix}"
            if candidate in gdf.columns:
                return gdf.rename(columns={candidate: col})
    return gdf


def _align_crs(gdf: gpd.GeoDataFrame, target_crs) -> gpd.GeoDataFrame:
    if gdf.empty or gdf.crs == target_crs or target_crs is None:
        return gdf
    if gdf.crs is None:
        raise ValueError(
            "Input dataset has no CRS defined, so it cannot be aligned to the stream CRS."
        )
    return gdf.to_crs(target_crs)


def _filter_stream_orders(
    streams: gpd.GeoDataFrame,
    stream_order_attribute: str,
    excluded_orders: Iterable[int],
    *,
    min_stream_order: Optional[int] = None,
    max_stream_order: Optional[int] = None,
) -> gpd.GeoDataFrame:
    if stream_order_attribute not in streams.columns:
        return streams.copy()
    filtered = streams.copy()
    order_series = pd.to_numeric(filtered[stream_order_attribute], errors="coerce")
    mask = ~order_series.isin(list(excluded_orders))
    if min_stream_order is not None:
        mask &= order_series >= min_stream_order
    if max_stream_order is not None:
        mask &= order_series <= max_stream_order
    filtered = filtered.loc[mask].copy()
    filtered[stream_order_attribute] = order_series.loc[filtered.index]
    return filtered


def _ensure_reach_id(
    streams: gpd.GeoDataFrame, reach_id_attribute: str
) -> gpd.GeoDataFrame:
    if reach_id_attribute not in streams.columns:
        raise KeyError(
            f"Reach id column '{reach_id_attribute}' was not found in the stream network."
        )
    streams = streams.copy()
    streams[reach_id_attribute] = streams[reach_id_attribute].astype(str)
    return streams


def _find_column(gdf: gpd.GeoDataFrame, candidates: Iterable[str]) -> Optional[str]:
    """First candidate present in ``gdf``, matched without regard to case."""
    lookup = {str(col).lower(): col for col in gdf.columns}
    for name in candidates:
        hit = lookup.get(name.lower())
        if hit is not None:
            return hit
    return None


def _id_stem(value) -> str:
    """Numeric stem of an id, so ``nex-270518`` lines up with reach ``270518``."""
    match = _DIGITS.search(str(value))
    return match.group(0) if match else str(value).strip()


def _endpoints(geom) -> tuple[tuple[float, float], tuple[float, float]]:
    """Upstream-most and downstream-most vertex of a flowline.

    Multi-part reaches that resisted merging keep their parts in the order the
    source digitised them, which for every hydrography source is downstream.
    """
    if geom.geom_type == "LineString":
        coords = geom.coords
        return coords[0], coords[-1]
    parts = list(geom.geoms)
    return parts[0].coords[0], parts[-1].coords[-1]


def _standardize_streams(
    streams: gpd.GeoDataFrame, reach_id_attribute: str
) -> gpd.GeoDataFrame:
    """One row, one single-part LineString, one reach id — whatever was staged.

    Three source quirks are ironed out here so nothing downstream has to know
    which hydrography it is looking at:

    * empty / missing geometry is dropped rather than crashing the node walk;
    * a reach split across several rows is dissolved back into one;
    * MultiLineStrings are stitched with ``line_merge`` (NextGen wraps every
      flowpath in a multi, and shapely refuses ``.coords`` on those).
    """
    streams = streams.copy()
    geometry = streams.geometry.name

    blank = streams.geometry.isna() | streams.geometry.is_empty
    if blank.any():
        log.warning(
            "Dropping %d stream reach(es) with empty geometry", int(blank.sum())
        )
        streams = streams.loc[~blank].copy()
    if streams.empty:
        return streams

    # A duplicated reach id would silently collapse in the reach->length lookup
    # the level-path walk builds, so fold the parts together up front instead.
    duplicated = streams[reach_id_attribute].duplicated(keep=False)
    if duplicated.any():
        n_ids = streams.loc[duplicated, reach_id_attribute].nunique()
        log.warning(
            "Dissolving %d reach id(s) that span multiple rows into one row each",
            n_ids,
        )
        merged = streams.dissolve(by=reach_id_attribute, as_index=False)
        # dissolve keeps the first row's attributes, which is what we want; only
        # the column order needs restoring.
        streams = merged[
            [c for c in streams.columns if c in merged.columns and c != geometry]
            + [geometry]
        ]
        streams = gpd.GeoDataFrame(streams, geometry=geometry, crs=merged.crs)

    streams[geometry] = shapely.line_merge(streams.geometry.values)
    still_multi = streams.geom_type == "MultiLineString"
    if still_multi.any():
        log.warning(
            "%d reach(es) stayed multi-part after merging (disconnected segments); "
            "their end vertices are read from the first and last part",
            int(still_multi.sum()),
        )

    if streams.crs is not None and streams.crs.is_geographic:
        log.warning(
            "Stream CRS %s is geographic — branch buffers and reach lengths are "
            "in metres, so project the AOI (EPSG:5070 by default) before staging",
            streams.crs.to_string(),
        )
    return streams.reset_index(drop=True)


def _coordinate_nodes(streams: gpd.GeoDataFrame) -> tuple[list[str], list[str]]:
    """Node keys from shared endpoint coordinates — the NWM / NHDPlus convention."""
    from_nodes, to_nodes = [], []
    for geom in streams.geometry:
        start, end = _endpoints(geom)
        from_nodes.append(f"{round(start[0], 6)}_{round(start[1], 6)}")
        to_nodes.append(f"{round(end[0], 6)}_{round(end[1], 6)}")
    return from_nodes, to_nodes


def _explicit_nodes(
    streams: gpd.GeoDataFrame, reach_id_attribute: str, to_id_attribute: str
) -> tuple[list[str], list[str]]:
    """Node keys from a downstream-id column — the NextGen / nexus convention.

    Each reach owns the node at its head (``reach:<id>``) and points at the head
    node of the reach it drains into, so equality of these opaque keys carries
    exactly the same meaning as a shared coordinate would. Terminal reaches get
    a private key that nothing else can match.
    """
    reach_ids = streams[reach_id_attribute].astype(str)
    by_id = set(reach_ids)
    by_stem = {_id_stem(rid): rid for rid in by_id}

    from_nodes = [f"reach:{rid}" for rid in reach_ids]
    to_nodes = []
    for rid, raw in zip(reach_ids, streams[to_id_attribute]):
        target = str(raw).strip()
        if target not in by_id:
            # ``nex-270518`` -> ``270518``; also rescues int/float round-trips.
            target = by_stem.get(_id_stem(target), "") if target else ""
        if not target or target.lower() in _NULL_IDS or target == rid:
            to_nodes.append(f"outlet:{rid}")
        else:
            to_nodes.append(f"reach:{target}")
    return from_nodes, to_nodes


def _internal_edges(from_nodes: list[str], to_nodes: list[str]) -> int:
    """Upstream->downstream links these node keys resolve inside the network.

    This is the score the two connectivity conventions are compared on: a
    convention that leaves reaches unlinked scores low, whatever the reason.
    """
    heads: dict[str, int] = defaultdict(int)
    for node in from_nodes:
        heads[node] += 1
    return sum(heads.get(node, 0) for node in to_nodes)


def _derive_network_fields(
    streams: gpd.GeoDataFrame,
    *,
    reach_id_attribute: str = "ID",
    to_id_attribute: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Attach the node keys, reach length, and inlet vertex the branch walk needs.

    Connectivity is taken from whichever convention the staged network actually
    honours: an explicit downstream-id column when one is present and links more
    reaches than the geometry does, otherwise shared endpoint coordinates.
    Picking by connected-reach count means a source with a stale or partly-null
    ``to_`` column still falls back to coordinates instead of producing a
    network of orphans.

    ``to_id_attribute`` forces a specific column; leave it ``None`` to
    auto-detect (``to_``, ``toid``, ``FromNode``/``ToNode``, ...).
    """
    streams = streams.copy()

    coord_from, coord_to = _coordinate_nodes(streams)
    from_nodes, to_nodes = coord_from, coord_to
    source = "endpoint coordinates"

    node_pair = next(
        (
            (_find_column(streams, [a]), _find_column(streams, [b]))
            for a, b in _NODE_PAIR_CANDIDATES
            if _find_column(streams, [a]) and _find_column(streams, [b])
        ),
        None,
    )
    if to_id_attribute is None and node_pair is None:
        to_id_attribute = _find_column(streams, _TO_ID_CANDIDATES)

    if node_pair is not None:
        candidate = (
            [f"node:{v}" for v in streams[node_pair[0]]],
            [f"node:{v}" for v in streams[node_pair[1]]],
        )
        label = f"node columns {node_pair[0]}/{node_pair[1]}"
    elif to_id_attribute and to_id_attribute in streams.columns:
        candidate = _explicit_nodes(streams, reach_id_attribute, to_id_attribute)
        label = f"downstream-id column '{to_id_attribute}'"
    else:
        candidate = None
        label = ""

    if candidate is not None and _internal_edges(*candidate) > _internal_edges(
        coord_from, coord_to
    ):
        from_nodes, to_nodes = candidate
        source = label

    log.info(
        "Network connectivity from %s — %d of %d reach(es) linked",
        source,
        _internal_edges(from_nodes, to_nodes),
        len(streams),
    )

    streams["_from_node"] = from_nodes
    streams["_to_node"] = to_nodes
    streams["_length_km"] = _reach_lengths_km(streams)
    # Inlet vertex, carried as plain floats so it survives the GeoPackage write
    # and headwater derivation never has to touch ``.coords`` again.
    inlets = [_endpoints(geom)[0] for geom in streams.geometry]
    streams["_inlet_x"] = [float(pt[0]) for pt in inlets]
    streams["_inlet_y"] = [float(pt[1]) for pt in inlets]
    return streams


def _reach_lengths_km(streams: gpd.GeoDataFrame) -> list[float]:
    """Reach lengths in km, measured in a projected CRS whatever the input is."""
    geometry = streams.geometry
    if streams.crs is not None and streams.crs.is_geographic:
        geometry = streams.geometry.to_crs(streams.estimate_utm_crs())
    return [float(length) / 1000.0 for length in geometry.length]


def _assign_levelpaths(
    streams: gpd.GeoDataFrame,
    *,
    reach_id_attribute: str,
    stream_order_attribute: str,
    branch_id_attribute: str,
    max_branch_id_digits: int = 6,
) -> gpd.GeoDataFrame:
    streams = streams.copy()
    upstreams: dict[str, list[str]] = defaultdict(list)
    downstream: dict[str, Optional[str]] = {}
    reach_lengths = streams.set_index(reach_id_attribute)["_length_km"].to_dict()
    order_lookup = (
        streams.set_index(reach_id_attribute)[stream_order_attribute].to_dict()
        if stream_order_attribute in streams.columns
        else {}
    )

    to_node_to_reaches: dict[str, list[str]] = defaultdict(list)
    from_node_to_reaches: dict[str, list[str]] = defaultdict(list)

    for _, row in streams.iterrows():
        reach_id = row[reach_id_attribute]
        to_node_to_reaches[row["_to_node"]].append(reach_id)
        from_node_to_reaches[row["_from_node"]].append(reach_id)

    for _, row in streams.iterrows():
        reach_id = row[reach_id_attribute]
        direct_upstreams = to_node_to_reaches.get(row["_from_node"], [])
        upstreams[reach_id] = [rid for rid in direct_upstreams if rid != reach_id]
        direct_downstreams = from_node_to_reaches.get(row["_to_node"], [])
        downstream[reach_id] = next(
            (rid for rid in direct_downstreams if rid != reach_id), None
        )

    arbolate_cache: dict[str, float] = {}
    # A braided or mis-digitised network can route a reach back into itself.
    # Without this the walk would recurse until the stack gives out.
    walking: set[str] = set()

    def arbolate_sum(reach_id: str) -> float:
        if reach_id in arbolate_cache:
            return arbolate_cache[reach_id]
        if reach_id in walking:
            log.warning("Ignoring a flow loop through reach %s", reach_id)
            return 0.0
        walking.add(reach_id)
        total = reach_lengths.get(reach_id, 0.0)
        total += sum(arbolate_sum(up_id) for up_id in upstreams.get(reach_id, []))
        walking.discard(reach_id)
        arbolate_cache[reach_id] = total
        return total

    for reach_id in reach_lengths:
        arbolate_sum(reach_id)

    # assign synthetic branch IDs: outlets get prefix[:4]+sequential, tributaries inherit parent prefix
    assigned: dict[str, str] = {}
    outlet_reaches = [rid for rid, down_id in downstream.items() if down_id is None]
    outlet_reaches = sorted(
        outlet_reaches, key=lambda rid: (arbolate_cache[rid], rid), reverse=True
    )
    bid = [len(outlet_reaches) + 1]

    def walk_branch(start_reach_id: str, branch_id: str) -> None:
        current = start_reach_id
        while current and current not in assigned:
            assigned[current] = branch_id
            candidates = [
                rid for rid in upstreams.get(current, []) if rid not in assigned
            ]
            if not candidates:
                return
            # Mainstem selection: pick the upstream reach with the highest
            # stream order, breaking ties by arbolate sum, then reach id for
            # determinism.
            candidates = sorted(
                candidates,
                key=lambda rid: (
                    order_lookup.get(rid, -1),
                    arbolate_cache.get(rid, 0.0),
                    rid,
                ),
                reverse=True,
            )
            mainstem = candidates[0]
            for side_branch in candidates[1:]:
                new_branch_id = str(branch_id)[:4] + str(bid[0]).zfill(
                    max_branch_id_digits
                )
                bid[0] += 1
                walk_branch(side_branch, new_branch_id)
            current = mainstem

    for b, outlet_id in enumerate(outlet_reaches):
        if outlet_id not in assigned:
            synthetic_id = str(outlet_id)[:4] + str(b + 1).zfill(max_branch_id_digits)
            walk_branch(outlet_id, synthetic_id)

    for reach_id in reach_lengths:
        if reach_id not in assigned:
            synthetic_id = str(reach_id)[:4] + str(bid[0]).zfill(max_branch_id_digits)
            bid[0] += 1
            walk_branch(reach_id, synthetic_id)

    streams[branch_id_attribute] = streams[reach_id_attribute].map(assigned).astype(str)
    streams["arbolate_sum"] = streams[reach_id_attribute].map(arbolate_cache)
    return streams


def _standardize_catchments(
    catchments: gpd.GeoDataFrame,
    stream_reach_ids: Iterable[str],
    catchment_reach_id_attribute: str,
) -> gpd.GeoDataFrame:
    """Add ``_reach_key``: the catchment id in the form the reach ids use.

    Sources label the same catchment differently — ``ID``, ``divide_id``
    (``cat-270517``), ``wb_id`` (``wb-270517``) — and a BYO layer may only carry
    one of them. The column that actually overlaps the reach ids wins, with
    numeric-stem matching as the last resort so a prefix mismatch alone never
    strands a branch.
    """
    reach_ids = {str(rid) for rid in stream_reach_ids}
    stems = {_id_stem(rid): rid for rid in reach_ids}

    candidates = [catchment_reach_id_attribute] + [
        c for c in _CATCHMENT_ID_CANDIDATES if c != catchment_reach_id_attribute
    ]
    best_column, best_keys, best_hits = None, None, 0
    for name in candidates:
        column = _find_column(catchments, [name])
        if column is None:
            continue
        keys = catchments[column].astype(str)
        direct = keys.isin(reach_ids)
        # Only pay for the regex pass when the labels don't already line up.
        if not direct.all():
            keys = keys.where(direct, keys.map(lambda v: stems.get(_id_stem(v), v)))
            direct = keys.isin(reach_ids)
        hits = int(direct.sum())
        if hits > best_hits:
            best_column, best_keys, best_hits = column, keys, hits
        if best_hits == len(catchments):
            break

    if best_column is None:
        raise KeyError(
            f"Catchment reach id column '{catchment_reach_id_attribute}' was not "
            f"found in catchments, and none of {_CATCHMENT_ID_CANDIDATES} matched. "
            "Pass catchment_reach_id_attribute=<your column>."
        )
    if best_column != catchment_reach_id_attribute:
        log.info(
            "Catchments joined on '%s' (%d match) — '%s' matched no reach ids",
            best_column,
            best_hits,
            catchment_reach_id_attribute,
        )

    catchments = catchments.copy()
    catchments["_reach_key"] = best_keys
    return catchments


def _remove_branches_without_catchments(
    streams: gpd.GeoDataFrame,
    catchments: gpd.GeoDataFrame,
    *,
    reach_id_attribute: str,
    branch_id_attribute: str,
) -> gpd.GeoDataFrame:
    """Drop level paths whose reaches have no matching catchment.

    A whole branch is removed only when *none* of its reach ids appear in the
    catchment layer, so a branch with at least one catchment is kept intact.
    """
    catchment_reach_ids = set(catchments["_reach_key"].unique())

    reach_ids = streams[reach_id_attribute].astype(str)
    branch_has_catchment = (
        reach_ids.isin(catchment_reach_ids)
        .groupby(streams[branch_id_attribute])
        .transform("any")
    )
    dropped = streams.loc[~branch_has_catchment, branch_id_attribute].unique()
    if len(dropped):
        log.info(
            "Removing %d branch(es) without catchments: %s",
            len(dropped),
            ", ".join(map(str, dropped)),
        )
    return streams.loc[branch_has_catchment].copy()


def _attach_branch_ids_to_catchments(
    catchments: gpd.GeoDataFrame,
    streams: gpd.GeoDataFrame,
    *,
    reach_id_attribute: str,
    branch_id_attribute: str,
) -> gpd.GeoDataFrame:
    """Tag every catchment with the level path of the reach it drains."""
    stream_lookup = streams[[reach_id_attribute, branch_id_attribute]].copy()
    stream_lookup[reach_id_attribute] = stream_lookup[reach_id_attribute].astype(str)

    merged = catchments.merge(
        stream_lookup,
        how="inner",
        left_on="_reach_key",
        right_on=reach_id_attribute,
    )
    return merged.drop(columns="_reach_key")


def _dissolve_levelpaths(
    streams: gpd.GeoDataFrame,
    branch_id_attribute: str,
    *,
    clip_boundary: gpd.GeoDataFrame,
    waterbodies: gpd.GeoDataFrame,
    huc_boundary: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    dissolved = (
        streams[[branch_id_attribute, "geometry"]]
        .dissolve(by=branch_id_attribute)
        .reset_index()
    )

    if not clip_boundary.empty:
        dissolved = gpd.clip(dissolved, clip_boundary)

    if not waterbodies.empty:
        keep_mask = ~dissolved.geometry.within(waterbodies.union_all())
        dissolved = dissolved.loc[keep_mask].copy()

    if not huc_boundary.empty:
        dissolved = gpd.sjoin(
            dissolved, huc_boundary[["geometry"]], predicate="intersects", how="inner"
        )
        dissolved = dissolved.drop(
            columns=[col for col in dissolved.columns if col.startswith("index_")]
        )

    return dissolved.reset_index(drop=True)


def _build_headwaters(
    streams: gpd.GeoDataFrame,
    *,
    branch_id_attribute: str,
    reach_id_attribute: str,
    provided_headwaters: Optional[gpd.GeoDataFrame],
) -> gpd.GeoDataFrame:
    to_nodes = set(streams["_to_node"])
    upstream_start_rows = streams.loc[~streams["_from_node"].isin(to_nodes)].copy()

    # One headwater per level path: the upstream-most vertex of its
    # upstream-start reach. Taking the point from the level-path geometry
    # guarantees it lies ON the line and inside the per-branch DEM, which is
    # what flow accumulation seeds from — a seed outside the DEM gives
    # 0 stream cells and crashes StreamNetReaches.
    #
    # ``provided_headwaters`` (external NWM inventory), if given, only annotates
    # snap distance for diagnostics — it never sets geometry or drops a branch.
    headwater_points = []
    for _, row in upstream_start_rows.iterrows():
        headwater_points.append(
            {
                reach_id_attribute: str(row[reach_id_attribute]),
                branch_id_attribute: str(row[branch_id_attribute]),
                "geometry": Point(row["_inlet_x"], row["_inlet_y"]),
            }
        )
    headwaters = gpd.GeoDataFrame(
        headwater_points, crs=streams.crs, geometry="geometry"
    )

    if provided_headwaters is None or provided_headwaters.empty:
        return headwaters

    # Diagnostic-only: distance from each inlet vertex to the nearest provided
    # NWM headwater point. Large values flag inlets the NWM inventory doesn't
    # corroborate, but every branch is still kept.
    try:
        points = provided_headwaters.to_crs(streams.crs)
        nearest = gpd.sjoin_nearest(
            headwaters,
            points[["geometry"]],
            how="left",
            distance_col="_snap_dist",
        )
        nearest = nearest.drop(
            columns=[c for c in nearest.columns if c.startswith("index_")]
        )
        # sjoin_nearest can duplicate rows on ties — collapse back to one row
        # per inlet reach, keeping the closest match.
        nearest = (
            nearest.sort_values("_snap_dist")
            .drop_duplicates(subset=[reach_id_attribute])
            .sort_index()
        )
        headwaters = nearest.reset_index(drop=True)
    except Exception as exc:  # diagnostics must never break headwater derivation
        log.warning("Headwater snap-distance annotation skipped: %s", exc)

    return headwaters


def _associate_levelpaths_with_levees(
    levees_path: Path,
    leveed_areas_path: Path,
    dissolved_levelpaths: gpd.GeoDataFrame,
    branch_id_attribute: str,
    levee_id_attribute: str,
    levee_buffer: float,
    out_path: Path,
) -> bool:
    """
    Associate each levee system with the level paths it protects, writing the
    mapping to CSV.
    Returns True if the CSV was written, False if no associations were found.
    """
    levees = gpd.read_file(levees_path, engine="pyogrio")
    leveed_areas = gpd.read_file(leveed_areas_path, engine="pyogrio")
    levelpaths = dissolved_levelpaths[[branch_id_attribute, "geometry"]].copy()

    levees = _align_crs(levees, levelpaths.crs)
    leveed_areas = _align_crs(leveed_areas, levelpaths.crs)

    # Ensure leveed_areas has the same ID column as levees (levee_id_attribute).
    # NLD raw data uses SYSTEM_ID in levees and LEVEED_ID in leveed_areas; the
    # overlay logic below requires both to share the same column name so that
    # geopandas suffixes it _1 (from left/levees) and _2 (from right/leveed_areas).
    if levee_id_attribute not in leveed_areas.columns:
        # look for a plausible candidate: LEVEED_ID, then any *_ID column
        candidates = [
            c for c in leveed_areas.columns if c.upper() in ("LEVEED_ID", "SYSTEM_ID")
        ]
        if not candidates:
            candidates = [c for c in leveed_areas.columns if c.upper().endswith("_ID")]
        if candidates:
            leveed_areas = leveed_areas.rename(
                columns={candidates[0]: levee_id_attribute}
            )
            log.debug(
                "_associate_levelpaths_with_levees: renamed leveed_areas column %s --> %s",
                candidates[0],
                levee_id_attribute,
            )
        else:
            log.warning(
                "_associate_levelpaths_with_levees: leveed_areas has no column matching "
                "levee_id_attribute=%r and no fallback — skipping levee association",
                levee_id_attribute,
            )
            return False

    # buffer each side of levee line
    levees_buffered_left = levees.copy()
    levees_buffered_right = levees.copy()
    levees_buffered_left.geometry = levees.buffer(levee_buffer, single_sided=True)
    levees_buffered_right.geometry = levees.buffer(-levee_buffer, single_sided=True)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        leveed_left = gpd.overlay(
            levees_buffered_left, leveed_areas, how="intersection"
        )
        leveed_right = gpd.overlay(
            levees_buffered_right, leveed_areas, how="intersection"
        )

    leveed_intersected: list = []

    if not leveed_left.empty:
        leveed_intersected.extend(leveed_left[f"{levee_id_attribute}_1"].values)
        matches = np.where(
            leveed_left[f"{levee_id_attribute}_1"]
            == leveed_left[f"{levee_id_attribute}_2"]
        )[0]
        leveed_left = leveed_left.loc[matches].copy()
        leveed_left["leveed_area"] = leveed_left.area
        leveed_left = leveed_left[
            [f"{levee_id_attribute}_1", "leveed_area", "geometry"]
        ]

    if not leveed_right.empty:
        leveed_intersected.extend(leveed_right[f"{levee_id_attribute}_1"].values)
        matches = np.where(
            leveed_right[f"{levee_id_attribute}_1"]
            == leveed_right[f"{levee_id_attribute}_2"]
        )[0]
        leveed_right = leveed_right.loc[matches].copy()
        leveed_right["leveed_area"] = leveed_right.area
        leveed_right = leveed_right[
            [f"{levee_id_attribute}_1", "leveed_area", "geometry"]
        ]

    levees_not_found = gpd.GeoDataFrame()
    if leveed_intersected:
        levees_not_found = leveed_areas[
            ~leveed_areas[levee_id_attribute].isin(leveed_intersected)
        ].copy()

    if leveed_left.empty and leveed_right.empty:
        return False

    if not leveed_left.empty and not leveed_right.empty:
        leveed = leveed_left.merge(
            leveed_right,
            on=f"{levee_id_attribute}_1",
            how="outer",
            suffixes=["_left", "_right"],
        )
        leveed.loc[np.isnan(leveed["leveed_area_left"]), "leveed_area_left"] = 0.0
        leveed.loc[np.isnan(leveed["leveed_area_right"]), "leveed_area_right"] = 0.0
    elif leveed_left.empty:
        leveed = leveed_right.rename(columns={"leveed_area": "leveed_area_right"})
        leveed["leveed_area_left"] = 0.0
    else:
        leveed = leveed_left.rename(columns={"leveed_area": "leveed_area_left"})
        leveed["leveed_area_right"] = 0.0

    leveed["levee_side"] = np.where(
        leveed["leveed_area_left"] < leveed["leveed_area_right"], "left", "right"
    )
    left_ids = leveed.loc[leveed["levee_side"] == "left", f"{levee_id_attribute}_1"]
    right_ids = leveed.loc[leveed["levee_side"] == "right", f"{levee_id_attribute}_1"]

    levee_levelpaths_left = gpd.sjoin(levees_buffered_left, levelpaths)
    levee_levelpaths_right = gpd.sjoin(levees_buffered_right, levelpaths)
    # geopandas appends _left/_1 suffix when both frames share a column name; normalise back
    levee_levelpaths_left = _normalise_sjoin_col(
        levee_levelpaths_left, levee_id_attribute
    )
    levee_levelpaths_right = _normalise_sjoin_col(
        levee_levelpaths_right, levee_id_attribute
    )
    levee_levelpaths_left = levee_levelpaths_left[
        [levee_id_attribute, branch_id_attribute]
    ]
    levee_levelpaths_right = levee_levelpaths_right[
        [levee_id_attribute, branch_id_attribute]
    ]
    levee_levelpaths_left = levee_levelpaths_left[
        levee_levelpaths_left[levee_id_attribute].isin(left_ids)
    ]
    levee_levelpaths_right = levee_levelpaths_right[
        levee_levelpaths_right[levee_id_attribute].isin(right_ids)
    ]

    out_df = (
        pd.concat(
            [
                levee_levelpaths_right[[levee_id_attribute, branch_id_attribute]],
                levee_levelpaths_left[[levee_id_attribute, branch_id_attribute]],
            ]
        )
        .drop_duplicates()
        .reset_index(drop=True)
    )

    if not levees_not_found.empty:
        levees_not_found = levees_not_found.copy()
        levees_not_found.geometry = levees_not_found.buffer(2 * levee_buffer)
        levees_not_found = _normalise_sjoin_col(
            gpd.sjoin(levees_not_found, levelpaths), levee_id_attribute
        )
        out_df = (
            pd.concat(
                [
                    out_df[[levee_id_attribute, branch_id_attribute]],
                    levees_not_found[[levee_id_attribute, branch_id_attribute]],
                ]
            )
            .drop_duplicates()
            .reset_index(drop=True)
        )

    # remove levelpaths that cross a levee exactly once (they aren't truly blocked)
    drop_indices = []
    for j, row in out_df.iterrows():
        levee_geom = levees[levees[levee_id_attribute] == row[levee_id_attribute]]
        lp_geom = levelpaths[
            levelpaths[branch_id_attribute] == row[branch_id_attribute]
        ]
        intersections = gpd.overlay(
            levee_geom, lp_geom, how="intersection", keep_geom_type=False
        ).explode(index_parts=True)
        intersections = intersections[intersections.geom_type == "Point"]
        if len(intersections) == 1:
            drop_indices.append(j)
        elif intersections.empty:
            leveed_area_check = gpd.overlay(
                lp_geom,
                leveed_areas[
                    leveed_areas[levee_id_attribute] == row[levee_id_attribute]
                ],
                how="intersection",
                keep_geom_type=False,
            )
            if not leveed_area_check.empty:
                drop_indices.append(j)

    out_df = out_df.drop(index=drop_indices)
    if out_df.empty:
        return False

    out_df.to_csv(
        out_path, columns=[levee_id_attribute, branch_id_attribute], index=False
    )
    return True


def _build_branch_polygons(
    dissolved_levelpaths: gpd.GeoDataFrame,
    branch_id_attribute: str,
    buffer_distance_meters: float,
    buffered_boundary: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    polygons = dissolved_levelpaths[[branch_id_attribute, "geometry"]].copy()
    polygons["geometry"] = polygons.geometry.buffer(buffer_distance_meters)
    polygons = gpd.GeoDataFrame(
        polygons, crs=dissolved_levelpaths.crs, geometry="geometry"
    )
    if not buffered_boundary.empty:
        polygons = gpd.clip(polygons, buffered_boundary)
    return polygons.reset_index(drop=True)
