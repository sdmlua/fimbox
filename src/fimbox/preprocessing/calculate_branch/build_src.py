"""
Author: Supath Dhital
Date Updated: June 2026

Developing the Synthetic Rating Curve (SRC) geometry table.

For each catchment and each stage height ``h``, accumulate over the catchment's
cells:
    * WET condition is ``HAND < h`` (strict) OR ``|HAND| < 1e-6`` (a zero-HAND
      channel cell is wet at every stage, including stage 0).
    * A cell is dropped entirely when its slope (or HAND, or catchment) is
      nodata — it contributes to no accumulator.
    * BedArea uses the per-cell wetted-bed factor ``sqrt(1 + slope^2)``.
    * Volume is the depth integral ``Σ (h - HAND_i) * cellArea``.

Cell area:
The pipeline runs in projected metres (EPSG:5070 CONUS Albers by default),
where every cell has the same area ``|transform.a * transform.e|``. We use that
constant and emit a WARNING if the raster is geographic, since a single planar
cell area would then be wrong (cell width in metres varies with latitude).

Output schema — the ``SLOPE`` column is written with a leading space
(``" SLOPE"``); the downstream ``add_crosswalk`` strips it on read:
    CatchId, Stage, Number of Cells, SurfaceArea (m2), BedArea (m2),
    Volume (m3),  SLOPE, LENGTHKM, AREASQKM

Inputs
------
hand_raster   : rem_zeroed_masked_{id}.tif    (float32, masked to catchments)
catch_raster  : gw_catchments_reaches_filtered_addedAttributes_{id}.tif (int32 HydroID)
slope_raster  : slopes_d8_dem_meters_masked_{id}.tif (float32, rise/run)
catchlist_txt : catch_list_{id}.txt           (HydroID S0 LengthKm areasqkm rows)
stages_txt    : stage_{id}.txt                (one stage per line; header 'Stage')
out_csv       : src_base_{id}.csv
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd
import rasterio

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def build_src_base(
    hand_raster: PathLike,
    catch_raster: PathLike,
    slope_raster: PathLike,
    catchlist_txt: PathLike,
    stages_txt: PathLike,
    out_csv: PathLike,
) -> Path:
    """
    Build the SRC base CSV consumed by add_crosswalk.

    Returns the output CSV path. Skips work and returns the existing file
    when ``out_csv`` already exists and is non-empty.
    """
    hand_raster = Path(hand_raster)
    catch_raster = Path(catch_raster)
    slope_raster = Path(slope_raster)
    catchlist_txt = Path(catchlist_txt)
    stages_txt = Path(stages_txt)
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    if out_csv.exists() and out_csv.stat().st_size > 0:
        log.info("build_src: output exists, skipping --> %s", out_csv.name)
        return out_csv

    # A HAND value within this tolerance of zero is treated as a channel cell
    # (wet at every stage).
    ZERO_TOL = 1e-6

    stages = _read_stages(stages_txt)
    catch_meta = _read_catchlist(catchlist_txt)  # HydroID --> (S0, LengthKm, areasqkm)
    hydro_ids = list(catch_meta.keys())

    log.info(
        "build_src: %d hydroIDs × %d stages = %d SRC rows",
        len(hydro_ids),
        len(stages),
        len(hydro_ids) * len(stages),
    )

    with rasterio.open(str(hand_raster)) as hand_ds:
        hand_nodata = hand_ds.nodata
        # Cell area is constant for projected rasters; warn if geographic,
        # where one planar cell area would be wrong (cell width in metres
        # varies with latitude).
        if hand_ds.crs is not None and hand_ds.crs.is_geographic:
            log.warning(
                "build_src: HAND raster is geographic (%s); a single planar "
                "cell area is used, which is inaccurate. Reproject to a metric "
                "CRS (e.g. EPSG:5070) for a faithful SRC.",
                hand_ds.crs.to_string(),
            )
        pixel_area = abs(hand_ds.transform.a * hand_ds.transform.e)  # m^2 per cell
        hand_arr = hand_ds.read(1).astype(np.float64)

    with rasterio.open(str(catch_raster)) as cat_ds:
        cat_arr = cat_ds.read(1).astype(np.int64)

    with rasterio.open(str(slope_raster)) as slp_ds:
        slope_nodata = slp_ds.nodata
        slope_arr = slp_ds.read(1).astype(np.float64)

    # A cell contributes only when catch / HAND / slope are ALL valid.
    # nodata-slope cells are dropped entirely (they contribute to no
    # accumulator), not coerced to a flat bed.
    valid_pix = cat_arr > 0
    if hand_nodata is not None:
        valid_pix &= hand_arr != hand_nodata
    valid_pix &= ~np.isnan(hand_arr)
    if slope_nodata is not None:
        valid_pix &= slope_arr != slope_nodata
    valid_pix &= ~np.isnan(slope_arr)
    if not valid_pix.any():
        log.warning("build_src: no valid (catchment, HAND, slope) pixels found")

    flat_cat = cat_arr[valid_pix]
    flat_hand = hand_arr[valid_pix]
    flat_slope = slope_arr[valid_pix]

    # Per-cell wetted bed factor: sqrt(1 + slope^2) accounts for the terrain
    # tilt of each cell — flat cells contribute pixel_area, steep cells contribute
    # more bed area than their planar projection.
    bed_factor = np.sqrt(1.0 + flat_slope**2)
    bed_area_per_cell = pixel_area * bed_factor  # m^2

    # The wet test is `HAND < h` (strict) OR `|HAND| < 1e-6`. The second clause
    # makes a zero-HAND channel cell wet at EVERY stage. We handle the two
    # populations separately so the strict `<` is exact:
    #   * zero-HAND cells  -> always wet (counted at every stage)
    #   * positive cells   -> wet when HAND < h, via searchsorted(side='left')
    is_zero = np.abs(flat_hand) < ZERO_TOL
    pos_mask = ~is_zero

    # Map each unique HydroID to a dense 0..K-1 index.
    unique_hids, inverse = np.unique(flat_cat, return_inverse=True)
    n_catch = len(unique_hids)
    hid_to_k: dict[int, int] = {int(h): i for i, h in enumerate(unique_hids)}

    # --- Zero-HAND (always-wet) per-catchment totals (stage-independent). ---
    # Volume contribution of a zero-HAND cell at stage h is (h - 0) * area.
    zero_count = np.bincount(inverse[is_zero], minlength=n_catch)
    zero_bed = np.bincount(
        inverse[is_zero], weights=bed_area_per_cell[is_zero], minlength=n_catch
    )
    # Σ HAND over zero cells ≈ 0 by definition, so their volume per stage is
    # simply h * (count * pixel_area); no HAND sum needed.

    # --- Positive-HAND cells: sort within catchment for cumulative partial sums. ---
    pos_inv = inverse[pos_mask]
    pos_hand = flat_hand[pos_mask]
    pos_bed = bed_area_per_cell[pos_mask]

    order = np.lexsort((pos_hand, pos_inv))
    sorted_inv = pos_inv[order]
    sorted_hand = pos_hand[order]
    sorted_bed = pos_bed[order]

    counts = np.bincount(sorted_inv, minlength=n_catch)
    starts = np.concatenate([[0], np.cumsum(counts)])

    cum_hand = np.zeros_like(sorted_hand, dtype=np.float64)
    cum_bed = np.zeros_like(sorted_bed, dtype=np.float64)
    for k in range(n_catch):
        s, e = starts[k], starts[k + 1]
        if s == e:
            continue
        cum_hand[s:e] = np.cumsum(sorted_hand[s:e], dtype=np.float64)
        cum_bed[s:e] = np.cumsum(sorted_bed[s:e], dtype=np.float64)

    # Build the per-row records, ordered by HydroID then ascending stage.
    rows: list[dict] = []

    def _row(hid, h, n, surf, bed, vol, s0, length_km, area_sq_km):
        # ' SLOPE' keeps the leading space that add_crosswalk strips on read.
        return {
            "CatchId": int(hid),
            "Stage": float(h),
            "Number of Cells": int(n),
            "SurfaceArea (m2)": float(surf),
            "BedArea (m2)": float(bed),
            "Volume (m3)": float(vol),
            " SLOPE": float(s0),
            "LENGTHKM": float(length_km),
            "AREASQKM": float(area_sq_km),
        }

    for hid in hydro_ids:
        s0, length_km, area_sq_km = catch_meta[hid]
        k = hid_to_k.get(int(hid))

        # Stage-independent zero-HAND contribution for this catchment.
        zc = int(zero_count[k]) if k is not None else 0
        zb = float(zero_bed[k]) if k is not None else 0.0
        z_surface = zc * pixel_area

        if k is None:
            # No raster pixels at all — still emit a full stage ladder so the
            # downstream merge produces a (zeroed) curve for this HydroID.
            for h in stages:
                rows.append(_row(hid, h, 0, 0.0, 0.0, 0.0, s0, length_km, area_sq_km))
            continue

        s, e = starts[k], starts[k + 1]
        catch_hand = sorted_hand[s:e]
        catch_cum_hand = cum_hand[s:e]
        catch_cum_bed = cum_bed[s:e]

        # Positive-HAND cells wet at stage h: HAND < h (strict) -> side='left'.
        idx = np.searchsorted(catch_hand, stages, side="left")

        for h, n_lt in zip(stages, idx):
            n_pos = int(n_lt)
            if n_pos > 0:
                sum_hand_lt = float(catch_cum_hand[n_pos - 1])
                sum_bed_lt = float(catch_cum_bed[n_pos - 1])
            else:
                sum_hand_lt = 0.0
                sum_bed_lt = 0.0

            # Combine the always-wet zero cells with the stage-wet positive cells.
            n_total = n_pos + zc
            surface_area = (n_pos * pixel_area) + z_surface
            bed_area = sum_bed_lt + zb
            # Volume = Σ_pos (h - HAND_i) A + Σ_zero (h - 0) A
            volume = (
                (float(h) * n_pos * pixel_area) - (sum_hand_lt * pixel_area)
            ) + (float(h) * zc * pixel_area)

            rows.append(
                _row(
                    hid,
                    h,
                    n_total,
                    surface_area,
                    bed_area,
                    max(volume, 0.0),
                    s0,
                    length_km,
                    area_sq_km,
                )
            )

    df = pd.DataFrame.from_records(rows)
    df.to_csv(str(out_csv), index=False)

    log.info("build_src: written %d rows --> %s", len(df), out_csv.name)
    return out_csv


def _read_stages(stages_txt: Path) -> np.ndarray:
    """Return the stage column as a 1-D float array (skips the 'Stage' header)."""
    with stages_txt.open("r") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if lines and lines[0].lower().startswith("stage"):
        lines = lines[1:]
    return np.asarray([float(x) for x in lines], dtype=np.float64)


def _read_catchlist(catchlist_txt: Path) -> dict[int, tuple[float, float, float]]:
    """
    Parse catch_list_{id}.txt into ``HydroID -> (S0, LengthKm, areasqkm)``.
    First row is the count of HydroIDs and is skipped.
    """
    out: dict[int, tuple[float, float, float]] = {}
    with catchlist_txt.open("r") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    # Skip header count line if it is a single integer.
    if lines and len(lines[0].split()) == 1:
        lines = lines[1:]
    for ln in lines:
        parts = ln.split()
        if len(parts) < 4:
            continue
        hid = int(parts[0])
        s0 = float(parts[1])
        length_km = float(parts[2])
        area_sq_km = float(parts[3])
        out[hid] = (s0, length_km, area_sq_km)
    return out
