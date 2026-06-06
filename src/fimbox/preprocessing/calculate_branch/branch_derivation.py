"""
Author: Supath Dhital
Date Updated : May 2026

branch-derivation helpers for area inputs.
1. derive level paths from staged streams/catchments/lakes
2. buffer dissolved branches into processing polygons
3. write a branch list file
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import logging
import warnings
from pathlib import Path
from typing import Iterable, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from ..source_naming import detect_identifier, resolve_source, source_name

gpd.options.io_engine = "pyogrio"

log = logging.getLogger(__name__)


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
    ) -> None:
        self.out_dir = Path(out_dir).expanduser().resolve()
        self.area_id = area_id
        self.branch_id_attribute = branch_id_attribute
        self.reach_id_attribute = reach_id_attribute
        self.catchment_reach_id_attribute = catchment_reach_id_attribute
        self.stream_order_attribute = stream_order_attribute
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

        levees_path: Optional[Path] = (
            Path(self.levees_override).resolve()
            if self.levees_override
            else (discovered.levees if discovered is not None else None)
        )
        leveed_areas_path: Optional[Path] = (
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
        streams = _derive_network_fields(streams)
        streams = _assign_levelpaths(
            streams,
            reach_id_attribute=self.reach_id_attribute,
            stream_order_attribute=self.stream_order_attribute,
            branch_id_attribute=self.branch_id_attribute,
        )
        self._announce("Levelpaths derived")

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
        lakes_gdf = _align_crs(lakes_gdf, streams.crs)
        boundary_gdf = _align_crs(boundary_gdf, streams.crs)
        buffered_boundary_gdf = _align_crs(buffered_boundary_gdf, streams.crs)
        buffered_stream_boundary_gdf = _align_crs(
            buffered_stream_boundary_gdf, streams.crs
        )

        # Drop level paths with no catchment (matches inundation-mapping's
        # remove_branches_without_catchments). Done before
        # dissolve/headwaters/polygons so a catchment-less branch never reaches
        # any output and never seeds an empty per-branch DEM.
        streams = _remove_branches_without_catchments(
            streams,
            catchments_gdf,
            reach_id_attribute=self.reach_id_attribute,
            branch_id_attribute=self.branch_id_attribute,
            catchment_reach_id_attribute=self.catchment_reach_id_attribute,
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
            catchment_reach_id_attribute=self.catchment_reach_id_attribute,
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
        else:
            self.logger.info("Levees --> not provided; skipping levee association")

        self.logger.info("=== BRANCH DERIVATION COMPLETE ===")
        self._announce("Branch derivation complete")

        return result

    def _setup_logger(self) -> logging.Logger:
        # Attach the shared file+stream handlers to the fimbox root so this
        # stage's logs appear in the same preprocess.log as the rest of the
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
    branch_id_attribute: str = "levpa_id",
    reach_id_attribute: str = "ID",
    catchment_reach_id_attribute: str = "ID",
    stream_order_attribute: str = "order_",
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


def _derive_network_fields(streams: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    streams = streams.copy()
    from_nodes: list[str] = []
    to_nodes: list[str] = []
    lengths_km: list[float] = []

    for idx, geom in enumerate(streams.geometry):
        if geom is None or geom.is_empty:
            raise ValueError(f"Stream geometry at row {idx} is empty.")
        coords = list(geom.coords)
        start = coords[0]
        end = coords[-1]
        from_nodes.append(f"{round(start[0], 6)}_{round(start[1], 6)}")
        to_nodes.append(f"{round(end[0], 6)}_{round(end[1], 6)}")
        lengths_km.append(float(geom.length) / 1000.0)

    streams["_from_node"] = from_nodes
    streams["_to_node"] = to_nodes
    streams["_length_km"] = lengths_km
    return streams


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

    def arbolate_sum(reach_id: str) -> float:
        if reach_id in arbolate_cache:
            return arbolate_cache[reach_id]
        total = reach_lengths.get(reach_id, 0.0)
        total += sum(arbolate_sum(up_id) for up_id in upstreams.get(reach_id, []))
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
            # Mainstem selection (matches inundation-mapping's
            # derive_stream_branches): pick the upstream reach with the highest
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


def _remove_branches_without_catchments(
    streams: gpd.GeoDataFrame,
    catchments: gpd.GeoDataFrame,
    *,
    reach_id_attribute: str,
    branch_id_attribute: str,
    catchment_reach_id_attribute: str,
) -> gpd.GeoDataFrame:
    """Drop level paths whose reaches have no matching catchment.

    Port of inundation-mapping ``StreamNetwork.remove_branches_without_catchments``:
    a whole branch is removed only when *none* of its reach ids appear in the
    catchment layer, so a branch with at least one catchment is kept intact.
    """
    if catchment_reach_id_attribute not in catchments.columns:
        raise KeyError(
            f"Catchment reach id column '{catchment_reach_id_attribute}' was not found in catchments."
        )
    catchment_reach_ids = set(
        catchments[catchment_reach_id_attribute].astype(str).unique()
    )

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
    catchment_reach_id_attribute: str,
    reach_id_attribute: str,
    branch_id_attribute: str,
) -> gpd.GeoDataFrame:
    if catchment_reach_id_attribute not in catchments.columns:
        raise KeyError(
            f"Catchment reach id column '{catchment_reach_id_attribute}' was not found in catchments."
        )
    stream_lookup = streams[[reach_id_attribute, branch_id_attribute]].copy()
    stream_lookup[reach_id_attribute] = stream_lookup[reach_id_attribute].astype(str)

    catchments = catchments.copy()
    catchments[catchment_reach_id_attribute] = catchments[
        catchment_reach_id_attribute
    ].astype(str)
    return catchments.merge(
        stream_lookup,
        how="inner",
        left_on=catchment_reach_id_attribute,
        right_on=reach_id_attribute,
    )


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
    # upstream-start reach (matches inundation-mapping's
    # derive_headwater_points_with_inlets). Taking the point from the level-path
    # geometry guarantees it lies ON the line and inside the per-branch DEM,
    # which is what flow accumulation seeds from — a seed outside the DEM gives
    # 0 stream cells and crashes StreamNetReaches.
    #
    # ``provided_headwaters`` (external NWM inventory), if given, only annotates
    # snap distance for diagnostics — it never sets geometry or drops a branch.
    headwater_points = []
    for _, row in upstream_start_rows.iterrows():
        first_coord = list(row.geometry.coords)[0]
        headwater_points.append(
            {
                reach_id_attribute: str(row[reach_id_attribute]),
                branch_id_attribute: str(row[branch_id_attribute]),
                "geometry": Point(first_coord),
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
    Port of inundation-mapping associate_levelpaths_with_levees.py.
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
