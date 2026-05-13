"""
Author: Supath Dhital
Date Updated: May 2026

Filter catchment polygons and split reaches, then attach flow attributes.

Steps:
  1. Optionally clip to reaches whose HydroID prefix matches a node ID inside
     the AOI boundary file. When the boundary file is absent all reaches are kept.
  2. Drop isolated tiny outlet stubs: reaches with NextDownID == -1,
     no upstream branch, and LengthKm < 0.02 km.
  3. Drop sub-metre reaches (LengthKm < 0.001 km).
  4. Join filtered reaches onto catchment polygons on HydroID.
  5. Remove smaller duplicate catchment polygons for the same HydroID.
  6. Add areasqkm column.

Inputs
------
catchments_gpkg  : gw_catchments_reaches_{id}.gpkg  (polygonised from raster)
flows_gpkg       : demDerived_reaches_split_{id}.gpkg
aoi_code         : AOI identifier used to look up matching boundary rows
                   (e.g. an 8-digit HUC for HUC-based AOIs, or any user-defined
                   code that appears in the boundary attribute table).
boundary_gpkg    : optional AOI boundary clip file. The first column whose name
                   matches one of {HUC8, HUC_attribute, HUC, aoi_code, AOI, code}
                   is used to filter rows by ``aoi_code``.

Outputs
-------
out_catchments   : gw_catchments_reaches_filtered_addedAttributes_{id}.gpkg
out_flows        : demDerived_reaches_split_filtered_{id}.gpkg
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np

log = logging.getLogger(__name__)

_MIN_LENGTH_TINY_KM = 0.02    # tiny isolated outlet stub threshold
_MIN_LENGTH_KM      = 0.001   # absolute minimum reach length (sub-metre)


@dataclass
class FilterCatchments:
    """
    Filter catchment polygons and split reaches, then attach flow attributes.

    Parameters
    ----------
    catchments_gpkg : gw_catchments_reaches_{id}.gpkg  (polygon layer)
    flows_gpkg      : demDerived_reaches_split_{id}.gpkg
    out_catchments  : output filtered catchment polygons (.gpkg)
    out_flows       : output filtered reaches (.gpkg)
    aoi_code        : AOI identifier used to filter the boundary file when
                      provided. Pass any user-defined code; HUC8 strings work
                      unchanged.
    boundary_gpkg   : optional AOI boundary file; when absent all reaches are kept.
                      (Alias kept: ``wbd8_clp_gpkg``.)
    min_length_km   : sub-metre reach cutoff (default 0.001 km).
    min_tiny_stub_km: isolated-outlet-stub cutoff (default 0.02 km).
    """

    catchments_gpkg: Path
    flows_gpkg: Path
    out_catchments: Path
    out_flows: Path
    aoi_code: str = ""
    boundary_gpkg: Optional[Path] = None
    min_length_km: float = _MIN_LENGTH_KM
    min_tiny_stub_km: float = _MIN_LENGTH_TINY_KM

    def __post_init__(self):
        for attr in ("catchments_gpkg", "flows_gpkg", "out_catchments", "out_flows"):
            setattr(self, attr, Path(getattr(self, attr)))
        if self.boundary_gpkg is not None:
            self.boundary_gpkg = Path(self.boundary_gpkg)
        self.out_catchments.parent.mkdir(parents=True, exist_ok=True)

    def run(self) -> tuple[Path, Path]:
        """
        Execute filtering. Returns (out_catchments, out_flows).
        Raises NoFlowlinesError when no reaches survive filtering.
        """
        if (
            self.out_catchments.exists() and self.out_catchments.stat().st_size > 0
            and self.out_flows.exists()  and self.out_flows.stat().st_size > 0
        ):
            log.info("FilterCatchments: outputs exist, skipping")
            return self.out_catchments, self.out_flows

        log.info("FilterCatchments: reading inputs")
        catchments = gpd.read_file(str(self.catchments_gpkg), engine="fiona")
        flows      = gpd.read_file(str(self.flows_gpkg),      engine="fiona")

        flows = _filter_by_aoi(flows, self.boundary_gpkg, self.aoi_code)
        flows = _drop_tiny_outlet_stubs(flows, self.min_tiny_stub_km)
        flows = flows[flows["LengthKm"] > self.min_length_km].copy()
        log.info("FilterCatchments: %d reaches after length filter", len(flows))

        if len(flows) == 0:
            raise NoFlowlinesError(
                f"No flowlines remain after filtering for AOI {self.aoi_code!r}"
            )

        # join filtered reaches onto catchment polygons
        if catchments["HydroID"].dtype != int:
            catchments["HydroID"] = catchments["HydroID"].astype(int)
        if flows["HydroID"].dtype != int:
            flows["HydroID"] = flows["HydroID"].astype(int)

        out_catchments = catchments.merge(
            flows.drop(columns=["geometry"]), on="HydroID"
        )

        out_catchments = _drop_smaller_duplicates(out_catchments)
        out_catchments["areasqkm"] = out_catchments.geometry.area / 1e6

        if out_catchments.empty:
            raise NoFlowlinesError(
                f"No catchments remain after join for AOI {self.aoi_code!r}"
            )

        log.info(
            "FilterCatchments: writing %d catchments, %d reaches",
            len(out_catchments), len(flows),
        )

        for p in (self.out_catchments, self.out_flows):
            if p.exists():
                p.unlink()

        out_catchments.to_file(
            str(self.out_catchments), driver="GPKG", index=False, engine="fiona"
        )
        flows.to_file(
            str(self.out_flows), driver="GPKG", index=False, engine="fiona"
        )

        return self.out_catchments, self.out_flows


class NoFlowlinesError(RuntimeError):
    """Raised when no flowlines survive the AOI / length filters."""


# internal helpers

_AOI_CODE_COLUMNS = ("HUC8", "HUC_attribute", "huc8", "HUC", "aoi_code", "AOI", "code")


def _filter_by_aoi(
    flows: gpd.GeoDataFrame,
    boundary_path: Optional[Path],
    aoi_code: str,
) -> gpd.GeoDataFrame:
    """
    Keep only reaches whose HydroID prefix matches a node ID inside the AOI
    boundary file for this ``aoi_code``. The boundary file column holding the
    code is auto-detected from common names (see ``_AOI_CODE_COLUMNS``). When
    the file is missing, the column is missing, or no rows match, all reaches
    are returned unchanged.
    """
    if boundary_path is None or not boundary_path.exists():
        log.debug("FilterCatchments: no boundary file — keeping all %d reaches", len(flows))
        return flows.copy()
    if not aoi_code:
        log.debug("FilterCatchments: no aoi_code — keeping all reaches")
        return flows.copy()

    boundary = gpd.read_file(str(boundary_path), engine="fiona")

    code_col = next((c for c in _AOI_CODE_COLUMNS if c in boundary.columns), None)
    if code_col is None:
        log.warning("FilterCatchments: boundary file has no recognised code column — keeping all reaches")
        return flows.copy()

    if "HydroID" not in boundary.columns:
        log.debug("FilterCatchments: boundary file has no HydroID column — keeping all reaches")
        return flows.copy()

    select_ids = tuple(
        str(int(v))
        for v in boundary.loc[boundary[code_col].astype(str).str.contains(aoi_code), "HydroID"]
    )
    if not select_ids:
        log.debug("FilterCatchments: AOI filter produced no IDs — keeping all reaches")
        return flows.copy()

    mask = flows["HydroID"].astype(str).str.startswith(select_ids)
    result = flows[mask].copy()
    log.debug("FilterCatchments: AOI filter kept %d / %d reaches", len(result), len(flows))
    return result


def _drop_tiny_outlet_stubs(
    flows: gpd.GeoDataFrame,
    min_tiny_km: float = _MIN_LENGTH_TINY_KM,
) -> gpd.GeoDataFrame:
    """
    Remove reaches that drain to nothing (NextDownID == -1), have no upstream
    branch, and are shorter than ``min_tiny_km``. These are isolated stub
    artifacts at the network outlet.
    """
    if "NextDownID" not in flows.columns or "LengthKm" not in flows.columns:
        return flows

    hydro_ids   = flows["HydroID"].astype(int)
    next_dn_ids = flows["NextDownID"].astype(int)

    referenced_as_downstream = set(next_dn_ids.values)

    drop_mask = (
        (next_dn_ids == -1)
        & (~hydro_ids.isin(referenced_as_downstream))
        & (flows["LengthKm"] < min_tiny_km)
    )
    n_dropped = int(drop_mask.sum())
    if n_dropped:
        log.debug("FilterCatchments: dropping %d tiny isolated outlet stubs", n_dropped)

    return flows[~drop_mask].copy()


def _drop_smaller_duplicates(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    When the same HydroID appears more than once (can occur at HUC boundary
    overlap after the spatial join), keep only the largest polygon.
    """
    counts = gdf["HydroID"].value_counts()
    dup_ids = counts[counts > 1].index.tolist()

    if not dup_ids:
        return gdf

    hids = gdf["HydroID"].values
    drop_indices: list[int] = []
    for hid in dup_ids:
        idx   = np.where(hids == hid)[0]
        areas = gdf.iloc[idx].geometry.area.values
        drop_indices.extend(idx[areas != areas.max()].tolist())

    return gdf.drop(gdf.index[drop_indices]).copy()
