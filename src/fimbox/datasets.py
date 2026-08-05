"""
Author: Supath Dhital
Date Updated: August 2026

On-demand access to the fimbox reference datasets held in S3.

Nothing ships inside the package: every lookup table is fetched on first use,
verified against a known SHA256, and cached. Set ``$FIMBOX_DATA_DIR`` to choose
where, otherwise the OS cache dir is used.

One flat set of national tables, one file each. Nothing here raises: an unknown
key, a network failure, or a bucket miss returns None so the caller can skip that
step and carry on.

Usage::

    from fimbox import datasets

    datasets.fetch("bankfull_flows")                     #cached local path
    datasets.resolve("channel_roughness", my_own_table)  #caller's file wins
    datasets.prefetch()                                  #warm the cache
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterable, Optional, Union

import pooch

from ._data_manifest import DATASETS, REGISTRY

__all__ = [
    "fetch",
    "fetch_data",
    "resolve",
    "prefetch",
    "available",
    "cache_dir",
    "clear_cache",
    "BASE_URL",
    "DATASETS",
]

PathLike = Union[str, Path]

log = logging.getLogger(__name__)

# Public SDML bucket prefix; read anonymously. The release segment is pinned so a
# later release never changes what an installed fimbox resolves.
BASE_URL = "s3://sdmlab/FIMbox/calibration_data/v1releaseAug2026/"

_POOCH = pooch.create(
    path=pooch.os_cache("fimbox"),
    base_url=BASE_URL,
    registry={path: f"sha256:{digest}" for path, digest in REGISTRY.items()},
    env="FIMBOX_DATA_DIR",  # user override for the cache location
)


def cache_dir() -> Path:
    """Directory the fetched datasets are cached in."""
    return Path(_POOCH.abspath)


def available() -> list[str]:
    """Dataset keys that can be fetched."""
    return sorted(DATASETS)


def _s3_anon_downloader(url, output_file, _pooch):
    # Anonymous read, so it works whether or not AWS is configured. output_file is
    # a temp path pooch moves into place once the hash checks out.
    import s3fs  # lazy: heavy optional dependency

    fs = s3fs.S3FileSystem(anon=True)
    remote = url[len("s3://") :]  # "s3://sdmlab/FIMbox/x" -> "sdmlab/FIMbox/x"
    if hasattr(output_file, "write"):
        with fs.open(remote, "rb") as src:
            shutil.copyfileobj(src, output_file)
    else:
        fs.get_file(remote, str(output_file))


def fetch(key: str) -> Optional[Path]:
    """Local path to dataset ``key``, downloaded on first use.

    Returns None rather than raising for an unknown key or a failed download —
    the caller's job is to skip that step, not to sink the run."""
    path = f"national/{_LEGACY_ALIASES.get(key, key)}.parquet"
    if path not in REGISTRY:
        log.warning(f"datasets: unknown dataset {key!r}; known: {available()}")
        return None
    try:
        return Path(_POOCH.fetch(path, downloader=_s3_anon_downloader))
    except Exception as exc:  # network, credentials, hash mismatch
        log.warning(f"datasets: could not fetch {key!r}: {exc}")
        return None


def resolve(key: str, override: Optional[PathLike] = None) -> Optional[Path]:
    """Caller's file if given, else the published dataset — the seam every
    calibration step reads its inputs through."""
    if override is not None:
        p = Path(override)
        if p.exists():
            return p
        log.warning(f"datasets: {key}: no such file {p} — falling back to published")
    return fetch(key)


def prefetch() -> dict[str, Optional[Path]]:
    """Warm the cache so a later run needs no network."""
    return {k: fetch(k) for k in DATASETS}


def clear_cache(keys: Optional[Iterable[str]] = None) -> None:
    """Delete cached downloads — everything, or just the named datasets."""
    root = cache_dir()
    if keys is None:
        shutil.rmtree(root, ignore_errors=True)
        return
    for key in keys:
        (root / "national" / f"{key}.parquet").unlink(missing_ok=True)


# Pre-rename aliases, so older notebooks keep resolving. New code uses fetch().
_LEGACY_ALIASES = {
    "acceptable_gages": "gage_quality_filter",
    "bathymetry_ehydro_ohrfc": "channel_bathymetry",
    "mannings_optz": "channel_roughness",
    "nwm3_high_water_threshold": "bankfull_flows",
    "nwm3_recurrence_flows": "recurrence_flows",
    "usgs_rating_curves": "gage_rating_curves",
}


def fetch_data(name: str) -> Optional[Path]:
    key = name.split("/")[-1]
    for suffix in (".parquet", ".csv", ".gpkg"):
        key = key[: -len(suffix)] if key.endswith(suffix) else key
    return fetch(key)
