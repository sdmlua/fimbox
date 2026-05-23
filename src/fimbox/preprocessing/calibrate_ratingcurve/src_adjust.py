"""
Author: Supath Dhital
Date Updated: May 2026

Synthetic rating curve (SRC) geometry-side adjustments.

Three classes, one per routine:

  SrcBankfull        identify the in-channel bankfull stage for each HydroID
                     using an external table of NWM bankfull-recurrence flows.
  SrcSubdiv          subdivide each rating curve into channel + overbank
                     volumes, then recompute Manning's equation with separate
                     channel_n / overbank_n. Also rewrites the per-branch
                     hydroTable with the subdivided discharge.
  SrcNonmonotonic    walk each HydroID's in-channel SRC and force the
                     discharge to be monotonically non-decreasing. Used
                     after subdivision so the floodplain portion is
                     untouched.

Every class operates branch-by-branch with a process pool. Inputs and
outputs are CSV files inside each branch directory; no rasters touched.
"""

from __future__ import annotations

import logging
import os
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

import numpy as np
import pandas as pd

from ._common import (
    BEDAREA_VAR,
    HRADIUS_VAR,
    SURFACE_AREA_VAR,
    VOLUME_VAR,
    PathLike,
    iter_branches,
    resolve_aoi_dir,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bankfull
# ---------------------------------------------------------------------------


@dataclass
class SrcBankfull:
    # Identify bankfull stage for every HydroID by looking up the closest
    # SRC discharge to an external NWM bankfull-recurrence flow per
    # feature_id.

    aoi_dir: PathLike
    bankfull_flows_file: PathLike
    n_workers: int = 1
    include_branch_zero: bool = True

    def run(self) -> dict[str, str]:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        bflows_path = Path(self.bankfull_flows_file)
        if not bflows_path.is_file():
            raise FileNotFoundError(f"bankfull flows file not found: {bflows_path}")

        df_bflows = pd.read_csv(bflows_path, dtype={"feature_id": int})
        df_bflows = df_bflows.rename(columns={"discharge": "bankfull_flow"})

        branches = list(iter_branches(aoi_dir, exclude_zero=not self.include_branch_zero))
        if not branches:
            log.warning(f"SrcBankfull: no branches found under {aoi_dir}")
            return {}

        log.info(
            f"SrcBankfull: {len(branches)} branches, {self.n_workers} workers"
        )
        results: dict[str, str] = {}
        if self.n_workers <= 1:
            for bid, bp in branches:
                results[bid] = _bankfull_one_branch(bp, bid, df_bflows)
        else:
            with ProcessPoolExecutor(max_workers=self.n_workers) as pool:
                fut_to_bid = {
                    pool.submit(_bankfull_one_branch, bp, bid, df_bflows): bid
                    for bid, bp in branches
                }
                for fut in as_completed(fut_to_bid):
                    bid = fut_to_bid[fut]
                    try:
                        results[bid] = fut.result()
                    except Exception as exc:
                        results[bid] = f"FAIL {exc}"
                        log.exception(f"SrcBankfull branch {bid} failed")
        return results


def _bankfull_one_branch(branch_dir: Path, bid: str, df_bflows: pd.DataFrame) -> str:
    src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
    if not src_path.is_file():
        return "SKIP no src_full_crosswalked"

    df_src = pd.read_csv(src_path, dtype={"HydroID": int, "feature_id": int})

    # Merge external bankfull flow onto the SRC by feature_id.
    df_src = df_src.merge(df_bflows, how="left", on="feature_id")

    # Fill missing bankfull flows so the closest-stage lookup below doesn't
    # propagate NaNs into the entire HydroID.
    df_src["bankfull_flow"] = df_src["bankfull_flow"].fillna(-999)

    # Distance to bankfull flow at each stage. NaN guarded.
    df_src["Q_bfull_find"] = (df_src["bankfull_flow"] - df_src["Discharge (m3s-1)"]).abs()
    df_src["Q_bfull_find"] = df_src["Q_bfull_find"].fillna(999999)

    # For each HydroID, the row whose discharge is closest to bankfull_flow
    # is the bankfull stage. Drop the stage=0 row before idxmin so a flat
    # rating curve doesn't degenerate to zero.
    cand = df_src[df_src["Stage"] > 0.0][[
        "Stage", "HydroID", BEDAREA_VAR, VOLUME_VAR, HRADIUS_VAR,
        SURFACE_AREA_VAR, "Q_bfull_find",
    ]].reset_index(drop=True)
    if cand.empty:
        return "SKIP empty SRC"
    picked = cand.loc[cand.groupby("HydroID")["Q_bfull_find"].idxmin()].reset_index(drop=True)
    picked = picked.rename(columns={
        "Stage": "Stage_bankfull",
        BEDAREA_VAR: "BedArea_bankfull",
        VOLUME_VAR: "Volume_bankfull",
        HRADIUS_VAR: "HRadius_bankfull",
        SURFACE_AREA_VAR: "SurfArea_bankfull",
    })

    df_src = df_src.drop(columns=["Q_bfull_find"])
    df_src = df_src.merge(
        picked[[
            "Stage_bankfull", "HydroID", "BedArea_bankfull",
            "Volume_bankfull", "HRadius_bankfull", "SurfArea_bankfull",
        ]],
        how="left",
        on="HydroID",
    )

    # Mask bankfull when the source flow was unusable (NWM lake/coast).
    df_src.loc[df_src["bankfull_flow"] <= 0.0, "Stage_bankfull"] = np.nan

    # Tag each row as channel vs floodplain by comparing stage to bankfull.
    df_src.loc[df_src["Stage"] <= df_src["Stage_bankfull"], "bankfull_proxy"] = "channel"
    df_src.loc[df_src["Stage"] > df_src["Stage_bankfull"], "bankfull_proxy"] = "floodplain"
    df_src["bankfull_proxy"] = df_src["bankfull_proxy"].fillna("channel")

    df_src.to_csv(src_path, index=False)
    return f"OK bankfull set on {len(df_src)} rows"


# ---------------------------------------------------------------------------
# Subdivision (channel / overbank)
# ---------------------------------------------------------------------------


@dataclass
class SrcSubdiv:
    # Channel/overbank subdivision using Manning's equation with separate
    # roughness coefficients. Requires SrcBankfull to have run first
    # (consumes the Stage_bankfull column it adds).

    aoi_dir: PathLike
    vmann_table: PathLike  # CSV with columns: feature_id, channel_n, overbank_n
    n_workers: int = 1
    include_branch_zero: bool = True
    default_channel_n: float = 0.06
    default_overbank_n: float = 0.12

    def run(self) -> dict[str, str]:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        mann_path = Path(self.vmann_table)
        if not mann_path.is_file():
            raise FileNotFoundError(f"Manning's n table not found: {mann_path}")

        df_mann = pd.read_csv(mann_path, dtype={"feature_id": "int64"})

        branches = list(iter_branches(aoi_dir, exclude_zero=not self.include_branch_zero))
        log.info(f"SrcSubdiv: {len(branches)} branches, {self.n_workers} workers")
        results: dict[str, str] = {}
        if self.n_workers <= 1:
            for bid, bp in branches:
                results[bid] = _subdiv_one_branch(
                    bp, bid, df_mann, self.default_channel_n, self.default_overbank_n
                )
        else:
            with ProcessPoolExecutor(max_workers=self.n_workers) as pool:
                fut_to_bid = {
                    pool.submit(
                        _subdiv_one_branch, bp, bid, df_mann,
                        self.default_channel_n, self.default_overbank_n,
                    ): bid
                    for bid, bp in branches
                }
                for fut in as_completed(fut_to_bid):
                    bid = fut_to_bid[fut]
                    try:
                        results[bid] = fut.result()
                    except Exception as exc:
                        results[bid] = f"FAIL {exc}"
                        log.exception(f"SrcSubdiv branch {bid} failed")
        return results


def _subdiv_geometry(df: pd.DataFrame) -> pd.DataFrame:
    # Split per-stage volume / bed area / wetted perimeter into channel and
    # overbank components using Stage_bankfull as the divide.
    in_channel = df["Stage"] <= df["Stage_bankfull"]

    # Channel volume: full SRC volume below bankfull; above bankfull the
    # channel volume grows linearly with the bankfull surface area.
    df["Volume_chan (m3)"] = np.where(
        in_channel,
        df[VOLUME_VAR],
        df["Volume_bankfull"] + (df["Stage"] - df["Stage_bankfull"]) * df["SurfArea_bankfull"],
    )
    df["BedArea_chan (m2)"] = np.where(
        in_channel, df[BEDAREA_VAR], df["BedArea_bankfull"]
    )
    base_wp_chan = df["BedArea_chan (m2)"] / df["LENGTHKM"] / 1000
    df["WettedPerimeter_chan (m)"] = np.where(
        in_channel,
        base_wp_chan,
        base_wp_chan + (df["Stage"] - df["Stage_bankfull"]) * 2,
    )

    # Overbank: everything above the channel partition. Zero in-channel.
    df["Volume_obank (m3)"] = np.where(
        df["Stage"] > df["Stage_bankfull"],
        df[VOLUME_VAR] - df["Volume_chan (m3)"],
        0.0,
    )
    df["BedArea_obank (m2)"] = np.where(
        df["Stage"] > df["Stage_bankfull"],
        df[BEDAREA_VAR] - df["BedArea_chan (m2)"],
        0.0,
    )
    df["WettedPerimeter_obank (m)"] = df["BedArea_obank (m2)"] / df["LENGTHKM"] / 1000
    return df


def _subdiv_mannings(df: pd.DataFrame) -> pd.DataFrame:
    # Apply Manning's equation separately to channel and overbank, then sum
    # the two discharges to get the subdivided total.

    # Channel
    df["WetArea_chan (m2)"] = df["Volume_chan (m3)"] / df["LENGTHKM"] / 1000
    df["HydraulicRadius_chan (m)"] = df["WetArea_chan (m2)"] / df["WettedPerimeter_chan (m)"]
    df["HydraulicRadius_chan (m)"] = df["HydraulicRadius_chan (m)"].fillna(0)
    df["Discharge_chan (m3s-1)"] = (
        df["WetArea_chan (m2)"]
        * np.power(df["HydraulicRadius_chan (m)"], 2.0 / 3)
        * np.power(df["SLOPE"], 0.5)
        / df["channel_n"]
    )
    df["Velocity_chan (m/s)"] = (df["Discharge_chan (m3s-1)"] / df["WetArea_chan (m2)"]).fillna(0)

    # Overbank
    df["WetArea_obank (m2)"] = df["Volume_obank (m3)"] / df["LENGTHKM"] / 1000
    df["HydraulicRadius_obank (m)"] = df["WetArea_obank (m2)"] / df["WettedPerimeter_obank (m)"]
    df = df.replace([np.inf, -np.inf], np.nan)
    df["HydraulicRadius_obank (m)"] = df["HydraulicRadius_obank (m)"].fillna(0)
    df["Discharge_obank (m3s-1)"] = (
        df["WetArea_obank (m2)"]
        * np.power(df["HydraulicRadius_obank (m)"], 2.0 / 3)
        * np.power(df["SLOPE"], 0.5)
        / df["overbank_n"]
    )
    df["Velocity_obank (m/s)"] = (df["Discharge_obank (m3s-1)"] / df["WetArea_obank (m2)"]).fillna(0)

    # Total
    df["Discharge (m3s-1)_subdiv"] = df["Discharge_chan (m3s-1)"] + df["Discharge_obank (m3s-1)"]
    df.loc[df["Stage"] == 0, "Discharge (m3s-1)_subdiv"] = 0
    return df


def _subdiv_one_branch(
    branch_dir: Path,
    bid: str,
    df_mann: pd.DataFrame,
    default_chan_n: float,
    default_obank_n: float,
) -> str:
    src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
    ht_path = branch_dir / f"hydroTable_{bid}.csv"
    if not src_path.is_file() or not ht_path.is_file():
        return "SKIP src or hydroTable missing"

    df = pd.read_csv(src_path, dtype={"feature_id": "int64"})
    if "Stage_bankfull" not in df.columns:
        return "SKIP no Stage_bankfull (run SrcBankfull first)"

    # Drop any leftover subdiv columns from a previous run so we recompute
    # cleanly. errors='ignore' covers the first-time case.
    df = df.drop(columns=[
        "channel_n", "overbank_n", "subdiv_applied", "Discharge (m3s-1)_subdiv",
        "Volume_chan (m3)", "Volume_obank (m3)",
        "BedArea_chan (m2)", "BedArea_obank (m2)",
        "WettedPerimeter_chan (m)", "WettedPerimeter_obank (m)",
        "WetArea_chan (m2)", "HydraulicRadius_chan (m)",
        "Discharge_chan (m3s-1)", "Velocity_chan (m/s)",
        "WetArea_obank (m2)", "HydraulicRadius_obank (m)",
        "Discharge_obank (m3s-1)", "Velocity_obank (m/s)",
    ], errors="ignore")

    # Geometry and Manning's calculations.
    df = _subdiv_geometry(df)
    df = df.merge(df_mann, how="left", on="feature_id")
    df["channel_n"] = df["channel_n"].fillna(default_chan_n)
    df["overbank_n"] = df["overbank_n"].fillna(default_obank_n)
    df["subdiv_applied"] = ~df["Stage_bankfull"].isnull()
    df = _subdiv_mannings(df)

    # If subdiv didn't apply (NaN bankfull), keep the default discharge.
    df["Discharge (m3s-1)_subdiv"] = np.where(
        df["subdiv_applied"], df["Discharge (m3s-1)_subdiv"], df["Discharge (m3s-1)"]
    )

    df.to_csv(src_path, index=False)

    # Push the subdivided discharge + n values into the hydroTable.
    trim = df[[
        "HydroID", "Stage", "subdiv_applied", "channel_n", "overbank_n",
        "Discharge (m3s-1)_subdiv",
    ]].copy()
    if "Bathymetry_source" in df.columns:
        trim["Bathymetry_source"] = df["Bathymetry_source"]
    trim = trim.rename(columns={
        "Stage": "stage",
        "Discharge (m3s-1)_subdiv": "subdiv_discharge_cms",
    })
    trim["discharge_cms"] = trim["subdiv_discharge_cms"]

    ht = pd.read_csv(ht_path, dtype={"HUC": str, "last_updated": object,
                                      "submitter": object, "obs_source": object})
    ht = ht.drop(columns=[
        "subdiv_applied", "discharge_cms", "overbank_n", "channel_n",
        "subdiv_discharge_cms", "Bathymetry_source",
    ], errors="ignore")
    ht = ht.merge(trim, how="left", on=["HydroID", "stage"])
    ht.to_csv(ht_path, index=False)

    return f"OK subdiv on {len(df)} src rows / {len(ht)} ht rows"


# ---------------------------------------------------------------------------
# Nonmonotonic SRC correction
# ---------------------------------------------------------------------------


@dataclass
class SrcNonmonotonic:
    # Walk each in-channel rating curve and force discharge to be
    # monotonically non-decreasing with stage. Floodplain portion is
    # untouched. Defaults to stream_order >= 1 (apply everywhere).

    aoi_dir: PathLike
    stream_order_min: int = 1
    include_branch_zero: bool = True

    def run(self) -> dict[str, str]:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)

        # Branch zero gets a couple of bookkeeping touches before the loop.
        self._normalize_branch_zero(aoi_dir)

        branches = list(iter_branches(aoi_dir, exclude_zero=True))
        log.info(f"SrcNonmonotonic: {len(branches)} non-zero branches")
        results: dict[str, str] = {}
        for bid, bp in branches:
            try:
                results[bid] = self._one_branch(bp, bid)
            except Exception as exc:
                results[bid] = f"FAIL {exc}"
                log.exception(f"SrcNonmonotonic branch {bid} failed")
        return results

    def _normalize_branch_zero(self, aoi_dir: Path) -> None:
        # Tidy branch 0's SRC + hydroTable: ensure Bathymetry_source is a
        # readable string, add the "_adjustment_applied" flag columns, drop
        # duplicate (HydroID, stage) rows.
        b0 = aoi_dir / "branches" / "0"
        src0 = b0 / "src_full_crosswalked_0.csv"
        ht0 = b0 / "hydroTable_0.csv"
        if not (src0.is_file() and ht0.is_file()):
            return

        src = pd.read_csv(src0, low_memory=False)
        ht = pd.read_csv(ht0, low_memory=False)

        if "Bathymetry_source" in src.columns:
            src.loc[src["Bathymetry_source"].astype(str) == "0", "Bathymetry_source"] = "No Bathymetry Applied"
            src["Bathymetry_source"] = src["Bathymetry_source"].fillna("No Bathymetry Applied")
            ht["Bathymetry_source"] = src["Bathymetry_source"]
        for flag in (
            "Longitudinal_adjustment_applied",
            "Thalweg_adjustment_applied",
            "Nonmonotonic_adjustment_applied",
        ):
            src[flag] = False

        src = src.drop_duplicates(subset=["HydroID", "feature_id", "Stage"], keep="first").reset_index(drop=True)
        ht = ht.drop_duplicates(subset=["HydroID", "feature_id", "stage"], keep="first").reset_index(drop=True)
        src.to_csv(src0, index=False)
        ht.to_csv(ht0, index=False)

    def _one_branch(self, branch_dir: Path, bid: str) -> str:
        src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
        if not src_path.is_file():
            return "SKIP no src"

        df = pd.read_csv(src_path, low_memory=False)
        df = df.drop_duplicates(subset=["HydroID", "Stage"], keep="first").reset_index(drop=True)

        # If subdivision ran, use that as the discharge source. Otherwise
        # the standard Discharge column is canonical.
        if "subdiv_applied" in df.columns and "Discharge (m3s-1)_subdiv" in df.columns:
            df["Discharge (m3s-1)"] = np.where(
                df["subdiv_applied"] == True,
                df["Discharge (m3s-1)_subdiv"],
                df["Discharge (m3s-1)"],
            )

        original = df.copy()

        # Per-HydroID nonmonotonic fix.
        fixed = df.groupby("HydroID", group_keys=False).apply(
            self._fix_hydroid, strm_order=self.stream_order_min,
        )

        # Restore floodplain rows to the original values so we only touch
        # in-channel stages. Some columns may not exist on first runs.
        if "bankfull_proxy" in original.columns:
            mask_fp = original["bankfull_proxy"] == "floodplain"
            cols = [
                "Discharge (m3s-1)", "SurfaceArea (m2)", "BedArea (m2)",
                "TopWidth (m)", "WettedPerimeter (m)", "HydraulicRadius (m)",
            ]
            if "Discharge (m3s-1)_subdiv" in original.columns:
                cols.append("Discharge (m3s-1)_subdiv")
            for c in cols:
                if c in original.columns and c in fixed.columns:
                    fixed.loc[mask_fp, c] = original.loc[mask_fp, c]

        # Zero stage always gets zero discharge.
        fixed.loc[fixed["Stage"] == 0, "Discharge (m3s-1)"] = 0
        if "Discharge (m3s-1)_subdiv" in fixed.columns:
            fixed.loc[fixed["Stage"] == 0, "Discharge (m3s-1)_subdiv"] = 0

        # Tidy Bathymetry_source the same way as branch 0.
        if "Bathymetry_source" in fixed.columns:
            fixed.loc[fixed["Bathymetry_source"].astype(str) == "0", "Bathymetry_source"] = "No Bathymetry Applied"
            fixed["Bathymetry_source"] = fixed["Bathymetry_source"].fillna("No Bathymetry Applied")

        # Forward-fill per-HydroID flag columns that were attached at one
        # stage only.
        for col in ("subdiv_applied", "channel_n", "overbank_n"):
            if col in fixed.columns:
                fixed[col] = fixed.groupby("HydroID")[col].ffill()

        fixed.to_csv(src_path, index=False)
        return f"OK nonmonotonic on {len(fixed)} rows"

    @staticmethod
    def _fix_hydroid(grp: pd.DataFrame, strm_order: int) -> pd.DataFrame:
        # Per-HydroID worker. Skipped if stream order is below threshold
        # (low-order streams are often too noisy to trust the fix on).
        if grp.empty or grp["order_"].iloc[0] < strm_order:
            return grp
        grp = grp.copy()
        grp.loc[grp["Stage"] == 0, "Discharge (m3s-1)"] = 0

        # Operate only on the channel portion.
        if "bankfull_proxy" not in grp.columns:
            return grp
        chan = grp[grp["bankfull_proxy"] == "channel"]
        non_mono_idx = chan.index[chan["Discharge (m3s-1)"].diff().lt(0)].tolist()
        if not non_mono_idx:
            return grp

        # Replay all rows before the last nonmonotonic index using the
        # geometry of that row. This forces a smooth, monotonically rising
        # rating curve up to the violation.
        target = non_mono_idx[-1]
        tgt_cells = grp.at[target, "Number of Cells"]
        tgt_sa = grp.at[target, "SurfaceArea (m2)"]
        tgt_ba = grp.at[target, "BedArea (m2)"]
        rows = slice(0, target)
        grp.loc[rows, "Number of Cells"] = tgt_cells
        grp.loc[rows, "SurfaceArea (m2)"] = tgt_sa
        grp.loc[rows, "BedArea (m2)"] = tgt_ba

        length_km = grp.loc[rows, "LENGTHKM"].replace(0, np.nan)
        grp.loc[rows, "TopWidth (m)"] = tgt_sa / length_km / 1000
        grp.loc[rows, "WettedPerimeter (m)"] = tgt_ba / length_km / 1000

        wet_area = grp.loc[rows, "WetArea (m2)"]
        grp.loc[rows, "HydraulicRadius (m)"] = wet_area / grp.loc[rows, "WettedPerimeter (m)"]
        grp["HydraulicRadius (m)"] = grp["HydraulicRadius (m)"].fillna(0)

        # Recompute discharge from the corrected geometry.
        grp.loc[rows, "Discharge (m3s-1)"] = (
            wet_area
            * np.power(grp.loc[rows, "HydraulicRadius (m)"], 2.0 / 3)
            * np.power(grp.loc[rows, "SLOPE"], 0.5)
            / grp.loc[rows, "channel_n"]
        ) if "channel_n" in grp.columns else (
            wet_area
            * np.power(grp.loc[rows, "HydraulicRadius (m)"], 2.0 / 3)
            * np.power(grp.loc[rows, "SLOPE"], 0.5)
            / grp.loc[rows, "ManningN"]
        )
        return grp
