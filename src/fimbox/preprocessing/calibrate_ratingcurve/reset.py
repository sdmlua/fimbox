"""
Author: Supath Dhital
Date Updated: May 2026

Reset per-branch hydroTable_{id}.csv and src_full_crosswalked_{id}.csv
back to their uncalibrated baseline. Used at the start of a calibration
rerun so subsequent adjustments don't stack on top of a prior run.

The math is straightforward: re-evaluate Manning's equation using the
default SLOPE and ManningN columns that were stamped in by
``build_src_base``, then push the result into both files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from ._common import PathLike, aoi_id_of, resolve_aoi_dir

log = logging.getLogger(__name__)


# Columns kept in src_full_crosswalked after a reset. Anything else (the
# bankfull / subdivision columns) is a calibration artefact we clear.
# Only the names that actually exist in a given file are kept, so optional
# slope variants can be listed here even when they aren't present yet.
_PRESERVE_COLUMNS = [
    "HydroID",
    "NextDownID",
    "order_",
    "feature_id",
    "Stage",
    "Number of Cells",
    "SurfaceArea (m2)",
    "LENGTHKM",
    "AREASQKM",
    "Volume (m3)",
    "BedArea (m2)",
    "TopWidth (m)",
    "WettedPerimeter (m)",
    "WetArea (m2)",
    "HydraulicRadius (m)",
    "Discharge (m3s-1)",
    "SLOPE",
    "ManningN",
    "Bathymetry_source",
    "default_SLOPE",
    "default_ManningN",

    # Slope variants carried from the crosswalk (kept only if present).
    "SLOPE_RISE_RUN",
    "SLOPE_HFAB",
    "SLOPE_IRIS_SWORD",
]


@dataclass
class HydroTableReset:
    aoi_dir: PathLike

    def run(self) -> None:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        aoi_id = aoi_id_of(aoi_dir)
        log.info(f"--- HydroTableReset: {aoi_id} ---")
        branches_dir = aoi_dir / "branches"
        if not branches_dir.is_dir():
            log.warning(f"No branches dir under {aoi_dir}")
            return
        for bd in sorted(branches_dir.iterdir()):
            if bd.is_dir():
                self._reset_one(bd, bd.name)

    def _reset_one(self, branch_dir: Path, bid: str) -> None:
        src_base = branch_dir / f"src_base_{bid}.csv"
        src_full = branch_dir / f"src_full_crosswalked_{bid}.csv"
        htable = branch_dir / f"hydroTable_{bid}.csv"
        small_segs = branch_dir / f"small_segments_{bid}.csv"

        # If any of the core files is missing the branch never finished.
        if not all(p.is_file() for p in (src_base, src_full, htable)):
            return

        base = pd.read_csv(src_base, dtype={"CatchId": str})
        full = pd.read_csv(src_full)
        h = pd.read_csv(htable, dtype={"HydroID": str})

        for df in (base, full, h):
            df.columns = df.columns.str.strip()

        for col in ("HydroID", "NextDownID", "feature_id"):
            if col in full.columns:
                full[col] = full[col].astype(str)

        # All numeric base columns get coerced; non-numeric ones are dropped.
        numeric_cols = [
            c for c in base.columns if c not in ("CatchId", "HydroID", "NextDownID")
        ]
        base[numeric_cols] = base[numeric_cols].apply(pd.to_numeric, errors="coerce")

        base = base.drop(columns=["SLOPE"]).rename(columns={"CatchId": "HydroID"})
        recalc = base.merge(
            full[
                [
                    "HydroID",
                    "Stage",
                    "default_SLOPE",
                    "default_ManningN",
                    "NextDownID",
                    "order_",
                ]
            ],
            on=["HydroID", "Stage"],
        )
        if recalc.empty:
            log.warning(f"HydroTableReset: merge failed for branch {bid}")
            return

        recalc = recalc.rename(
            columns={"default_SLOPE": "SLOPE", "default_ManningN": "ManningN"}
        )

        # Geometric properties (derived from raw cell counts).
        recalc["TopWidth (m)"] = recalc["SurfaceArea (m2)"] / recalc["LENGTHKM"] / 1000
        recalc["WettedPerimeter (m)"] = (
            recalc["BedArea (m2)"] / recalc["LENGTHKM"] / 1000
        )
        recalc["WetArea (m2)"] = recalc["Volume (m3)"] / recalc["LENGTHKM"] / 1000
        recalc["HydraulicRadius (m)"] = (
            recalc["WetArea (m2)"] / recalc["WettedPerimeter (m)"]
        ).fillna(0)

        # Manning's equation.
        recalc["Discharge (m3s-1)"] = (
            recalc["WetArea (m2)"]
            * recalc["HydraulicRadius (m)"] ** (2 / 3)
            * recalc["SLOPE"] ** 0.5
            / recalc["ManningN"]
        )
        recalc.loc[recalc["Stage"] == 0, "Discharge (m3s-1)"] = 0

        if small_segs.is_file():
            recalc = self._apply_small_segments(recalc, small_segs)

        # Push the recomputed discharge back into both files.
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

        preserved = [c for c in _PRESERVE_COLUMNS if c in full.columns]
        full[preserved].to_csv(src_full, index=False)
        h.to_csv(htable, index=False)
        log.info(f"Reset branch {bid} hydroTable + src_full")

    @staticmethod
    def _apply_small_segments(
        recalc: pd.DataFrame, small_segs_path: Path
    ) -> pd.DataFrame:
        # Replace each short reach's rating curve with its parent reach's
        # values (matched stage-by-stage) so very short reaches don't carry
        # noisy curves. small_segments maps short_id -> update_id (parent).
        sml = pd.read_csv(small_segs_path, dtype=str)
        if sml.empty:
            return recalc

        # Parent (update_id) discharge at each stage, relabelled to the short
        # reach, then merged back so the short reach inherits it.
        parent_q = recalc[recalc["HydroID"].isin(sml["update_id"])][
            ["HydroID", "Stage", "Discharge (m3s-1)"]
        ]
        relabelled = sml.merge(
            parent_q, left_on="update_id", right_on="HydroID", suffixes=("", "_new")
        )[["short_id", "Stage", "Discharge (m3s-1)"]]

        recalc = recalc.merge(
            relabelled.rename(columns={"short_id": "HydroID"}),
            on=["HydroID", "Stage"],
            how="left",
            suffixes=("", "_sml"),
        )
        recalc["Discharge (m3s-1)"] = recalc["Discharge (m3s-1)_sml"].fillna(
            recalc["Discharge (m3s-1)"]
        )
        return recalc.drop(columns=["Discharge (m3s-1)_sml"], errors="ignore")


# Function-style wrapper for callers that prefer it.
def reset_hydro_and_src(
    aoi_dir: Optional[PathLike] = None, *, huc_dir: Optional[PathLike] = None
) -> None:
    HydroTableReset(aoi_dir=resolve_aoi_dir(aoi_dir, huc_dir)).run()
