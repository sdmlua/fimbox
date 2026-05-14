"""
Author: Supath Dhital
Date Updated: May 2026

Synthetic Rating Curve (SRC) base-table builder.

Python port of TauDEM ``catchhydrogeo``: for every (HydroID, Stage) pair the
inundated channel geometry is integrated from the zeroed/masked HAND raster
within the catchment footprint. The output CSV columns match what FIM's
``add_crosswalk.py`` expects so the downstream hydraulic-table merge runs
unmodified.

Algorithm (per catchment, per stage h):
    inundated      = HAND <= h within the catchment
    Number of Cells = count(inundated)
    SurfaceArea    = N * pixel_area                   # planar water surface
    Volume         = Σ (h - HAND_i) * pixel_area      # depth integral
    BedArea        = Σ pixel_area * sqrt(1 + slope_i^2)  # wetted bed (accounts for terrain)

Output schema (CSV with leading-space ' SLOPE' kept for FIM compatibility):
    CatchId, Number of Cells, SurfaceArea (m2), BedArea (m2),
    Volume (m3), Stage,  SLOPE, LENGTHKM

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
        hand_nodata = hand_ds.nodata if hand_ds.nodata is not None else -9999.0
        pixel_area = abs(hand_ds.transform.a * hand_ds.transform.e)  # m^2 per cell
        hand_arr = hand_ds.read(1).astype(np.float32)

    with rasterio.open(str(catch_raster)) as cat_ds:
        cat_arr = cat_ds.read(1).astype(np.int64)

    with rasterio.open(str(slope_raster)) as slp_ds:
        slope_nodata = slp_ds.nodata if slp_ds.nodata is not None else -9999.0
        slope_arr = slp_ds.read(1).astype(np.float32)

    # Vectorised per-catchment accumulation via np.bincount.
    # All catchment pixels with valid HAND contribute; missing slope falls
    # back to zero (treated as flat bed, the FIM default behaviour).
    valid_pix = (cat_arr > 0) & (hand_arr != hand_nodata)
    if not valid_pix.any():
        log.warning("build_src: no valid (catchment, HAND) pixels found")

    flat_cat = cat_arr[valid_pix]
    flat_hand = hand_arr[valid_pix]
    flat_slope = slope_arr[valid_pix]
    flat_slope = np.where(flat_slope == slope_nodata, 0.0, flat_slope)

    # Per-cell wetted bed factor: sqrt(1 + slope^2) accounts for the terrain
    # tilt of each cell — flat cells contribute pixel_area, steep cells contribute
    # more bed area than their planar projection. This is the same expression
    # TauDEM catchhydrogeo uses for the BedArea integral.
    bed_factor = np.sqrt(1.0 + flat_slope.astype(np.float64) ** 2)
    bed_area_per_cell = pixel_area * bed_factor  # m^2

    # Map each unique HydroID to a dense 0..K-1 index for bincount.
    unique_hids, inverse = np.unique(flat_cat, return_inverse=True)
    n_catch = len(unique_hids)

    # Build pre-summed quantities per catchment that can be combined with
    # any stage h without re-scanning pixels:
    #   N_le_h    = count of cells with HAND <= h
    #   sum_hand  = sum of HAND for cells with HAND <= h
    #   sum_bed   = sum of bed_area for cells with HAND <= h
    # We achieve this by first sorting pixels by HAND within each catchment,
    # then for each stage doing a searchsorted lookup. Memory: O(n_pixels);
    # time: O(n_pixels log n_pixels + n_stages * n_catchments).
    order = np.lexsort((flat_hand, inverse))
    sorted_inv = inverse[order]
    sorted_hand = flat_hand[order]
    sorted_bed = bed_area_per_cell[order]

    # Per-catchment slice boundaries in the sorted arrays.
    counts = np.bincount(sorted_inv, minlength=n_catch)
    starts = np.concatenate([[0], np.cumsum(counts)])

    # Cumulative arrays inside each catchment slice for fast partial sums.
    cum_hand = np.zeros_like(sorted_hand, dtype=np.float64)
    cum_bed = np.zeros_like(sorted_bed, dtype=np.float64)
    for k in range(n_catch):
        s, e = starts[k], starts[k + 1]
        if s == e:
            continue
        cum_hand[s:e] = np.cumsum(sorted_hand[s:e], dtype=np.float64)
        cum_bed[s:e] = np.cumsum(sorted_bed[s:e], dtype=np.float64)

    # Build the per-row records. Order rows by HydroID (ascending stage within)
    # to match the file layout produced by TauDEM catchhydrogeo.
    rows: list[dict] = []
    hid_to_k: dict[int, int] = {int(h): i for i, h in enumerate(unique_hids)}

    for hid in hydro_ids:
        s0, length_km, _area_sq_km = catch_meta[hid]
        k = hid_to_k.get(int(hid))
        if k is None:
            # No raster pixels for this catchment — emit zero-volume rows so
            # the downstream merge still produces a stage ladder for this HydroID.
            for h in stages:
                rows.append(
                    {
                        "CatchId": int(hid),
                        "Number of Cells": 0,
                        "SurfaceArea (m2)": 0.0,
                        "BedArea (m2)": 0.0,
                        "Volume (m3)": 0.0,
                        "Stage": float(h),
                        " SLOPE": float(s0),
                        "LENGTHKM": float(length_km),
                    }
                )
            continue

        s, e = starts[k], starts[k + 1]
        catch_hand = sorted_hand[s:e]
        catch_cum_hand = cum_hand[s:e]
        catch_cum_bed = cum_bed[s:e]

        # For each stage, find the count of pixels with HAND <= h and use
        # cumulative arrays to assemble the per-stage geometry totals.
        # side='right' so a pixel with HAND exactly equal to h is included.
        idx = np.searchsorted(catch_hand, stages, side="right")

        for h, n_le in zip(stages, idx):
            if n_le == 0:
                rows.append(
                    {
                        "CatchId": int(hid),
                        "Number of Cells": 0,
                        "SurfaceArea (m2)": 0.0,
                        "BedArea (m2)": 0.0,
                        "Volume (m3)": 0.0,
                        "Stage": float(h),
                        " SLOPE": float(s0),
                        "LENGTHKM": float(length_km),
                    }
                )
                continue
            n_int = int(n_le)
            sum_hand_le = float(catch_cum_hand[n_int - 1])
            sum_bed_le = float(catch_cum_bed[n_int - 1])
            surface_area = n_int * pixel_area
            # Volume = Σ (h - HAND_i) * pixel_area = h*N*A - A*Σ HAND_i
            volume = (float(h) * n_int * pixel_area) - (sum_hand_le * pixel_area)
            rows.append(
                {
                    "CatchId": int(hid),
                    "Number of Cells": n_int,
                    "SurfaceArea (m2)": surface_area,
                    "BedArea (m2)": sum_bed_le,
                    "Volume (m3)": max(volume, 0.0),
                    "Stage": float(h),
                    " SLOPE": float(s0),
                    "LENGTHKM": float(length_km),
                }
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
