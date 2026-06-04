"""
Author: Supath Dhital
Date Updated: May 2026

Encoding
--------
- gw_catchments_reaches_filtered_addedAttributes_{B}.tif
    HydroIDs are 32-bit integers (commonly 8-digit codes like 18640001).
    We store only the **last 4 digits** as Int16 and write the common 4-digit
    prefix (e.g. "1864") into ``<branch_dir>/hydroid_prefix.txt`` so the
    original IDs can be reconstructed:
        original_HydroID = stored_value + prefix * 10000
    Nodata is preserved from the source raster.

- rem_zeroed_masked_{B}.tif
    HAND values (m) up to 32.766 m are kept (anything taller is clipped to
    32.766). Values are stored as ``round(hand * 1000)`` so 1 mm of HAND
    resolution survives the cast. The Int16 sentinel ``32767`` is used as
    nodata.

Raises
------
ValueError when a catchment raster has too many unique HydroIDs (>32766) or
HydroIDs with more than 8 digits — Int16 can't represent them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

import numpy as np

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

# Int16 nodata sentinel used for the REM. 32767 is the max Int16 value.
_REM_NODATA = np.int16(32767)
_REM_CLIP_M = 32.766  # m, max HAND value that survives the cast


class CannotConvertHydroIDsToInt16(ValueError):
    """Raised when a catchment raster can't be losslessly downcast to Int16."""


def convert_branch_to_int16(branch_dir: PathLike) -> None:
    """Downcast catchments and REM rasters in one branch directory.

    Writes ``*_int32.tif`` / ``*_float32.tif`` siblings as backups of the
    originals so deny-list cleanup can remove them when not needed."""
    branch_dir = Path(branch_dir)
    catchments = sorted(
        branch_dir.glob("gw_catchments_reaches_filtered_addedAttributes_*.tif")
    )
    # Skip the int32 backups, the rasterized-non-gpkg companion file
    catchments = [p for p in catchments if not p.stem.endswith("_int32")]
    rems = sorted(branch_dir.glob("rem_zeroed_masked_*.tif"))
    rems = [p for p in rems if not p.stem.endswith("_float32")]

    if not catchments or not rems:
        log.info(
            f"convert_to_int16: no catchment/REM pair found under {branch_dir} — skipping"
        )
        return

    hydroid_prefix: Optional[int] = None
    prefix_path = branch_dir / "hydroid_prefix.txt"

    for catch_path, rem_path in zip(catchments, rems):
        hydroid_prefix = _convert_catchment(catch_path, hydroid_prefix)
        _convert_rem(rem_path)

    if hydroid_prefix is not None and not prefix_path.exists():
        prefix_path.write_text(str(hydroid_prefix))
        log.info(f"hydroid_prefix={hydroid_prefix} --> {prefix_path.name}")


def _convert_catchment(path: Path, hydroid_prefix: Optional[int]) -> int:
    import rioxarray as rxr
    import xarray as xr

    catchment = rxr.open_rasterio(path)
    arr = catchment.data
    nodata = catchment.rio.nodata
    crs = catchment.rio.crs

    valid_mask = arr != nodata if nodata is not None else np.ones_like(arr, dtype=bool)
    unique_ids = np.unique(arr[valid_mask])
    if unique_ids.size > 32766:
        raise CannotConvertHydroIDsToInt16(
            f"{path.name}: {unique_ids.size} unique HydroIDs exceed Int16 capacity"
        )
    max_id = int(unique_ids.max()) if unique_ids.size else 0
    if len(str(max_id)) > 8:
        raise CannotConvertHydroIDsToInt16(
            f"{path.name}: HydroID {max_id} has more than 8 digits"
        )

    if hydroid_prefix is None:
        hydroid_prefix = int(np.floor(max_id / 10000)) if max_id else 0

    # Save the Int32 original as a backup sibling so deny-list cleanup can keep discard it later. We re-read with the same rxr handle to preserve
    # CRS / nodata metadata.
    backup = path.with_name(path.stem + "_int32" + path.suffix)
    catchment.rio.to_raster(backup, compress="LZW", tiled=True)

    # Strip the common 4-digit prefix (e.g. 18640001 -> 0001) so the last 4 digits fit in Int16. Nodata pixels are left alone.
    shifted = xr.where(
        catchment != nodata, catchment - hydroid_prefix * 10000, catchment
    )
    shifted = shifted.astype(np.int16)
    shifted.rio.write_nodata(nodata, inplace=True)
    shifted.rio.write_crs(crs, inplace=True)
    shifted.rio.to_raster(path, dtype=np.int16, compress="LZW", tiled=True)
    log.info(f"Int16 catchment (prefix={hydroid_prefix}) --> {path.name}")
    return hydroid_prefix


def _convert_rem(path: Path) -> None:
    import rioxarray as rxr
    import xarray as xr

    rem = rxr.open_rasterio(path)
    crs = rem.rio.crs

    backup = path.with_name(path.stem + "_float32" + path.suffix)
    rem.rio.to_raster(backup, compress="LZW", tiled=True)

    # Cap HAND values at the largest Int16-encodable HAND in 1mm units (32.766 m) and encode invalid pixels with the Int16 sentinel.
    capped = xr.where(rem > _REM_CLIP_M, _REM_CLIP_M, rem)
    encoded = xr.where(capped >= 0, np.round(capped * 1000), float(_REM_NODATA))
    encoded = encoded.astype(np.int16)
    encoded.rio.write_nodata(_REM_NODATA, inplace=True)
    encoded.rio.write_crs(crs, inplace=True)
    encoded.rio.to_raster(path, dtype=np.int16, compress="LZW", tiled=True)
    log.info(f"Int16 REM (×1000, nodata={_REM_NODATA}) --> {path.name}")


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="Downcast gw_catchments and rem_zeroed_masked rasters to Int16."
    )
    parser.add_argument("-b", "--branch-dir", required=True)
    args = parser.parse_args()
    convert_branch_to_int16(args.branch_dir)
