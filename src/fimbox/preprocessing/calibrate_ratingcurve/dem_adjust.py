"""
Author: Supath Dhital
Date Updated: June 2026

Pre-subdivision SRC adjustments. Each edits the per-branch
src_full_crosswalked, not the DEM raster.

  ThalwegNotchesAdjustment   drop notch rows, refill stage ladder
  LongitudinalFlowFilter     smooth geometry along reach chains, recompute Q
  BathymetricAdjustment      add eHydro / AI channel depth below the DEM
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

import numpy as np
import pandas as pd

from ._common import PathLike, aoi_id_of, iter_branches, resolve_aoi_dir
from .src_adjust import _run_branches

log = logging.getLogger(__name__)

# Linearly extrapolated to refill missing stages; other columns repeat last.
_EXTRAPOLATE_COLUMNS = [
    "Number of Cells",
    "SurfaceArea (m2)",
    "BedArea (m2)",
    "Volume (m3)",
    "TopWidth (m)",
    "WettedPerimeter (m)",
    "WetArea (m2)",
    "HydraulicRadius (m)",
    "Discharge (m3s-1)",
]

# Restored to int after extrapolation produces floats.
_INT_COLUMNS = [
    "Number of Cells",
    "SurfaceArea (m2)",
    "HydroID",
    "NextDownID",
    "order_",
    "feature_id",
]


@dataclass
class ThalwegNotchesAdjustment:
    # Drop notch rows (Stage > 0 with Number of Cells == 0), re-index each
    # HydroID onto a clean stage ladder, and linear-extrapolate to refill.
    # Non-zero branches only; rewrites the SRC.

    aoi_dir: PathLike
    n_workers: Optional[int] = None  # branch-parallel workers
    stage_interval_m: float = 0.3048
    n_stages: int = 84  # ladder length
    extrap_rows: int = 3  # trailing rows fit for extrapolation

    def run(self) -> dict[str, str]:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        aoi_id = aoi_id_of(aoi_dir)
        branches = list(iter_branches(aoi_dir, exclude_zero=True))
        log.info(f"ThalwegNotchesAdjustment: {aoi_id} ({len(branches)} branches)")
        return _run_branches(
            branches,
            _thalweg_one_branch,
            (self.stage_interval_m, self.n_stages, self.extrap_rows),
            self.n_workers,
            "ThalwegNotchesAdjustment",
        )


def _thalweg_one_branch(
    branch_dir: Path,
    bid: str,
    stage_interval_m: float,
    n_stages: int,
    extrap_rows: int,
) -> str:
    src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
    if not src_path.is_file():
        return "SKIP no src_full_crosswalked"

    stages_full = np.array([round(i * stage_interval_m, 4) for i in range(n_stages)])
    src = pd.read_csv(src_path, low_memory=False)
    prethalweg_discharge = src["Discharge (m3s-1)"].copy()

    df = src.drop_duplicates(subset=["HydroID", "Stage"], keep="first").reset_index(
        drop=True
    )

    notch = (df["Number of Cells"] == 0) & (df["Stage"] > 0)
    if notch.sum() > 0:
        kept = df[~notch].copy()
        # Group on a renamed key so HydroID stays a column inside the worker
        # (the lambdas read it), sidestepping the grouping-column deprecation.
        kept = (
            kept.groupby(kept["HydroID"].rename("_grp"), group_keys=False)
            .apply(lambda g: _reset_stage(g, stage_interval_m))
            .reset_index(drop=True)
        )
        df = (
            kept.groupby(kept["HydroID"].rename("_grp"), group_keys=False)
            .apply(lambda g: _extend_linear(g, stages_full, extrap_rows))
            .sort_values(["HydroID", "Stage"])
            .reset_index(drop=True)
        )
        present_int = [c for c in _INT_COLUMNS if c in df.columns]
        df[present_int] = df[present_int].astype(int)

    df.loc[df["Stage"] == 0, "Discharge (m3s-1)"] = 0

    # Flag rows whose discharge changed vs the original.
    df["Discharge (m3s-1)_thalwegAdjusted"] = df["Discharge (m3s-1)"]
    df = df.reset_index(drop=True)
    if len(df) == len(prethalweg_discharge):
        df["prethalweg_Discharge (m3s-1)"] = prethalweg_discharge.values
    else:
        df["prethalweg_Discharge (m3s-1)"] = np.nan
    df["Thalweg_adjustment_applied"] = (
        (df["Discharge (m3s-1)_thalwegAdjusted"] - df["prethalweg_Discharge (m3s-1)"])
        .abs()
        .gt(0)
        .fillna(False)
    )

    df.to_csv(src_path, index=False)
    return f"OK thalweg notches: {int(notch.sum())} notch rows on {len(df)} SRC rows"


def _reset_stage(grp: pd.DataFrame, stage_interval_m: float) -> pd.DataFrame:
    # Re-index onto a clean 0-based stage ladder.
    grp = grp.sort_values("Stage").reset_index(drop=True)
    grp["Stage"] = np.array([round(i * stage_interval_m, 4) for i in range(len(grp))])
    return grp


def _extend_linear(
    grp: pd.DataFrame, stages_full: np.ndarray, extrap_rows: int
) -> pd.DataFrame:
    # Refill to the full ladder; extrapolate geometry/Q, repeat others.
    if len(grp["Stage"].values) == len(stages_full):
        return grp

    existing = grp.set_index("Stage")
    out = pd.DataFrame({"Stage": stages_full})
    out["HydroID"] = grp["HydroID"].iloc[0]
    out = out.set_index("Stage")

    for col in [c for c in grp.columns if c != "Stage"]:
        if col in _EXTRAPOLATE_COLUMNS:
            tail_x = existing.index.values[-extrap_rows:]
            tail_y = existing[col].values[-extrap_rows:]
            mask = ~np.isnan(tail_x) & ~np.isnan(tail_y)
            x, y = tail_x[mask], tail_y[mask]
            if len(x) >= 2 and np.var(x) > 1e-8:
                try:
                    coeffs = np.polyfit(x, y, 1)
                    out[col] = np.polyval(coeffs, out.index.values)
                except np.linalg.LinAlgError:
                    out[col] = y[-1] if len(y) else existing[col].iloc[-1]
            else:
                out[col] = existing[col].iloc[-1]
            for stage in existing.index.values:
                out.at[stage, col] = existing.at[stage, col]
        else:
            out[col] = np.nan
            for stage in existing.index.values:
                out.at[stage, col] = existing.at[stage, col]
            last_value = existing[col].loc[np.sort(existing.index.values)[-1]]
            out[col] = out[col].fillna(last_value)

    return out.reset_index()


_LONGITUDINAL_KEYS = [
    "SurfaceArea (m2)",
    "Volume (m3)",
    "BedArea (m2)",
    "WetArea (m2)",
    "HydraulicRadius (m)",
    "Discharge (m3s-1)",
]
# Smooth only geometry (first N keys); discharge is recomputed afterwards.
_LONGITUDINAL_SMOOTH_N = 2


def _min_pct_filter(values: np.ndarray) -> np.ndarray:
    """10th percentile of the positive values in a 4-wide sliding window.

    At most four samples go into each window, so at the 10th percentile only the
    two smallest positives can matter — which turns the whole pass into a sort of
    four shifted copies instead of a Python callback per cell. Window offsets
    (-2..+1) and reflected edges match scipy's ``generic_filter(size=4)``.
    """
    padded = np.pad(np.asarray(values, dtype=float), (2, 1), mode="symmetric")
    n = values.size
    win = np.stack([padded[k : k + n] for k in range(4)], axis=1)

    positive = win > 0  # NaN compares False, so lake reaches drop out
    counts = positive.sum(axis=1)
    ranked = np.sort(np.where(positive, win, np.inf), axis=1)

    # Collapse the empty/single-value windows onto the same expression so no
    # arithmetic ever touches the +inf padding.
    lo = np.where(counts >= 1, ranked[:, 0], 0.0)
    second = np.where(counts >= 2, ranked[:, 1], lo)
    # np.percentile interpolates 0.1*(n-1) of the way from the smallest positive
    # toward the next one.
    return lo + (second - lo) * (0.1 * (counts - 1))


def _filter_voi(voi_array):
    # Min (10th-pct) then gaussian, along the chain.
    from scipy.ndimage import gaussian_filter1d

    return gaussian_filter1d(_min_pct_filter(voi_array), sigma=2, radius=2)


@dataclass
class LongitudinalFlowFilter:
    # Smooth geometry down each headwater->outlet chain, recompute Q.
    # Lakes keep original Q. Non-zero branches only.

    aoi_dir: PathLike
    n_workers: Optional[int] = None  # branch-parallel workers
    n_stages: int = 84

    def run(self) -> dict[str, str]:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        aoi_id = aoi_id_of(aoi_dir)
        branches = list(iter_branches(aoi_dir, exclude_zero=True))
        log.info(f"LongitudinalFlowFilter: {aoi_id} ({len(branches)} branches)")
        return _run_branches(
            branches,
            _longitudinal_one_branch,
            (self.n_stages,),
            self.n_workers,
            "LongitudinalFlowFilter",
        )


def _longitudinal_one_branch(branch_dir: Path, bid: str, n_stages: int) -> str:
    src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
    catch_gpkg = (
        branch_dir
        / f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{bid}.gpkg"
    )
    if not src_path.is_file():
        return "SKIP no src_full_crosswalked"
    if not catch_gpkg.is_file():
        return "SKIP no crosswalked catchments gpkg"

    import geopandas as gpd

    catch = gpd.read_file(catch_gpkg).drop_duplicates(subset=["HydroID"], keep="first")
    lake_df = catch[["HydroID", "LakeID"]].drop_duplicates(subset=["HydroID"])

    src = pd.read_csv(src_path, low_memory=False)
    # Smoothing is not idempotent — re-running re-smooths already-smoothed geometry and keeps shrinking discharge. If it already ran, skip; arerun must reset the SRC to baseline first (calibration_rerun).
    if "Discharge (m3s-1)_longitudinalAdjusted" in src.columns:
        return "SKIP already applied (reset SRC to re-run)"
    # Drop the catchment LakeID so the merge below adds it cleanly (no _x/_y).
    src = src.drop(columns=["LakeID"], errors="ignore")
    # Defensively collapse any duplicate columns left by earlier bad merges, so boolean masks stay 1-D Series rather than DataFrames.
    src = src.loc[:, ~src.columns.duplicated()]
    src = src.merge(lake_df, on="HydroID", how="inner")
    stages = [round(v, 4) for v in src["Stage"][:n_stages]]

    # BedArea<->SurfaceArea fit, used to re-derive BedArea later.
    a_coef, b_coef = np.polyfit(src["SurfaceArea (m2)"], src["BedArea (m2)"], 1)

    q0_mask = src["Discharge (m3s-1)"] == 0
    nocell0_mask = src["Number of Cells"] == 0

    chains = _build_chains(catch)
    if not chains:
        return "SKIP no multi-reach chains"

    # Smooth geometry, write back for non-lake reaches.
    smooth_keys = _LONGITUDINAL_KEYS[:_LONGITUDINAL_SMOOTH_N]
    ladders = _reach_ladders(src, smooth_keys)
    filtered = {}
    for key in smooth_keys:
        filtered[key] = _filter_key(src, chains, stages, key, ladders)
    for key in _LONGITUDINAL_KEYS[:_LONGITUDINAL_SMOOTH_N]:
        adj_col = f"{key}_longitudinalAdjusted"
        src = src.merge(filtered[key], on=["HydroID", "Stage"], how="left")
        mask = src[adj_col].notna() & (src["LakeID"] < 0)
        # Smoothed values are float; the raw geometry column may be int — cast
        # before writing so the assignment doesn't downcast/warn.
        src[key] = src[key].astype(float)
        src.loc[mask, key] = src.loc[mask, adj_col]

    # Recompute geometry + Q from the smoothed surface area / volume.
    mask_land = src["LakeID"] < 0
    src.loc[mask_land, "BedArea (m2)"] = a_coef * src["SurfaceArea (m2)"] + b_coef
    length_m = src["LENGTHKM"] * 1000.0
    src["WettedPerimeter (m)"] = src["BedArea (m2)"] / length_m
    src["WetArea (m2)"] = src["Volume (m3)"] / length_m
    src["HydraulicRadius (m)"] = (
        src["WetArea (m2)"] / src["WettedPerimeter (m)"]
    ).fillna(0)
    src["Discharge (m3s-1)"] = (
        src["WetArea (m2)"]
        * np.power(src["HydraulicRadius (m)"].clip(lower=0), 2.0 / 3)
        * np.power(src["SLOPE"].clip(lower=0), 0.5)
        / src["ManningN"]
    )

    # Lakes keep their original discharge.
    q_lake = src[["HydroID", "LakeID", "Stage", "Discharge (m3s-1)"]].rename(
        columns={"Discharge (m3s-1)": "_q_lake"}
    )
    merged = src.merge(q_lake, on=["HydroID", "LakeID", "Stage"], how="left")
    lake_mask = (merged["LakeID"] > 0) & merged["_q_lake"].notna()
    src.loc[lake_mask, "Discharge (m3s-1)"] = merged.loc[lake_mask, "_q_lake"]

    # Round but keep slope precision.
    slope_cols = [c for c in ("SLOPE", "default_SLOPE") if c in src.columns]
    slope_backup = src[slope_cols].copy()
    src = src.round(5)
    src[slope_cols] = slope_backup

    # Zero-Q / zero-cell rows stay physically zero.
    for col in (
        "Discharge (m3s-1)",
        "Volume (m3)",
        "WettedPerimeter (m)",
        "WetArea (m2)",
        "HydraulicRadius (m)",
        "BedArea (m2)",
    ):
        src.loc[q0_mask, col] = 0
    for col in ("Number of Cells", "SurfaceArea (m2)", "TopWidth (m)"):
        src.loc[nocell0_mask, col] = 0
    src.loc[src["Stage"] == 0, "Discharge (m3s-1)"] = 0

    src["Discharge (m3s-1)_longitudinalAdjusted"] = src["Discharge (m3s-1)"]
    if "Discharge (m3s-1)_thalwegAdjusted" in src.columns:
        changed = (
            src["Discharge (m3s-1)_thalwegAdjusted"]
            - src["Discharge (m3s-1)_longitudinalAdjusted"]
        ).abs() > 0
    else:
        changed = pd.Series(False, index=src.index)
    src["Longitudinal_adjustment_applied"] = changed.fillna(False)

    src.to_csv(src_path, index=False)
    return f"OK longitudinal: {len(chains)} chains on {len(src)} SRC rows"


def _build_chains(catch) -> list[list[int]]:
    # Walk NextDownID from each headwater to outlet; keep chains > 2 reaches.
    next_ids = catch["NextDownID"].astype(int)
    headwaters_rows = catch.loc[~catch["HydroID"].isin(next_ids)]
    headwaters = list(headwaters_rows[headwaters_rows["LakeID"] < 0]["HydroID"])
    # HydroID -> NextDownID up front: the walk used to re-scan the whole table
    # twice per step, which is what made big AOIs crawl.
    next_of = dict(zip(catch["HydroID"].astype(int).tolist(), next_ids.tolist()))
    chains: list[list[int]] = []
    for hw in headwaters:
        chain = [hw]
        nxt = hw
        while nxt in next_of:
            nxt = next_of[nxt]
            chain.append(nxt)
        if len(chain[:-1]) > 2:
            chains.append(chain)
    return chains


class _ReachLadder(NamedTuple):
    """One reach's rating curve: its stage ladder and the columns being smoothed."""

    stages: np.ndarray
    lake_id: float
    columns: dict


def _reach_ladders(src, keys) -> dict[int, _ReachLadder]:
    """Split the SRC into one ladder per HydroID, for O(1) lookup by reach.

    Chain smoothing asks for one (reach, stage, key) value at a time; without this
    it re-filters the whole SRC table for every single one.
    """
    return {
        int(hid): _ReachLadder(
            stages=sub["Stage"].to_numpy(),
            lake_id=float(sub["LakeID"].iloc[0]),
            columns={k: sub[k].to_numpy() for k in keys},
        )
        for hid, sub in src.groupby("HydroID", sort=False)
    }


def _filter_key(src, chains, stages, key, ladders) -> pd.DataFrame:
    # Per chain: build a HydroID x stage matrix, smooth each stage column,
    # return long (HydroID, Stage, adj).
    stage_cols = [str(s) for s in stages]
    out_frames = []
    for chain in chains:
        rows = {}
        for pos, hid in enumerate(chain[:-1]):
            rows[hid] = _interp_ladder(ladders.get(int(hid)), key, stages) + [pos]
        mat = pd.DataFrame.from_dict(
            rows, orient="index", columns=stage_cols + ["long_position"]
        )
        smoothed = {s: list(_filter_voi(mat[s].values)) for s in stage_cols}
        out_frames.append(pd.DataFrame(smoothed, index=mat.index))
    if not out_frames:
        return pd.DataFrame(columns=["HydroID", "Stage", f"{key}_longitudinalAdjusted"])
    full = pd.concat(out_frames)
    long = (
        full.reset_index()
        .melt(
            id_vars="index", var_name="Stage", value_name=f"{key}_longitudinalAdjusted"
        )
        .rename(columns={"index": "HydroID"})
    )
    long["Stage"] = long["Stage"].astype(float)
    adj_col = f"{key}_longitudinalAdjusted"
    # A HydroID can sit in several chains (converging tributaries), giving
    # duplicate (HydroID, Stage) rows. Average them to one value per pair so
    # the downstream left-merge doesn't inflate the SRC.
    return (
        long[["HydroID", "Stage", adj_col]]
        .groupby(["HydroID", "Stage"], as_index=False)[adj_col]
        .mean()
    )


def _interp_ladder(ladder: Optional[_ReachLadder], key, stages) -> list:
    """One reach's key interpolated onto `stages`; NaN for lakes and unknown ids."""
    if ladder is None or ladder.lake_id > 0:
        return [np.nan] * len(stages)
    values = np.interp(stages, ladder.stages, ladder.columns[key])
    return [round(float(v), 3) for v in values]


@dataclass
class BathymetricAdjustment:
    # Add missing in-channel area + wetted perimeter below the DEM. eHydro
    # (.gpkg) first; AI depths (.parquet) for order >= ai_strm_order when
    # ai_toggle is on.

    aoi_dir: PathLike
    bathy_file_ehydro: Optional[PathLike] = None
    bathy_file_aibased: Optional[PathLike] = None
    ai_toggle: int = 0
    ai_strm_order: int = 4
    n_workers: Optional[int] = None

    def run(self) -> dict[str, str]:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        aoi_id = aoi_id_of(aoi_dir)
        log.info(f"BathymetricAdjustment: {aoi_id} (ai_toggle={self.ai_toggle})")

        results: dict[str, str] = {}
        if self.bathy_file_ehydro and Path(self.bathy_file_ehydro).is_file():
            log.info("--- eHydro bathymetry ---")
            results["ehydro"] = self._source(aoi_dir, "eHydro", self._ehydro_frame)
        else:
            log.info("eHydro bathymetry file absent — skipping eHydro step")

        if self.ai_toggle == 1:
            if self.bathy_file_aibased and Path(self.bathy_file_aibased).is_file():
                log.info("--- AI-based bathymetry ---")
                results["ai"] = self._source(aoi_dir, "AI-based", self._ai_frame)
            else:
                log.info("AI bathymetry file absent — skipping AI step")
        return results

    def _source(self, aoi_dir: Path, label: str, frame_fn) -> str:
        # The two sources are independent — a bad file for one shouldn't cost
        # the other, which is what happens when the whole step raises.
        try:
            bathy = frame_fn(aoi_dir)
        except Exception:
            log.exception(f"{label} bathymetry failed — skipping this source")
            return f"FAIL {label} bathymetry"

        # Say so out loud: an empty frame still writes every SRC, and reporting
        # that as a success is how a no-op source hides for a whole release.
        if bathy.empty:
            log.warning(f"{label} bathymetry: no channels over this AOI")
        else:
            log.info(f"{label} bathymetry: {len(bathy):,} channels over this AOI")
        return self._apply(aoi_dir, label, bathy)

    def _apply(self, aoi_dir: Path, label: str, bathy: pd.DataFrame) -> str:
        # Inject the missing-geometry table into every branch SRC, in parallel.
        branches = list(iter_branches(aoi_dir, exclude_zero=False))
        log.info(f"BathymetricAdjustment: {len(branches)} branches")
        out = _run_branches(
            branches,
            _bathy_one_branch,
            (bathy,),
            self.n_workers,
            "BathymetricAdjustment",
        )
        ok = sum(v.startswith("OK") for v in out.values())
        return (
            f"OK {label} bathymetry on {ok} branches"
            if not bathy.empty
            else (f"OK {label} bathymetry: nothing to add on {ok} branches")
        )

    def _ehydro_frame(self, aoi_dir: Path) -> pd.DataFrame:
        import geopandas as gpd

        path = Path(self.bathy_file_ehydro)
        wbd = aoi_dir / "wbd8_clp.gpkg"
        mask = gpd.read_file(wbd) if wbd.is_file() else None

        if path.suffix.lower() in (".parquet", ".pq"):
            # GeoParquet takes no reader-side mask, so read the national table
            # (a few thousand surveys) and cut it to the AOI with the index.
            bathy = gpd.read_parquet(path)
            if mask is not None and not mask.empty and bathy.crs is not None:
                hit = bathy.sindex.query(
                    mask.to_crs(bathy.crs).union_all(), predicate="intersects"
                )
                bathy = bathy.iloc[sorted(set(hit))]
        else:
            bathy = gpd.read_file(str(path), mask=mask, engine="fiona")
        return bathy.rename(columns={"ID": "feature_id"})

    def _ai_frame(self, aoi_dir: Path) -> pd.DataFrame:
        import geopandas as gpd

        ml = pd.read_parquet(self.bathy_file_aibased, engine="pyarrow")[
            [
                "hf_id",
                "owp_tw_inchan",
                "owp_inchan_channel_area",
                "owp_inchan_channel_perimeter",
            ]
        ]
        streams = aoi_dir / "nwmmr_subset_streams.gpkg"
        if not streams.is_file():
            streams = aoi_dir / "nwm_subset_streams.gpkg"
        wbd = aoi_dir / "wbd.gpkg"
        nwm = gpd.read_file(streams)
        if wbd.is_file():
            # wbd.gpkg is lat/lon while the streams are projected — clipping
            # across CRSs silently returns nothing instead of failing.
            nwm = nwm.clip(gpd.read_file(wbd).to_crs(nwm.crs))

        ml = ml.merge(nwm[["ID", "order_"]], left_on="hf_id", right_on="ID")
        ml = ml.rename(
            columns={
                "owp_inchan_channel_area": "missing_xs_area_m2",
                "ID": "feature_id",
            }
        )
        ml["missing_wet_perimeter_m"] = (
            ml["owp_inchan_channel_perimeter"] - ml["owp_tw_inchan"]
        )
        ml["Bathymetry_source"] = "AI_Based"
        # Below the order threshold, AI depths are unreliable — zero them out.
        below = ml["order_"] < self.ai_strm_order
        ml.loc[below, ["missing_xs_area_m2", "missing_wet_perimeter_m"]] = 0.0
        ml.loc[below, "Bathymetry_source"] = pd.NA
        return ml[
            [
                "feature_id",
                "missing_xs_area_m2",
                "missing_wet_perimeter_m",
                "Bathymetry_source",
            ]
        ]


_OHRFC_SOURCE = "OHRFC provided bathymetry, compiled from USACE data"


def _reconcile_bathy(bathy: pd.DataFrame) -> pd.DataFrame:
    # One row per feature_id: keep the OHRFC survey verbatim where it exists,
    # otherwise average the overlapping surveys' missing area / perimeter.
    def pick(group: pd.DataFrame) -> pd.Series:
        ohrfc = group[group["Bathymetry_source"] == _OHRFC_SOURCE]
        if not ohrfc.empty:
            return ohrfc.iloc[0]
        row = group.iloc[0].copy()
        row[["missing_xs_area_m2", "missing_wet_perimeter_m"]] = group[
            ["missing_xs_area_m2", "missing_wet_perimeter_m"]
        ].mean()
        return row

    return (
        bathy.groupby("feature_id", group_keys=False)
        .apply(pick, include_groups=False)
        .reset_index()
    )


_BATHY_MERGE_COLUMNS = [
    "feature_id",
    "missing_xs_area_m2",
    "missing_wet_perimeter_m",
    "Bathymetry_source",
]


def _bathy_one_branch(branch_dir: Path, bid: str, bathy: pd.DataFrame) -> str:
    # Merge missing geometry by feature_id, add it in, recompute discharge.
    src_path = branch_dir / f"src_full_crosswalked_{bid}.csv"
    if not src_path.is_file():
        return "SKIP no src"
    src = pd.read_csv(src_path, low_memory=False)

    # What an earlier source already folded into this SRC. Everything below works
    # off the difference against it, so the AI pass adds only its own depth on
    # top of eHydro's, and re-running the step on an adjusted SRC is a no-op
    # instead of burying the channel twice as deep.
    zeros = pd.Series(0.0, index=src.index)
    prev_area = pd.to_numeric(src.get("missing_xs_area_m2", zeros)).fillna(0.0)
    prev_perim = pd.to_numeric(src.get("missing_wet_perimeter_m", zeros)).fillna(0.0)
    prev_source = src.get("Bathymetry_source", pd.Series(pd.NA, index=src.index))
    # Dropped before the merge, or pandas would suffix the incoming columns to
    # _x / _y and every read of the plain name would raise.
    src = src.drop(columns=_BATHY_MERGE_COLUMNS[1:], errors="ignore")

    if bathy.empty:
        src["missing_xs_area_m2"] = prev_area
        src["missing_wet_perimeter_m"] = prev_perim
        src["Bathymetry_source"] = prev_source
        src.to_csv(src_path, index=False)
        return "OK no bathy overlap"

    # Collapse to one row per feature_id with OHRFC precedence (take the OHRFC
    # survey verbatim where present, else average the surveys), then merge.
    reconciled = _reconcile_bathy(bathy[_BATHY_MERGE_COLUMNS])
    src = src.merge(reconciled, on="feature_id", how="left", validate="many_to_one")

    new_area = pd.to_numeric(src["missing_xs_area_m2"], errors="coerce").fillna(0.0)
    new_perim = pd.to_numeric(src["missing_wet_perimeter_m"], errors="coerce").fillna(
        0.0
    )
    # This source speaks only for the reaches it actually covers; elsewhere the
    # earlier source's numbers stand.
    covered = new_area.gt(0) | new_perim.gt(0)
    src["missing_xs_area_m2"] = new_area.where(covered, prev_area)
    src["missing_wet_perimeter_m"] = new_perim.where(covered, prev_perim)
    src["Bathymetry_source"] = src["Bathymetry_source"].where(covered, prev_source)

    # Add the below-DEM area/perimeter into the geometry, then re-derive discharge.
    add_area = src["missing_xs_area_m2"] - prev_area
    add_perim = src["missing_wet_perimeter_m"] - prev_perim
    length_m = src["LENGTHKM"] * 1000.0
    src["Volume (m3)"] = src["Volume (m3)"] + add_area * length_m
    src["BedArea (m2)"] = src["BedArea (m2)"] + add_perim * length_m
    src["WettedPerimeter (m)"] = src["WettedPerimeter (m)"] + add_perim
    src["WetArea (m2)"] = src["WetArea (m2)"] + add_area
    src["HydraulicRadius (m)"] = (
        src["WetArea (m2)"] / src["WettedPerimeter (m)"]
    ).fillna(0)
    src["Discharge (m3s-1)"] = (
        src["WetArea (m2)"]
        * np.power(src["HydraulicRadius (m)"].clip(lower=0), 2.0 / 3)
        * np.power(src["SLOPE"].clip(lower=0), 0.5)
        / src["ManningN"]
    )

    # Zero-stage / zero-cell rows stay zero.
    src.loc[src["Stage"] == 0, "Discharge (m3s-1)"] = 0
    zero_cell = src["Number of Cells"] == 0
    for col in (
        "Discharge (m3s-1)",
        "BedArea (m2)",
        "Volume (m3)",
        "WettedPerimeter (m)",
        "WetArea (m2)",
        "HydraulicRadius (m)",
    ):
        src.loc[zero_cell, col] = 0

    src["Discharge (m3s-1)_bathymetryAdjusted"] = src["Discharge (m3s-1)"]
    src.to_csv(src_path, index=False)
    return f"OK bathymetry on {len(src)} rows"
