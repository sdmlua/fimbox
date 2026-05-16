"""
Author: Supath Dhital
Date Updated: May 2026

Reset per-branch ``hydroTable_{id}.csv`` and ``src_full_crosswalked_{id}.csv``
back to the un-calibrated baseline by recomputing Manning's equation from the
default SLOPE and default ManningN that were saved during ``build_src_base``.

This is used at the start of a calibration **rerun** so each rerun starts
from the same baseline rather than stacking on top of the previous run's
calibrations.

Math
----
For each (HydroID, Stage) row in src_base:

    R   = WetArea / WettedPerimeter           (hydraulic radius)
    Q   = WetArea * R^(2/3) * SLOPE^(1/2) / ManningN

Then ``Discharge (m3s-1)`` is copied into ``discharge_cms`` and
``default_discharge_cms`` of the hydroTable.

Note: when the AOI identifier (or USGS HUC code) starts with "19" (Alaska) apply a Small Segment fix that copies
discharges from the parent reach into the short reach, matching
inundation-mapping's Alaska-specific path.

Inputs (per branch)
-------------------
src_base_{id}.csv               - reach geometry table (no SLOPE/ManningN updates)
src_full_crosswalked_{id}.csv   - crosswalked SRC (contains default_SLOPE, default_ManningN)
hydroTable_{id}.csv             - AOI-wide rating curve (column schema preserves the inundation-mapping "HUC" key)
small_segments_{id}.csv         - optional short-reach update list

Outputs (overwritten in place)
------------------------------
src_full_crosswalked_{id}.csv   - SLOPE/ManningN reset to default_*, Discharge recomputed
hydroTable_{id}.csv             - discharge_cms / default_discharge_cms recomputed
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

import pandas as pd

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


SRC_FULL_PRESERVE_COLUMNS = [
    "SLOPE_RISE_RUN", "ManningN", "HydroID", "NextDownID", "order_",
    "SLOPE_HFAB", "SLOPE_IRIS_SWORD", "SLOPE", "feature_id", "Stage",
    "Number of Cells", "SurfaceArea (m2)", "LENGTHKM", "AREASQKM",
    "Volume (m3)", "BedArea (m2)", "TopWidth (m)", "WettedPerimeter (m)",
    "WetArea (m2)", "HydraulicRadius (m)", "Discharge (m3s-1)",
    "Bathymetry_source", "default_SLOPE", "default_ManningN",
]


def reset_branch(branch_dir: PathLike, branch_id: str, aoi_id: str) -> None:
    """Recompute discharge for one branch's hydroTable + src_full_crosswalked."""
    branch_dir = Path(branch_dir)
    src_base = branch_dir / f"src_base_{branch_id}.csv"
    src_full = branch_dir / f"src_full_crosswalked_{branch_id}.csv"
    htable = branch_dir / f"hydroTable_{branch_id}.csv"
    small_segs = branch_dir / f"small_segments_{branch_id}.csv"

    if not all(p.is_file() for p in (src_base, src_full, htable)):
        return  # branch may have failed earlier; silently skip

    base = pd.read_csv(src_base, dtype={"CatchId": str})
    full = pd.read_csv(src_full)
    h = pd.read_csv(htable, dtype={"HydroID": str})

    for df in (base, full, h):
        df.columns = df.columns.str.strip()

    for col in ("HydroID", "NextDownID", "feature_id"):
        if col in full.columns:
            full[col] = full[col].astype(str)

    # numeric coercion for src_base columns other than IDs
    numeric_cols = [c for c in base.columns if c not in ("CatchId", "HydroID", "NextDownID")]
    base[numeric_cols] = base[numeric_cols].apply(pd.to_numeric, errors="coerce")

    base = base.drop(columns=["SLOPE"]).rename(columns={"CatchId": "HydroID"})
    recalc = base.merge(
        full[["HydroID", "Stage", "default_SLOPE", "default_ManningN", "NextDownID", "order_"]],
        on=["HydroID", "Stage"],
    )
    if recalc.empty:
        log.warning(f"reset_htable_src: merge failed for branch {branch_id}")
        return

    recalc = recalc.rename(columns={"default_SLOPE": "SLOPE", "default_ManningN": "ManningN"})

    # geometric properties
    recalc["TopWidth (m)"] = recalc["SurfaceArea (m2)"] / recalc["LENGTHKM"] / 1000
    recalc["WettedPerimeter (m)"] = recalc["BedArea (m2)"] / recalc["LENGTHKM"] / 1000
    recalc["WetArea (m2)"] = recalc["Volume (m3)"] / recalc["LENGTHKM"] / 1000
    recalc["HydraulicRadius (m)"] = (
        recalc["WetArea (m2)"] / recalc["WettedPerimeter (m)"]
    ).fillna(0)

    # Manning equation
    recalc["Discharge (m3s-1)"] = (
        recalc["WetArea (m2)"]
        * recalc["HydraulicRadius (m)"] ** (2 / 3)
        * recalc["SLOPE"] ** 0.5
        / recalc["ManningN"]
    )
    recalc.loc[recalc["Stage"] == 0, "Discharge (m3s-1)"] = 0

    if small_segs.is_file():
        recalc = _apply_small_segments(recalc, small_segs, aoi_id)

    # Merge back into src_full and hydroTable
    full = full.set_index(["HydroID", "Stage"])
    recalc_indexed = recalc.set_index(["HydroID", "Stage"])
    full.update(recalc_indexed)
    full = full.reset_index()
    full["Bathymetry_source"] = pd.NA

    h = h.merge(
        recalc_indexed[["Discharge (m3s-1)"]].reset_index(),
        left_on=["HydroID", "stage"],
        right_on=["HydroID", "Stage"],
        how="left",
    )
    h["discharge_cms"] = h["Discharge (m3s-1)"]
    h["default_discharge_cms"] = h["Discharge (m3s-1)"]
    h["subdiv_discharge_cms"] = pd.NA
    h = h.drop(columns=["Discharge (m3s-1)", "Stage"], errors="ignore")

    preserved = [c for c in SRC_FULL_PRESERVE_COLUMNS if c in full.columns]
    full[preserved].to_csv(src_full, index=False)
    h.to_csv(htable, index=False)
    log.info(f"Reset branch {branch_id} hydroTable + src_full")


def _apply_small_segments(recalc: pd.DataFrame, small_segs_path: Path, aoi_id: str) -> pd.DataFrame:
    sml = pd.read_csv(small_segs_path, dtype=str)
    if sml.empty:
        return recalc

    if aoi_id.startswith("19"):
        # Alaska: vectorised path
        new_values = recalc[recalc["HydroID"].isin(sml["update_id"])][
            ["HydroID", "Stage", "Discharge (m3s-1)"]
        ]
        merged = sml.merge(new_values, left_on="update_id", right_on="HydroID", suffixes=("", "_new"))
        merged = merged[["short_id", "Stage", "Discharge (m3s-1)"]]
        recalc = recalc.merge(
            merged.rename(columns={"short_id": "HydroID"}),
            on=["HydroID", "Stage"],
            how="left",
            suffixes=("", "_sml"),
        )
        recalc["Discharge (m3s-1)"] = recalc["Discharge (m3s-1)_sml"].fillna(recalc["Discharge (m3s-1)"])
        recalc = recalc.drop(columns=["Discharge (m3s-1)_sml"], errors="ignore")
        return recalc

    # CONUS: row-by-row copy
    for _, seg in sml.iterrows():
        short_id, update_id = seg.iloc[0], seg.iloc[1]
        new_vals = recalc.loc[recalc["HydroID"] == update_id, ["Stage", "Discharge (m3s-1)"]]
        for _, sv in new_vals.iterrows():
            mask = (recalc["HydroID"] == short_id) & (recalc["Stage"] == sv["Stage"])
            recalc.loc[mask, "Discharge (m3s-1)"] = sv["Discharge (m3s-1)"]
    return recalc


def reset_hydro_and_src(
    aoi_dir: Optional[PathLike] = None,
    *,
    huc_dir: Optional[PathLike] = None,
) -> None:
    """Walk every branch under ``<aoi_dir>/branches/`` and reset each one.

    ``aoi_dir`` and ``huc_dir`` are equivalent — pass whichever matches your
    workflow."""
    from ._stub import resolve_aoi_dir

    aoi_dir = Path(resolve_aoi_dir(aoi_dir, huc_dir))
    aoi_id = aoi_dir.name
    log.info(f"--- reset_htable_src: {aoi_id} ---")
    branches_dir = aoi_dir / "branches"
    if not branches_dir.is_dir():
        log.warning(f"No branches dir under {aoi_dir}")
        return
    for branch_dir in sorted(branches_dir.iterdir()):
        if branch_dir.is_dir():
            reset_branch(branch_dir, branch_dir.name, aoi_id)


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="Reset per-branch hydroTable + SRC to un-calibrated baseline."
    )
    parser.add_argument("-aoi_dir", required=True)
    args = parser.parse_args()
    reset_hydro_and_src(args.aoi_dir)
