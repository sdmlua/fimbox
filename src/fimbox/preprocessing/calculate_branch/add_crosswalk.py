"""
Author: Supath Dhital
Date Updated: May 2026

Crosswalk DEM-derived reaches to NWM feature_ids, compute the full synthetic
rating curve from the SRC base table, and write the per-branch hydroTable.

The crosswalk uses the midpoint of each split reach. Each midpoint is joined
to the nearest NWM flowline; matches farther than ``max_distance_m`` are
discarded (a HydroID without a matching feature_id is dropped from downstream
products).

Hydraulic derivations (Manning's equation, in SI metres) per (HydroID, Stage):
    TopWidth         = SurfaceArea / (LengthKm * 1000)
    WettedPerimeter  = BedArea     / (LengthKm * 1000)
    WetArea          = Volume      / (LengthKm * 1000)
    HydraulicRadius  = WetArea / WettedPerimeter
    Discharge        = (WetArea * HydraulicRadius^(2/3) * sqrt(SLOPE)) / ManningN

Slope feeding Manning's equation is chosen by ``src_slope_source``. Three
slope variants are carried as columns so the choice is transparent:
    SLOPE_RISE_RUN    DEM-derived rise/run slope (from src_base)
    SLOPE_HFAB        hydrofabric native slope (Slope/So, or a named column)
    SLOPE_IRIS_SWORD  IRIS-SWORD slope merged from an external table by feature_id
Default ``iris_sword`` uses SLOPE_IRIS_SWORD on streams of order >= 4 (within
the valid slope range), falling back to SLOPE_RISE_RUN everywhere else.

Short-reach rating curve replacement (areasqkm < min_catchment_area AND
LengthKm < min_stream_length AND LakeID < 0) borrows the stage-discharge
table from the nearest upstream reach so the SRC stays monotonic across
artificially short fragments.

Inputs
------
catchments_gpkg : gw_catchments_reaches_filtered_addedAttributes_{id}.gpkg
flows_gpkg      : demDerived_reaches_split_filtered_{id}.gpkg
src_base_csv    : src_base_{id}.csv     (output of build_src)
boundary_gpkg   : AOI boundary file (optional metadata lookup, e.g. wbd8_clp.gpkg)
nwm_streams     : reference flowlines gpkg containing 'ID' / 'feature_id',
                  optional 'order_'

Outputs
-------
out_catchments_gpkg : *_crosswalked.gpkg  (catchments with feature_id + order_)
out_flows_gpkg      : *_crosswalked.gpkg  (flows with feature_id + ManningN)
out_src_csv         : src_full_crosswalked_{id}.csv  (per-stage hydraulics)
out_src_json        : src_{id}.json        (per-HydroID stage/q arrays)
out_crosswalk_csv   : crosswalk_table_{id}.csv       (HydroID --> feature_id)
out_hydro_csv       : hydroTable_{id}.csv            (final hydraulic table)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

# Slope sanity bounds — out-of-range slopes are clipped to SLOPE_MIN.
SLOPE_MIN = 9.999e-7
SLOPE_MAX = 0.5

# Stream order at/above which the IRIS-SWORD slope is trusted over the
# DEM rise/run slope (low-order DEM slopes are noisy but usually fine).
IRIS_MIN_STREAM_ORDER = 4

# Where each SRC slope source comes from:
#   "iris_sword" (default) -> IRIS-SWORD slope for order_ >= IRIS_MIN_STREAM_ORDER
#                             (and in range), else the DEM rise/run slope.
#   "dem"                  -> DEM rise/run slope only (the column from src_base).
#   "hfab"                 -> NWM hydrofabric slope (Slope/So), else DEM fallback.
VALID_SLOPE_SOURCES = ("iris_sword", "dem", "hfab")

# Default IRIS-SWORD slope table shipped with the package
# (fimbox/data/FIMHF_IRIS_v1.0.csv: columns feature_id, slope_iris_sword, ...).
DEFAULT_IRIS_SLOPE_CSV = (
    Path(__file__).resolve().parents[4] / "data" / "FIMHF_IRIS_v1.0.csv"
)


class NoCrosswalkError(RuntimeError):
    """Raised when no DEM-derived reach can be joined to an NWM flowline."""


def add_crosswalk(
    catchments_gpkg: PathLike,
    flows_gpkg: PathLike,
    src_base_csv: PathLike,
    nwm_streams_gpkg: PathLike,
    out_catchments_gpkg: PathLike,
    out_flows_gpkg: PathLike,
    out_src_csv: PathLike,
    out_src_json: PathLike,
    out_crosswalk_csv: PathLike,
    out_hydro_csv: PathLike,
    boundary_gpkg: Optional[PathLike] = None,
    mannings_n: float = 0.06,
    min_catchment_area: float = 0.25,
    min_stream_length: float = 0.5,
    max_distance_m: float = 100.0,
    small_segments_csv: Optional[PathLike] = None,
    src_slope_source: str = "iris_sword",
    iris_slope_csv: Optional[PathLike] = None,
    hfab_slope_column: Optional[str] = None,
) -> dict[str, Path]:
    """
    Run the full crosswalk and hydraulic table build. Returns a dict of output
    paths keyed by their role.
    """
    catchments_gpkg = Path(catchments_gpkg)
    flows_gpkg = Path(flows_gpkg)
    src_base_csv = Path(src_base_csv)
    boundary_gpkg = Path(boundary_gpkg) if boundary_gpkg is not None else None
    nwm_streams_gpkg = Path(nwm_streams_gpkg)
    out_catchments_gpkg = Path(out_catchments_gpkg)
    out_flows_gpkg = Path(out_flows_gpkg)
    out_src_csv = Path(out_src_csv)
    out_src_json = Path(out_src_json)
    out_crosswalk_csv = Path(out_crosswalk_csv)
    out_hydro_csv = Path(out_hydro_csv)
    for p in (
        out_catchments_gpkg,
        out_flows_gpkg,
        out_src_csv,
        out_src_json,
        out_crosswalk_csv,
        out_hydro_csv,
    ):
        p.parent.mkdir(parents=True, exist_ok=True)
    if small_segments_csv is not None:
        small_segments_csv = Path(small_segments_csv)

    if src_slope_source not in VALID_SLOPE_SOURCES:
        raise ValueError(
            f"src_slope_source must be one of {VALID_SLOPE_SOURCES}, "
            f"got {src_slope_source!r}"
        )
    # IRIS-SWORD slope is only needed when that source is selected. Fall back
    # to the table shipped with the package when the caller doesn't pass one.
    if src_slope_source == "iris_sword":
        iris_slope_csv = (
            Path(iris_slope_csv)
            if iris_slope_csv is not None
            else DEFAULT_IRIS_SLOPE_CSV
        )
    else:
        iris_slope_csv = None

    log.info("add_crosswalk: reading inputs (src_slope_source=%s)", src_slope_source)
    catchments = gpd.read_file(str(catchments_gpkg), engine="fiona")
    flows = gpd.read_file(str(flows_gpkg), engine="fiona")
    boundary = (
        gpd.read_file(str(boundary_gpkg), engine="fiona")
        if boundary_gpkg is not None and boundary_gpkg.exists()
        else None
    )
    nwm = gpd.read_file(str(nwm_streams_gpkg), engine="fiona")

    # Dissolve duplicate catchment polygons that survived filtering — these
    # appear when overlay + explode created small fragments per HydroID.
    catchments = catchments.dissolve(by="HydroID").reset_index()

    nwm = _prepare_nwm(nwm, iris_slope_csv, hfab_slope_column)

    # Reproject NWM streams to the catchment CRS so sjoin_nearest distances
    # are in the same projected metres used elsewhere in the pipeline.
    if nwm.crs is not None and catchments.crs is not None and nwm.crs != catchments.crs:
        nwm = nwm.to_crs(catchments.crs)

    crosswalk = _build_crosswalk(flows, nwm, max_distance_m)
    if crosswalk.empty:
        raise NoCrosswalkError(
            f"No DEM-derived reach is within {max_distance_m} m of a reference flowline"
        )

    catchments["HydroID"] = catchments["HydroID"].astype(int)
    flows["HydroID"] = flows["HydroID"].astype(int)

    output_catchments = catchments.merge(crosswalk, on="HydroID")
    output_flows = flows.merge(crosswalk, on="HydroID")

    if "areasqkm" not in output_catchments.columns:
        output_catchments["areasqkm"] = output_catchments.geometry.area / 1e6

    output_flows = output_flows.merge(
        output_catchments[["HydroID", "areasqkm"]], on="HydroID"
    ).drop_duplicates(subset="HydroID")
    output_flows["ManningN"] = float(mannings_n)
    output_flows["NextDownID"] = output_flows["NextDownID"].astype(int)

    # Identify short fragments and the upstream segment whose SRC should be
    # reused for them. The replacement uses stream order to disambiguate
    # when several upstreams feed the short reach (mainline wins).
    sml_segs = _find_short_segments(output_flows, min_catchment_area, min_stream_length)
    log.info(
        "add_crosswalk: %d short reaches flagged for SRC replacement",
        len(sml_segs),
    )

    # Build per-stage hydraulic table from the SRC base + Manning's n.
    src = _build_src_full(src_base_csv, output_flows, mannings_n, src_slope_source)

    # Apply short-segment rating curve replacements
    if not sml_segs.empty:
        if small_segments_csv is not None:
            sml_segs.to_csv(str(small_segments_csv), index=False)
        src = _apply_short_segment_replacement(src, sml_segs)

    src = src.merge(crosswalk[["HydroID", "feature_id"]], on="HydroID")
    src["default_SLOPE"] = src["SLOPE"]
    src["default_ManningN"] = src["ManningN"]

    crosswalk_table = src[["HydroID", "feature_id"]].drop_duplicates(ignore_index=True)

    hydro_table = _build_hydro_table(src, output_flows, boundary)
    src_json = _build_src_json(src)

    # Drop any pre-existing outputs so the writers can recreate them cleanly.
    for p in (out_catchments_gpkg, out_flows_gpkg):
        if p.exists():
            p.unlink()

    log.info(
        "add_crosswalk: writing %d catchments, %d flows, %d SRC rows",
        len(output_catchments),
        len(output_flows),
        len(src),
    )
    output_catchments.to_file(
        str(out_catchments_gpkg), driver="GPKG", index=False, engine="fiona"
    )
    output_flows.to_file(
        str(out_flows_gpkg), driver="GPKG", index=False, engine="fiona"
    )
    src.to_csv(str(out_src_csv), index=False)
    crosswalk_table.to_csv(str(out_crosswalk_csv), index=False)
    hydro_table.to_csv(str(out_hydro_csv), index=False)
    with out_src_json.open("w") as f:
        json.dump(src_json, f, sort_keys=True, indent=2)

    return {
        "crosswalked_catchments": out_catchments_gpkg,
        "crosswalked_flows": out_flows_gpkg,
        "src_full_csv": out_src_csv,
        "src_json": out_src_json,
        "crosswalk_table": out_crosswalk_csv,
        "hydro_table": out_hydro_csv,
    }


# Internal helpers
def _prepare_nwm(
    nwm: gpd.GeoDataFrame,
    iris_slope_csv: Optional[Path] = None,
    hfab_slope_column: Optional[str] = None,
) -> gpd.GeoDataFrame:
    """Normalise the NWM streams gdf to a feature_id-indexed table.

    Carries three slope columns alongside ``order_`` so the SRC builder can
    pick one (see ``src_slope_source``):
      * ``SLOPE_HFAB``        — the hydrofabric's native slope. Auto-detected
                                from ``Slope`` (CONUS) / ``So`` (Alaska), or set
                                explicitly via ``hfab_slope_column`` when the
                                hydrofabric names it something else.
      * ``SLOPE_IRIS_SWORD``  — merged from the IRIS-SWORD table by feature_id.
    The DEM rise/run slope is not here — it rides through ``src_base``.
    """
    if "ID" in nwm.columns:
        nwm = nwm.rename(columns={"ID": "feature_id"})
    if "feature_id" not in nwm.columns:
        raise ValueError("NWM streams file must contain an 'ID' or 'feature_id' column")
    nwm["feature_id"] = nwm["feature_id"].astype(int)

    if "order_" not in nwm.columns:
        # Some hydrofabric tiles use 'order' or 'streamOrde' — fall back gracefully.
        for cand in ("order", "streamOrde", "StreamOrde"):
            if cand in nwm.columns:
                nwm = nwm.rename(columns={cand: "order_"})
                break
        if "order_" not in nwm.columns:
            nwm["order_"] = 1

    # Hydrofabric native slope. Use the caller-named column when given;
    # otherwise auto-detect the common names (CONUS 'Slope', Alaska 'So').
    hfab_candidates = (
        [hfab_slope_column] if hfab_slope_column else ["Slope", "So", "SLOPE_HFAB"]
    )
    hfab_col = next((c for c in hfab_candidates if c and c in nwm.columns), None)
    if hfab_col == "SLOPE_HFAB":
        pass  # already named correctly
    elif hfab_col is not None:
        nwm = nwm.rename(columns={hfab_col: "SLOPE_HFAB"})
    else:
        nwm["SLOPE_HFAB"] = np.nan
        log.warning(
            "add_crosswalk: hydrofabric slope column %s not found in NWM streams; "
            "SLOPE_HFAB set to NaN",
            hfab_slope_column or "'Slope'/'So'",
        )

    # IRIS-SWORD slope, merged in by feature_id from the external table.
    nwm["SLOPE_IRIS_SWORD"] = np.nan
    if iris_slope_csv is not None and Path(iris_slope_csv).is_file():
        iris = pd.read_csv(iris_slope_csv)
        if "id" in iris.columns and "feature_id" not in iris.columns:
            iris = iris.rename(columns={"id": "feature_id"})
        if "slope_iris_sword" in iris.columns:
            iris = iris.rename(columns={"slope_iris_sword": "SLOPE_IRIS_SWORD"})
        if {"feature_id", "SLOPE_IRIS_SWORD"}.issubset(iris.columns):
            iris["feature_id"] = iris["feature_id"].astype(int)
            iris = iris[["feature_id", "SLOPE_IRIS_SWORD"]].drop_duplicates("feature_id")
            nwm = nwm.drop(columns=["SLOPE_IRIS_SWORD"]).merge(
                iris, on="feature_id", how="left"
            )
            log.info(
                "add_crosswalk: merged IRIS-SWORD slope for %d feature_ids",
                iris["SLOPE_IRIS_SWORD"].notna().sum(),
            )
        else:
            log.warning(
                "add_crosswalk: IRIS slope csv %s lacks feature_id/slope_iris_sword "
                "columns; SLOPE_IRIS_SWORD left NaN",
                iris_slope_csv,
            )
    elif iris_slope_csv is not None:
        log.warning(
            "add_crosswalk: IRIS slope csv not found: %s; SLOPE_IRIS_SWORD left NaN",
            iris_slope_csv,
        )

    return nwm.set_index("feature_id")


def _build_crosswalk(
    flows: gpd.GeoDataFrame,
    nwm_indexed: gpd.GeoDataFrame,
    max_distance_m: float,
) -> pd.DataFrame:
    """Nearest-neighbour join: split-reach midpoint --> NWM feature_id."""
    midpoints = gpd.GeoDataFrame(
        {
            "HydroID": flows["HydroID"].values,
            "geometry": [g.interpolate(0.5, normalized=True) for g in flows.geometry],
        },
        crs=flows.crs,
    ).set_index("HydroID")

    joined = (
        gpd.sjoin_nearest(midpoints, nwm_indexed, how="left", distance_col="distance")
        .reset_index()
        .rename(columns={"index_right": "feature_id"})
    )
    joined.loc[joined["distance"] > max_distance_m, "feature_id"] = pd.NA
    joined = joined.dropna(subset=["feature_id"]).copy()
    joined["feature_id"] = joined["feature_id"].astype(int)

    keep = ["HydroID", "feature_id", "distance"]
    slope_cols = [
        c
        for c in ("order_", "SLOPE_HFAB", "SLOPE_IRIS_SWORD")
        if c in nwm_indexed.columns
    ]
    out = joined[keep].merge(
        nwm_indexed[slope_cols].reset_index(),
        on="feature_id",
        how="left",
    )
    return out


def _find_short_segments(
    flows: gpd.GeoDataFrame, min_area: float, min_length: float
) -> pd.DataFrame:
    """
    Flag reaches that are tiny by both area and length and not in a lake.
    Replacement ID is the longest upstream reach (highest order_), with fallback
    to the longest downstream sibling when no upstream feeds the short reach.
    """
    out_rows: list[dict] = []
    if "LakeID" not in flows.columns:
        flows = flows.assign(LakeID=-999)

    for _, row in flows.iterrows():
        if not (
            row["areasqkm"] < min_area
            and row["LengthKm"] < min_length
            and row["LakeID"] < 0
        ):
            continue
        short_id = int(row["HydroID"])
        to_node = row["To_Node"]
        from_node = row["From_Node"]

        upstreams = flows[flows["NextDownID"] == short_id]
        if len(upstreams) >= 1:
            update_id = int(upstreams.loc[upstreams["order_"].idxmax(), "HydroID"])
        else:
            siblings = flows[flows["From_Node"] == to_node]
            siblings = siblings[siblings["HydroID"] != short_id]
            if len(siblings) >= 1:
                update_id = int(siblings.loc[siblings["order_"].idxmax(), "HydroID"])
            else:
                continue  # no neighbour to borrow from — leave SRC untouched

        out_rows.append(
            {
                "short_id": short_id,
                "update_id": update_id,
                "str_order": int(row.get("order_", 0)),
            }
        )

    return pd.DataFrame(out_rows)


def _select_slope(base: pd.DataFrame, src_slope_source: str) -> pd.Series:
    """Pick the per-reach slope feeding Manning's equation.

    ``iris_sword`` (default): IRIS-SWORD slope for streams of order
    >= IRIS_MIN_STREAM_ORDER that are in range, else the DEM rise/run slope.
    ``hfab``: hydrofabric slope where in range, else DEM. ``dem``: DEM only.
    All fall back to the DEM rise/run slope when the preferred source is
    missing, so SLOPE is never NaN."""
    dem = pd.to_numeric(base.get("SLOPE_RISE_RUN"), errors="coerce")

    def _in_range(col: str) -> pd.Series:
        if col not in base.columns:
            return pd.Series(np.nan, index=base.index)
        v = pd.to_numeric(base[col], errors="coerce")
        return v.where((v >= SLOPE_MIN) & (v <= SLOPE_MAX))

    if src_slope_source == "dem":
        return dem

    if src_slope_source == "hfab":
        return _in_range("SLOPE_HFAB").combine_first(dem)

    # iris_sword: only trusted on higher-order streams, else DEM.
    iris = _in_range("SLOPE_IRIS_SWORD")
    if "order_" in base.columns:
        order = pd.to_numeric(base["order_"], errors="coerce")
        iris = iris.where(order >= IRIS_MIN_STREAM_ORDER)
    return iris.combine_first(dem)


def _build_src_full(
    src_base_csv: Path,
    flows: pd.DataFrame,
    mannings_n: float,
    src_slope_source: str = "iris_sword",
) -> pd.DataFrame:
    """Compute per-stage Manning hydraulics from the base SRC table.

    The DEM rise/run slope rides through src_base (column ' SLOPE', with the
    leading space) and is preserved as ``SLOPE_RISE_RUN``. The final ``SLOPE``
    used in Manning's equation is chosen by ``src_slope_source`` (see
    ``VALID_SLOPE_SOURCES``), then clipped to [SLOPE_MIN, SLOPE_MAX] and
    rounded to 3 sig-fig scientific to avoid spurious precision."""
    base = pd.read_csv(str(src_base_csv))
    base = base.rename(columns=lambda c: c.strip(" "))

    base["CatchId"] = base["CatchId"].astype(int)
    flow_cols = [
        c
        for c in ("HydroID", "NextDownID", "order_", "SLOPE_HFAB", "SLOPE_IRIS_SWORD")
        if c in flows.columns
    ]
    base = base.merge(flows[flow_cols], left_on="CatchId", right_on="HydroID")
    base["ManningN"] = float(mannings_n)

    # Preserve the DEM rise/run slope under its explicit name before choosing.
    base["SLOPE_RISE_RUN"] = pd.to_numeric(base["SLOPE"], errors="coerce")
    chosen = _select_slope(base, src_slope_source)

    # Sanity-bound the chosen slope so Manning's equation stays well-defined.
    chosen = chosen.where((chosen >= SLOPE_MIN) & (chosen <= SLOPE_MAX), other=SLOPE_MIN)
    base["SLOPE"] = chosen.apply(lambda x: float(f"{x:.3e}"))

    # Hydraulic geometry — all per-segment averaged values.
    length_m = base["LENGTHKM"] * 1000.0
    base["TopWidth (m)"] = base["SurfaceArea (m2)"] / length_m
    base["WettedPerimeter (m)"] = base["BedArea (m2)"] / length_m
    base["WetArea (m2)"] = base["Volume (m3)"] / length_m
    base["HydraulicRadius (m)"] = (
        base["WetArea (m2)"] / base["WettedPerimeter (m)"]
    ).fillna(0.0)

    base["Discharge (m3s-1)"] = (
        base["WetArea (m2)"]
        * np.power(base["HydraulicRadius (m)"].clip(lower=0.0), 2.0 / 3.0)
        * np.sqrt(base["SLOPE"].clip(lower=0.0))
        / base["ManningN"]
    )
    base.loc[base["Stage"] == 0, "Discharge (m3s-1)"] = 0.0
    base["Bathymetry_source"] = pd.NA

    return base.drop(columns=["CatchId"]).copy()


def _apply_short_segment_replacement(
    src: pd.DataFrame, sml_segs: pd.DataFrame
) -> pd.DataFrame:
    """Replace each short reach's stage-->discharge curve with its donor's curve."""
    donor = src[["HydroID", "Stage", "Discharge (m3s-1)"]].rename(
        columns={"HydroID": "update_id", "Discharge (m3s-1)": "Q_donor"}
    )
    merged = sml_segs.merge(donor, on="update_id")
    merged = merged.rename(columns={"short_id": "HydroID"})[
        ["HydroID", "Stage", "Q_donor"]
    ]
    out = src.merge(merged, on=["HydroID", "Stage"], how="left")
    out["Discharge (m3s-1)"] = out["Q_donor"].fillna(out["Discharge (m3s-1)"])
    return out.drop(columns=["Q_donor"])


def _build_hydro_table(
    src: pd.DataFrame,
    flows: pd.DataFrame,
    boundary: Optional[gpd.GeoDataFrame],
) -> pd.DataFrame:
    """Project the full SRC into the hydroTable schema."""
    cols = [
        "HydroID",
        "feature_id",
        "NextDownID",
        "order_",
        "Number of Cells",
        "SurfaceArea (m2)",
        "BedArea (m2)",
        "TopWidth (m)",
        "LENGTHKM",
        "WettedPerimeter (m)",
        "HydraulicRadius (m)",
        "WetArea (m2)",
        "Volume (m3)",
        "SLOPE",
        "SLOPE_RISE_RUN",
        "SLOPE_HFAB",
        "SLOPE_IRIS_SWORD",
        "ManningN",
        "Stage",
        "Discharge (m3s-1)",
    ]
    cols = [c for c in cols if c in src.columns]
    ht = src.loc[:, cols].rename(
        columns={"Stage": "stage", "Discharge (m3s-1)": "discharge_cms"}
    )
    # The boundary file is accepted for future per-AOI metadata enrichment;
    # keep the parameter wired so callers don't have to drop it later.
    _ = boundary
    ht["LakeID"] = (
        flows.set_index("HydroID")["LakeID"]
        .reindex(ht["HydroID"])
        .fillna(-999)
        .astype(int)
        .values
    )

    ht["HydroID"] = ht["HydroID"].astype(str)
    ht["feature_id"] = ht["feature_id"].astype(int).astype(str)
    return ht


def _build_src_json(src: pd.DataFrame) -> dict[str, dict[str, list]]:
    """One entry per HydroID: ordered stage / discharge arrays."""
    out: dict[str, dict[str, list]] = {}
    for hid, group in src.groupby("HydroID", sort=True):
        out[str(int(hid))] = {
            "stage_list": group["Stage"].astype(float).tolist(),
            "q_list": group["Discharge (m3s-1)"].astype(float).tolist(),
        }
    return out
