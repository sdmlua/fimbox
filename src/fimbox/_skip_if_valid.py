"""
Skip-if-valid helper.

A step's output is considered usable when the file exists *and* opens
cleanly in its native format. Crashes mid-write leave truncated
GeoTIFFs / GeoPackages that ``.exists()`` happily returns True for —
this module catches those and deletes them so the calling step reruns
instead of silently propagating corrupt data downstream.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def is_valid(path: PathLike) -> bool:
    """Return True if ``path`` exists and parses cleanly in its native format."""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        size = p.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False

    suffix = p.suffix.lower()
    try:
        if suffix in (".tif", ".tiff", ".vrt"):
            return _check_raster(p)
        if suffix in (".gpkg", ".shp", ".geojson", ".fgb"):
            return _check_vector(p)
        if suffix == ".csv":
            return _check_csv(p)
        if suffix in (".parquet", ".pq"):
            return _check_parquet(p)
        if suffix == ".feather":
            return _check_feather(p)
        # unknown extension — best-effort: non-empty is good enough
        return True
    except Exception as exc:
        log.debug("is_valid(%s) failed: %s", p, exc)
        return False


def should_skip(*paths: PathLike) -> bool:
    """Return True if every path is valid (caller skips the step).

    When any path is invalid, the *bad* paths are unlinked so the calling
    step can rerun and recreate them cleanly. Valid paths are left alone.
    """
    if not paths:
        return False
    bad: list[Path] = []
    for p in paths:
        if not is_valid(p):
            bad.append(Path(p))
    if not bad:
        return True
    for p in bad:
        if p.is_file():
            try:
                p.unlink()
                log.info("Removed corrupt/empty output: %s", p.name)
            except OSError as exc:
                log.warning("Could not remove %s: %s", p, exc)
    return False


def _check_raster(p: Path) -> bool:
    import rasterio

    with rasterio.open(str(p)) as src:
        if src.width <= 0 or src.height <= 0 or src.count < 1:
            return False
        # touch the first window so a truncated body is detected
        src.read(1, window=((0, min(1, src.height)), (0, min(1, src.width))))
    return True


def _check_vector(p: Path) -> bool:
    try:
        from pyogrio import read_info

        info = read_info(str(p))
        return info.get("features", 0) >= 0 and info.get("layer_name") is not None
    except ImportError:
        import fiona

        with fiona.open(str(p)) as src:
            _ = src.schema
        return True


def _check_csv(p: Path) -> bool:
    # final byte must be a newline — guards against a crash mid-row
    with open(p, "rb") as fh:
        fh.seek(-1, 2)
        return fh.read(1) == b"\n"


def _check_parquet(p: Path) -> bool:
    import pyarrow.parquet as pq

    pq.ParquetFile(str(p)).metadata  # raises on truncation
    return True


def _check_feather(p: Path) -> bool:
    import pyarrow.feather as feather

    feather.read_table(str(p), columns=[]).num_rows  # cheap footer read
    return True
