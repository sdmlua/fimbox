"""
Author: Supath Dhital
Date Updated: June 2026

On-the-fly access to fimbox reference datasets via :mod:`pooch`.

fimbox reads several lookup tables during crosswalk and rating-curve
calibration (IRIS-SWORD slopes, NWM recurrence / high-water flows, USGS rating
curves, optimized Manning's n, channel bathymetry). 

The cache lives under the OS cache dir (e.g. ``~/Library/Caches/fimbox`` on
macOS, ``~/.cache/fimbox`` on Linux); set ``$FIMBOX_DATA_DIR`` to override it.
Each file is verified against a known SHA256 on download.

Usage::

    from fimbox.datasets import fetch_data 

    path = fetch_data("nwm3_high_water_threshold")            # by alias
    path = fetch_data("usgs_rating_curves.parquet")           # by filename
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pooch

__all__ = ["fetch_data", "DATASETS", "REGISTRY", "BASE_URL"]

# Public SDML bucket prefix the files were uploaded to; read anonymously.
BASE_URL = "s3://sdmlab/FIMbox/calibration_data/"

# Registered files mapped to their SHA256. The hash is checked on every
# download, guarding against truncated transfers and silent upstream changes.
REGISTRY = {
    "FIMHF_IRIS_v1.0.csv":
        "sha256:984fa67d3ecd1467053d9d7f61b8c8adc287d1017ffe244f33e9a55e51c2634d",
    "acceptable_sites_for_rating_curves.parquet":
        "sha256:7a64b0b7e159c79f47cd3713d0d13338f5a6145426f0b3adb44dc90e0e306d7a",
    "final_bathymetry_ehydro_ohrfc.gpkg":
        "sha256:d2dede0fdcf53e2effbae20467bb2d07a4fbac9f714e0a49c14488aba8193527",
    "mannings_global_optz.parquet":
        "sha256:3da50f89bcffdf94eb1c791561856ae501ce811959262fc48c717edf7a26c4db",
    "nwm3_17C_recurrence_flows_cfs.parquet":
        "sha256:4a861998522454a6886d401993576bd8e96707f1900c0221b62e5efc6926c0f9",
    "nwm3_high_water_threshold_cms.parquet":
        "sha256:7e7023e968e86d0ba2a94ff76d79c8ebe179d509a32e340727da3d71d431cf6b",
    "usgs_rating_curves.parquet":
        "sha256:5f0a58c0ab04a91d3db2f5db76e9e62546cb9fdca9785afc205910e04436c111",
}

# Stable, readable aliases so callers don't hard-code file names/versions.
DATASETS = {
    "iris_sword_slopes": "FIMHF_IRIS_v1.0.csv",
    "acceptable_gages": "acceptable_sites_for_rating_curves.parquet",
    "bathymetry_ehydro_ohrfc": "final_bathymetry_ehydro_ohrfc.gpkg",
    "mannings_optz": "mannings_global_optz.parquet",
    "nwm3_recurrence_flows": "nwm3_17C_recurrence_flows_cfs.parquet",
    "nwm3_high_water_threshold": "nwm3_high_water_threshold_cms.parquet",
    "usgs_rating_curves": "usgs_rating_curves.parquet",
}

_POOCH = pooch.create(
    path=pooch.os_cache("fimbox"),
    base_url=BASE_URL,
    registry=REGISTRY,
    env="FIMBOX_DATA_DIR",  # user override for the cache location
)


def _s3_anon_downloader(url, output_file, _pooch):
    """pooch downloader that pulls a public S3 object anonymously.

    Mirrors ``--no-sign-request``: no credentials are used, so it works for any
    caller regardless of whether AWS is configured. ``output_file`` is a temp
    path (str) that pooch moves into place once the hash checks out.
    """
    import s3fs  # lazy: heavy optional dependency

    fs = s3fs.S3FileSystem(anon=True)
    remote = url[len("s3://") :]  # "s3://sdmlab/FIMbox/x" -> "sdmlab/FIMbox/x"
    if hasattr(output_file, "write"):
        with fs.open(remote, "rb") as src:
            shutil.copyfileobj(src, output_file)
    else:
        fs.get_file(remote, str(output_file))


def fetch_data(name: str) -> Path:
    """Return a local path to reference dataset ``name``, downloading if needed.

    ``name`` may be a friendly alias from :data:`DATASETS`
    (e.g. ``"nwm3_high_water_threshold"``) or a registered file name
    (e.g. ``"nwm3_high_water_threshold_cms.parquet"``). The file is fetched from
    ``s3://sdmlab/FIMbox/`` anonymously on first use and cached under the OS
    cache dir (override with ``$FIMBOX_DATA_DIR``); subsequent calls reuse the
    cache. Raises :class:`KeyError` for an unknown dataset.
    """
    fname = DATASETS.get(name, name)
    if fname not in REGISTRY:
        raise KeyError(
            f"unknown dataset {name!r}; expected an alias {sorted(DATASETS)} "
            f"or a registered filename {sorted(REGISTRY)}"
        )
    return Path(_POOCH.fetch(fname, downloader=_s3_anon_downloader))
