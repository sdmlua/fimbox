"""
Author: Supath Dhital
Date Updated: May 2026

Shared helpers, dtype maps, and the not-yet-ported sentinel used across
the calibrate_ratingcurve subpackage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]


class CalibrationNotImplemented(NotImplementedError):
    # Raised by stub routines that have not been ported yet.
    pass


def not_yet_ported(step_name: str) -> None:
    raise CalibrationNotImplemented(
        f"{step_name} has not been ported to fimbox yet. "
        f"Enable skip_unimplemented=True on CalibrationConfig to bypass."
    )


def resolve_aoi_dir(
    aoi_dir: Optional[PathLike] = None, huc_dir: Optional[PathLike] = None
) -> Path:
    # Accept either aoi_dir= or huc_dir= but not both with different values.
    if aoi_dir is not None and huc_dir is not None and aoi_dir != huc_dir:
        raise TypeError(
            f"Pass aoi_dir= or huc_dir=, not both with different values "
            f"({aoi_dir!r} vs {huc_dir!r})."
        )
    chosen = aoi_dir if aoi_dir is not None else huc_dir
    if chosen is None:
        raise TypeError("Either aoi_dir= or huc_dir= must be provided.")
    return Path(chosen)


def iter_branches(aoi_dir: Path, *, exclude_zero: bool = True):
    # Yield (branch_id, branch_dir) for every subdirectory under
    # <aoi_dir>/branches. Zero branch optionally excluded since several
    # routines treat it separately.
    branches_root = aoi_dir / "branches"
    if not branches_root.is_dir():
        return
    for bp in sorted(branches_root.iterdir()):
        if not bp.is_dir():
            continue
        bid = bp.name
        if exclude_zero and bid == "0":
            continue
        yield bid, bp


# Column dtype maps reused by the aggregator and several SRC routines.
USGS_DTYPES = {
    "location_id": str,
    "nws_lid": str,
    "feature_id": int,
    "HydroID": int,
    "levpa_id": str,
    "dem_elevation": float,
    "dem_adj_elevation": float,
    "order_": str,
    "LakeID": object,
    "HUC8": str,
    "snap_distance": float,
}
RAS_DTYPES = dict(USGS_DTYPES)

HYDROTABLE_DTYPES = {
    "HydroID": int,
    "branch_id": int,
    "feature_id": int,
    "NextDownID": int,
    "order_": int,
    "Number of Cells": int,
    "SurfaceArea (m2)": float,
    "BedArea (m2)": float,
    "TopWidth (m)": float,
    "LENGTHKM": float,
    "AREASQKM": float,
    "WettedPerimeter (m)": float,
    "HydraulicRadius (m)": float,
    "WetArea (m2)": float,
    "Volume (m3)": float,
    "SLOPE": float,
    "ManningN": float,
    "stage": float,
    "default_discharge_cms": float,
    "default_Volume (m3)": float,
    "default_WetArea (m2)": float,
    "default_HydraulicRadius (m)": float,
    "default_ManningN": float,
    "calb_applied": bool,
    "last_updated": str,
    "submitter": str,
    "obs_source": str,
    "precalb_discharge_cms": float,
    "calb_coef_usgs": float,
    "calb_coef_spatial": float,
    "calb_coef_final": float,
    "HUC": int,
    "LakeID": int,
    "subdiv_applied": bool,
    "channel_n": float,
    "overbank_n": float,
    "subdiv_discharge_cms": float,
    "discharge_cms": float,
}

SRC_CROSS_DTYPES = {
    "branch_id": int,
    "HydroID": int,
    "feature_id": int,
    "Stage": float,
    "Number of Cells": int,
    "SurfaceArea (m2)": float,
    "BedArea (m2)": float,
    "Volume (m3)": float,
    "SLOPE": float,
    "LENGTHKM": float,
    "AREASQKM": float,
    "ManningN": float,
    "NextDownID": int,
    "order_": int,
    "TopWidth (m)": float,
    "WettedPerimeter (m)": float,
    "WetArea (m2)": float,
    "HydraulicRadius (m)": float,
    "Discharge (m3s-1)": float,
    "bankfull_flow": float,
    "Stage_bankfull": float,
    "BedArea_bankfull": float,
    "Volume_bankfull": float,
    "HRadius_bankfull": float,
    "SurfArea_bankfull": float,
    "bankfull_proxy": str,
    "Volume_chan (m3)": float,
    "BedArea_chan (m2)": float,
    "WettedPerimeter_chan (m)": float,
    "Volume_obank (m3)": float,
    "BedArea_obank (m2)": float,
    "WettedPerimeter_obank (m)": float,
    "channel_n": float,
    "overbank_n": float,
    "subdiv_applied": bool,
    "WetArea_chan (m2)": float,
    "HydraulicRadius_chan (m)": float,
    "Discharge_chan (m3s-1)": float,
    "Velocity_chan (m/s)": float,
    "WetArea_obank (m2)": float,
    "HydraulicRadius_obank (m)": float,
    "Discharge_obank (m3s-1)": float,
    "Velocity_obank (m/s)": float,
    "Discharge (m3s-1)_subdiv": float,
}

BRIDGE_DTYPES = {
    "osmid": int,
    "name": str,
    "threshold_hand": float,
    "threshold_hand_75": float,
    "has_lidar_tif": str,
    "feature_id": int,
    "HydroID": int,
    "order_": str,
    "branch": str,
    "mainstem": int,
    "geometry": object,
}

ROAD_DTYPES = {
    "osmid": str,
    "highway": str,
    "name": str,
    "huc8": str,
    "osmid_catchid": str,
    "HydroID": str,
    "feature_id": str,
    "order_": str,
    "branch": str,
    "threshold_hand": float,
    "threshold_discharge": float,
    "threshold_hand_ft": float,
    "threshold_discharge_cfs": float,
}


# Geometry variable names used by the SRC routines.
HRADIUS_VAR = "HydraulicRadius (m)"
VOLUME_VAR = "Volume (m3)"
SURFACE_AREA_VAR = "SurfaceArea (m2)"
BEDAREA_VAR = "BedArea (m2)"
