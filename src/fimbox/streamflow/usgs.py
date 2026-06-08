"""
Author: Supath Dhital
Date Created: June 2026

USGS gage streamflow retrieval via teehr, archived under
``<AOI>/streamflow/usgs/``. Used for observation-vs-NWM comparison and
statistics — not a FIM forecast source.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence, Union

import pandas as pd

from . import _common as C
from ..logging_utils import WATERSHED_DIR_NAME

log = logging.getLogger(__name__)

PathLike = Union[str, Path]
_LOC_PREFIX = "usgs-"


def get_usgs_fid_pairs(aoi_dir: PathLike) -> pd.DataFrame:
    """Return the USGS-gage <-> NWM feature_id pairs for an AOI.

    Reads the ``usgs_subset_gages.gpkg`` produced by the branch-processing gage
    crosswalk (in the AOI root or its watershed-data folder) and returns a
    DataFrame of ``location_id, feature_id`` — i.e. which USGS gage falls on
    which reach. Raises if the gage file was never produced.
    """
    root = C.resolve_aoi(aoi_dir)
    candidates = [
        root / WATERSHED_DIR_NAME / "usgs_subset_gages.gpkg",
        root / "usgs_subset_gages.gpkg",
    ]
    gpkg = next((p for p in candidates if p.exists()), None)
    if gpkg is None:
        raise FileNotFoundError(
            "usgs_subset_gages.gpkg not found — run the branch-processing gage "
            f"crosswalk first (looked in {[str(c) for c in candidates]})."
        )
    import geopandas as gpd

    gdf = gpd.read_file(gpkg)
    cols = [c for c in ("location_id", "feature_id") if c in gdf.columns]
    pairs = (
        pd.DataFrame(gdf.drop(columns="geometry"))[cols]
        .dropna(subset=cols)
        .drop_duplicates()
        .reset_index(drop=True)
    )
    if "feature_id" in pairs.columns:
        pairs["feature_id"] = pairs["feature_id"].astype("int64")
    log.info("USGS<->feature_id pairs for AOI %s: %d", root.name, len(pairs))
    return pairs


class USGSData:
    """Fetch and read USGS gage streamflow for an AOI."""

    def __init__(self, aoi_dir: PathLike):
        self.aoi_dir = Path(aoi_dir)
        self.archive_dir = C.streamflow_dir(aoi_dir, "usgs")
        C.attach_log(aoi_dir)

    def fetch(self, sites: Sequence[str], start_date: str, end_date: str) -> Path:
        """Download USGS streamflow for ``sites`` over [start_date, end_date]
        (``YYYY-MM-DD``) into a parquet archive. Returns the parquet path."""
        parquet = self.archive_dir / f"{start_date}_{end_date}.parquet"
        if parquet.exists():
            log.info("SKIP (exists): %s", parquet.name)
            return parquet
        usgs = C.require("teehr.fetching.usgs.usgs")
        log.info("USGS: %d sites, %s -> %s", len(sites), start_date, end_date)
        usgs.usgs_to_parquet(
            start_date=start_date,
            end_date=end_date,
            sites=list(sites),
            output_parquet_dir=self.archive_dir,
            overwrite_output=True,
        )
        return parquet

    def series(
        self, site: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """Return a ``Date, Discharge`` time series for one site, or None when
        the parquet/site is absent."""
        parquet = self.archive_dir / f"{start_date}_{end_date}.parquet"
        if not parquet.exists():
            return None
        df = pd.read_parquet(parquet)
        rows = df[df["location_id"] == f"{_LOC_PREFIX}{site}"]
        if rows.empty:
            return None
        return rows[["value_time", "value"]].rename(
            columns={"value_time": "Date", "value": "Discharge"}
        )
