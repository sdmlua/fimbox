"""
Author: Supath Dhital
Date Created: June 2026

Shared helpers for the streamflow subpackage: AOI-layout resolution, feature_id
loading, date parsing, FIM-ready CSV writing, and lazy-import guards for the
heavy optional dependencies (teehr / s3fs / xarray / netCDF4 / matplotlib).
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from ..logging_utils import aoi_root

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

# Subfolders under the AOI root.
STREAMFLOW_DIR = "streamflow"
DISCHARGE_INPUTS_DIR = "discharge-inputs"
WATERSHED_DIR = "watershed-data"
PLOTS_DIR = "plots"

# Column name the FIM Inundator expects for the forecast value.
DISCHARGE_COL = "discharge_cms"


def require(module: str):
    """Import a streamflow dependency, raising a clear install hint if missing."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(
            f"'{module}' is required for this streamflow feature. "
            f"Install it with: pip install {module}"
        ) from exc


def resolve_aoi(aoi_dir: PathLike) -> Path:
    """Return the AOI root for any directory the caller passes (the root itself
    or its watershed-data subfolder)."""
    return aoi_root(Path(aoi_dir))


def streamflow_dir(aoi_dir: PathLike, source: Optional[str] = None) -> Path:
    """``<AOI>/streamflow[/<source>]`` — created on demand. This is the archive
    for raw downloads and full time series."""
    d = resolve_aoi(aoi_dir) / STREAMFLOW_DIR
    if source:
        d = d / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def discharge_inputs_dir(aoi_dir: PathLike) -> Path:
    """``<AOI>/discharge-inputs`` — the FIM-ready forecast CSVs the generator
    iterates."""
    d = resolve_aoi(aoi_dir) / DISCHARGE_INPUTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def plots_dir(aoi_dir: PathLike) -> Path:
    """``<AOI>/watershed-data/plots`` — all 500-DPI figures land here, kept
    inside the watershed-data folder alongside the processing artifacts."""
    d = resolve_aoi(aoi_dir) / WATERSHED_DIR / PLOTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_feature_ids(feature_id_csv: PathLike) -> list[int]:
    """Read the ``feature_id`` column from a feature_id.csv into a list of ints."""
    df = pd.read_csv(feature_id_csv)
    if "feature_id" not in df.columns:
        raise ValueError(f"{feature_id_csv} has no 'feature_id' column")
    return df["feature_id"].dropna().astype("int64").tolist()


def parse_date_kind(value: str) -> str:
    """Classify a date string as 'date' (YYYY-MM-DD), 'datetime'
    (YYYY-MM-DD HH:MM:SS), or 'invalid'."""
    if isinstance(value, (pd.Timestamp, datetime)):
        return "datetime"
    for fmt, kind in (("%Y-%m-%d", "date"), ("%Y-%m-%d %H:%M:%S", "datetime")):
        try:
            datetime.strptime(str(value), fmt)
            return kind
        except ValueError:
            continue
    return "invalid"


def write_fim_ready(
    discharge_by_fid: pd.DataFrame, out_csv: Path
) -> Path:
    """Write a FIM-ready CSV (``feature_id, discharge_cms``) the Inundator can
    read directly. ``discharge_by_fid`` must hold 'feature_id' and a discharge
    column named either 'discharge' or 'discharge_cms'."""
    df = discharge_by_fid.copy()
    src_col = "discharge_cms" if "discharge_cms" in df.columns else "discharge"
    out = df[["feature_id", src_col]].rename(columns={src_col: DISCHARGE_COL})
    out["feature_id"] = out["feature_id"].astype("int64")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_csv, index=False)
    log.info("FIM-ready discharge (%d reaches) --> %s", len(out), out_csv.name)
    return out_csv


def stamp(ts: Union[str, pd.Timestamp], with_hour: bool = True) -> str:
    """Filesystem-safe timestamp token, e.g. ``20240115T1200`` or ``20240115``."""
    t = pd.to_datetime(ts)
    return t.strftime("%Y%m%dT%H%M") if with_hour else t.strftime("%Y%m%d")
