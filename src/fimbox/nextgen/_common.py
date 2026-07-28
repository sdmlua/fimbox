"""
Shared helpers for the ``fimbox.nextgen`` subpackage: constants for the public
CIROH community NextGen DataStream bucket, an anonymous s3fs handle, AOI-layout
resolution, and the cached per-VPU bounding-box index used to resolve an area
of interest to its hydrofabric VPU(s) without opening every GeoPackage.

The bucket is read anonymously (``--no-sign-request`` equivalent) so it works
for any caller regardless of whether AWS credentials are configured.
"""

from __future__ import annotations

import importlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional, Union

from ..logging_utils import aoi_root, attach_case_log

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

BUCKET = "ciroh-community-ngen-datastream"
S3_BUCKET_URL = f"https://{BUCKET}.s3.amazonaws.com"

HF_VERSION = "v2.2"
HF_GEOPACKAGES_PREFIX = f"{BUCKET}/resources/{HF_VERSION}_hydrofabric/geopackages"

# outputs/<model>/<HF_VERSION>_hydrofabric/ngen.<YYYYMMDD>/<forecast>/<cycle>/VPU_<id>/
OUTPUTS_PREFIX = f"{BUCKET}/outputs"

HF_EPSG = 5070  # CONUS Albers — all spatial ops happen in this CRS
DISCHARGE_COL = "discharge_cms"  # column name the FIM Inundator expects

HYDROFABRIC_DIR = "hydrofabric"
DISCHARGE_INPUTS_DIR = "discharge-inputs"


def require(module: str):
    """Import an optional dependency, raising a clear install hint if missing."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - trivial
        raise ImportError(
            f"'{module}' is required for fimbox.nextgen. "
            f"Install it with: pip install {module}"
        ) from exc


@lru_cache(maxsize=1)
def s3() -> "object":
    """Anonymous s3fs filesystem for the public bucket (cached)."""
    s3fs = require("s3fs")
    return s3fs.S3FileSystem(anon=True)


def resolve_aoi(aoi_dir: PathLike) -> Path:
    """Return the AOI root for any directory the caller passes (the root itself
    or its ``watershed-data`` subfolder)."""
    return aoi_root(Path(aoi_dir))


def hydrofabric_dir(aoi_dir: PathLike) -> Path:
    """``<AOI>/hydrofabric`` — subset catchments / flowpaths / crosswalk land
    here. Created on demand."""
    d = resolve_aoi(aoi_dir) / HYDROFABRIC_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def discharge_inputs_dir(aoi_dir: PathLike) -> Path:
    """``<AOI>/discharge-inputs`` — the FIM-ready discharge CSVs the generator
    iterates (shared with the streamflow subpackage)."""
    d = resolve_aoi(aoi_dir) / DISCHARGE_INPUTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def attach_log(aoi_dir: PathLike) -> None:
    """Route nextgen log records into the AOI's combined processing.log."""
    attach_case_log(aoi_dir)


_DATA_DIR = Path(__file__).parent / "data"
_VPU_INDEX_PATH = _DATA_DIR / "vpu_bbox.json"


@lru_cache(maxsize=1)
def vpu_bbox_index() -> dict[str, list[float]]:
    """Load the cached ``{VPU_id: [minx, miny, maxx, maxy]}`` index (EPSG:5070).

    Shipped as package data so AOI->VPU resolution needs no network round-trip.
    """
    payload = json.loads(_VPU_INDEX_PATH.read_text())
    return {k: list(map(float, v)) for k, v in payload["bboxes"].items()}


def vpu_gpkg_uri(vpu: str) -> str:
    """``/vsis3`` URI for a VPU's NextGen GeoPackage (read anonymously)."""
    return f"/vsis3/{HF_GEOPACKAGES_PREFIX}/{vpu}/nextgen_{vpu}.gpkg"


def feature_id_from_wb(wb_id: str) -> Optional[int]:
    """``"wb-2855078"`` -> ``2855078``. Returns None for non-``wb`` ids."""
    if not isinstance(wb_id, str) or "-" not in wb_id:
        return None
    prefix, _, num = wb_id.partition("-")
    if prefix != "wb" or not num.isdigit():
        return None
    return int(num)
