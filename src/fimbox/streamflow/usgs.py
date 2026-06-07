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

log = logging.getLogger(__name__)

PathLike = Union[str, Path]
_LOC_PREFIX = "usgs-"


class USGSData:
    """Fetch and read USGS gage streamflow for an AOI."""

    def __init__(self, aoi_dir: PathLike):
        self.aoi_dir = Path(aoi_dir)
        self.archive_dir = C.streamflow_dir(aoi_dir, "usgs")

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
