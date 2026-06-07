"""
Author: Supath Dhital
Date Created: June 2026

GEOGLOWS v2 retrospective streamflow retrieval from the public S3 zarr store.

Maps the AOI's reaches to GEOGLOWS river ids (LINKNO) via a hydrotable, slices
the requested window, archives the full series under ``<AOI>/streamflow/geoglows/``,
and writes a FIM-ready CSV (``feature_id, discharge_cms``) for the event time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from . import _common as C

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

_BUCKET = "s3://geoglows-v2-retrospective/retrospective.zarr"
_REGION = "us-west-2"


class GeoglowsData:
    """Fetch GEOGLOWS retrospective streamflow for an AOI's reaches."""

    def __init__(self, aoi_dir: PathLike, hydrotable_csv: PathLike):
        # hydrotable must map LINKNO (GEOGLOWS river id) -> feature_id.
        self.aoi_dir = Path(aoi_dir)
        self.hydrotable_csv = Path(hydrotable_csv)
        self.archive_dir = C.streamflow_dir(aoi_dir, "geoglows")

    def _open_store(self):
        s3fs = C.require("s3fs")
        xarray = C.require("xarray")
        s3 = s3fs.S3FileSystem(anon=True, client_kwargs={"region_name": _REGION})
        store = s3fs.S3Map(root=_BUCKET, s3=s3, check=False)
        return xarray.open_zarr(store)

    def fetch(
        self,
        event_time: str,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> tuple[Path, Path]:
        """Slice the [start_date, end_date] window (default ±1 day around
        event_time, capped at now), archive the full series, and write a
        FIM-ready CSV at the event instant. Returns (series_csv, fim_csv)."""
        t = pd.to_datetime(event_time)
        if start_date is None or end_date is None:
            start = t - timedelta(days=1)
            end = min(t + timedelta(days=1), datetime.utcnow())
        else:
            start, end = pd.to_datetime(start_date), pd.to_datetime(end_date)

        hydro = pd.read_csv(self.hydrotable_csv)
        link_to_fid = hydro.set_index("LINKNO")["feature_id"].to_dict()
        riv_ids = hydro["LINKNO"].tolist()

        ds = self._open_store()
        frame = ds["Qout"].sel(rivid=riv_ids).to_dataframe().reset_index()
        frame["time"] = pd.to_datetime(frame["time"])
        frame = frame[(frame["time"] >= start) & (frame["time"] <= end)].copy()
        frame["feature_id"] = frame["rivid"].map(link_to_fid).astype("int64")

        series = frame[["feature_id", "Qout", "time"]].rename(
            columns={"Qout": "discharge"}
        )
        series_csv = (
            self.archive_dir
            / f"{C.stamp(start, False)}_{C.stamp(end, False)}_streamflow.csv"
        )
        series.to_csv(series_csv, index=False)
        log.info("GEOGLOWS series --> %s", series_csv.name)

        at_event = series[series["time"] == t][["feature_id", "discharge"]]
        fim_csv = C.discharge_inputs_dir(self.aoi_dir) / f"GEOGLOWS_{C.stamp(t)}.csv"
        C.write_fim_ready(at_event, fim_csv)
        return series_csv, fim_csv
