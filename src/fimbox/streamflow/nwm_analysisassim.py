"""
Author: Supath Dhital
Date Created: August 2026

NWM Analysis and Assimilation (AnA) streamflow retrieval.

AnA is the model's gauge-assimilated best estimate of past conditions, indexed
by valid time rather than a forecast cycle — one hourly tm00 channel_rt file
per hour. Given a feature_id list and a date (or date range), downloads the
AOI's reaches into an hourly parquet archive under
``<AOI>/streamflow/nwm_analysisassim/``, then emits FIM-ready discharge CSVs
(``feature_id, discharge_cms``) into ``<AOI>/discharge-inputs/``:

  * a single timestamp           -> one CSV at that hour
  * a date range                 -> one CSV per hour in the range
  * a range + sortby aggregation -> one CSV of max/min/median/mean over range
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from ..logging_utils import log_errors
from . import _common as C

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

_URL_BASE = "https://storage.googleapis.com/national-water-model"
# First AnA cycle published on the NWM bucket; earlier events need NWMRetrospective.
ARCHIVE_START = datetime(2018, 9, 17)


def getNWManalysisassim(
    aoi_dir: PathLike,
    *,
    feature_ids: Optional[list] = None,
    feature_id_csv: Optional[PathLike] = None,
    date: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    sortby: Optional[str] = None,
) -> list[Path]:
    """Retrieve NWM Analysis and Assimilation streamflow into FIM-ready CSVs.

    Feature ids come from ``feature_ids`` (a list), ``feature_id_csv`` (a path),
    or the AOI's ``feature_id.csv`` (default). Date handling:

      * ``date``            -> one CSV at that instant/day
      * ``start`` + ``end`` -> one CSV per hour (continuous)
      * ``start`` + ``end`` + ``sortby`` -> one aggregated CSV (max/min/median/mean)
    """
    with log_errors("Analysis assimilation streamflow"):
        fid_csv = C.resolve_feature_id_csv(
            aoi_dir, feature_id_csv=feature_id_csv, feature_ids=feature_ids
        )
        ana = NWMAnalysisAssim(aoi_dir, fid_csv)
        if date:
            return [ana.at(date)]
        if start and end:
            return ana.to_fim_inputs(start, end, sortby=sortby)
        raise ValueError("Provide date=, or start= and end=.")


class NWMAnalysisAssim:
    """Fetch and slice NWM Analysis and Assimilation streamflow for an AOI's feature_ids."""

    def __init__(self, aoi_dir: PathLike, feature_id_csv: PathLike):
        self.aoi_dir = Path(aoi_dir)
        self.feature_id_csv = Path(feature_id_csv)
        self.archive_dir = C.streamflow_dir(aoi_dir, "nwm_analysisassim")
        C.attach_log(aoi_dir)

    # downloading
    def fetch(self, start_date: str, end_date: str) -> Path:
        """Download hourly AnA (tm00) for [start_date, end_date] (idempotent).

        One parquet per requested window, named by its day span. Skips the
        download when a parquet already exists (or an existing wider window
        already covers the request), and returns the parquet path.
        """
        start = self._parse(start_date)
        end = self._parse(end_date, end_of_day=True)
        if end < start:
            raise ValueError("end_date must be on or after start_date.")
        if start < ARCHIVE_START:
            log.warning(
                "AnA starts %s -- clamping start (use NWMRetrospective for earlier events).",
                ARCHIVE_START.strftime("%Y-%m-%d"),
            )
            start = ARCHIVE_START

        target = self.archive_dir / self._canonical_name(start, end)
        covering = self._covering_file(start, end)
        if target.exists() or covering is not None:
            existing = target if target.exists() else covering
            log.info("SKIP (exists): %s", existing.name)
            return existing

        requests = C.require("requests")
        nc = C.require("netCDF4")
        feature_ids = set(C.load_feature_ids(self.feature_id_csv))

        netcdf_stage = self.archive_dir / "netCDF"
        hourly_dir = self.archive_dir / "hourly"
        netcdf_stage.mkdir(parents=True, exist_ok=True)
        hourly_dir.mkdir(parents=True, exist_ok=True)

        hours = self._hourly_range(start, end)
        log.info("AnA: %d reaches, %d hourly timestep(s)", len(feature_ids), len(hours))
        for i, valid_time in enumerate(hours, 1):
            hourly_path = hourly_dir / f"{valid_time:%Y%m%d_%H}.parquet"
            if not hourly_path.exists():
                df = self._fetch_hour(requests, nc, valid_time, feature_ids, netcdf_stage)
                if df is not None and not df.empty:
                    df.to_parquet(hourly_path, index=False)
            if i % 24 == 0 or i == len(hours):
                log.info("  %d/%d done (%s UTC)", i, len(hours), valid_time)
        self._rmtree(netcdf_stage)

        hourly_files = sorted(hourly_dir.glob("*.parquet"))
        if not hourly_files:
            self._rmtree(hourly_dir)
            log.warning("No AnA discharge retrieved for the requested period.")
            return target
        combined = pd.concat([pd.read_parquet(f) for f in hourly_files], ignore_index=True)
        combined[["feature_id", "value_time", "discharge"]].to_parquet(target, index=False)
        self._rmtree(hourly_dir)
        log.info("AnA parquet --> %s", target.name)
        return target

    @staticmethod
    def _fetch_hour(requests, nc, valid_time, feature_ids, stage_dir: Path) -> Optional[pd.DataFrame]:
        """Download the tm00 file valid at ``valid_time`` and filter to feature_ids."""
        fname = f"nwm.t{valid_time.hour:02d}z.analysis_assim.channel_rt.tm00.conus.nc"
        url = f"{_URL_BASE}/nwm.{valid_time:%Y%m%d}/analysis_assim/{fname}"
        path = stage_dir / fname

        try:
            r = requests.get(url)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            path.write_bytes(r.content)
        except requests.exceptions.RequestException as exc:
            log.debug("download failed %s: %s", fname, exc)
            return None

        try:
            ds = nc.Dataset(str(path), "r")
            df = pd.DataFrame(
                {
                    "feature_id": ds.variables["feature_id"][:],
                    "discharge": ds.variables["streamflow"][:],
                }
            )
            ds.close()
        except Exception as exc:
            log.debug("unreadable netCDF %s: %s", fname, exc)
            return None
        finally:
            path.unlink(missing_ok=True)

        df = df[df["feature_id"].isin(feature_ids)].copy()
        df["value_time"] = valid_time
        return df

    # slicing into FIM-ready CSVs
    def to_fim_inputs(
        self,
        start_date: str,
        end_date: str,
        *,
        sortby: Optional[str] = None,
    ) -> list[Path]:
        """Fetch the range, then write FIM-ready CSVs into discharge-inputs/.

        sortby in {"maximum","minimum","median","mean"} collapses the whole
        range to a single aggregated CSV. Otherwise one CSV is written per
        hourly timestamp in the range.
        """
        parquet = self.fetch(start_date, end_date)
        if not parquet.exists():
            return []
        df = self._read(parquet, self._parse(start_date), self._parse(end_date, end_of_day=True))
        out_dir = C.discharge_inputs_dir(self.aoi_dir)
        written: list[Path] = []

        if sortby:
            agg = self._aggregate(df, sortby)
            name = f"AnA_{C.stamp(start_date, False)}_{C.stamp(end_date, False)}_{sortby}.csv"
            written.append(C.write_fim_ready(agg, out_dir / name))
            return written

        for ts, group in df.groupby("value_time"):
            per = group[["feature_id", "discharge"]]
            written.append(C.write_fim_ready(per, out_dir / f"AnA_{C.stamp(ts)}.csv"))
        log.info("Wrote %d hourly FIM-ready CSVs --> %s", len(written), out_dir)
        return written

    def at(self, when: str) -> Path:
        """FIM-ready CSV for a single instant or day.

        A ``YYYY-MM-DD`` day is averaged to one value per reach; a
        ``YYYY-MM-DD HH:MM:SS`` instant takes that exact hour. The containing
        day is fetched so the instant is covered.
        """
        kind = C.parse_date_kind(when)
        if kind == "invalid":
            raise ValueError(f"Unparseable date: {when!r}")
        t = pd.to_datetime(when)
        day = t.strftime("%Y-%m-%d")

        parquet = self.fetch(day, day)
        if not parquet.exists():
            raise FileNotFoundError(f"No AnA discharge retrieved for {when}.")
        df = self._read(parquet, self._parse(day), self._parse(day, end_of_day=True))

        if kind == "date":
            sel = df[df["value_time"].dt.date == t.date()]
            per = sel.groupby("feature_id")["discharge"].mean().reset_index()
            name = f"AnA_{C.stamp(t, False)}.csv"
        else:
            sel = df[df["value_time"] == t.replace(minute=0, second=0, microsecond=0)]
            per = sel[["feature_id", "discharge"]]
            name = f"AnA_{C.stamp(t)}.csv"

        return C.write_fim_ready(per, C.discharge_inputs_dir(self.aoi_dir) / name)

    # internals
    @staticmethod
    def _parse(value: str, end_of_day: bool = False) -> datetime:
        """Parse a date/datetime string to the hour; a plain day snaps to
        00 UTC, or 23 UTC when ``end_of_day`` is set."""
        t = pd.to_datetime(value).to_pydatetime()
        if end_of_day and C.parse_date_kind(value) == "date":
            t = t.replace(hour=23)
        return t.replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def _hourly_range(start: datetime, end: datetime) -> list[datetime]:
        hours, cur = [], start
        while cur <= end:
            hours.append(cur)
            cur += timedelta(hours=1)
        return hours

    @staticmethod
    def _canonical_name(start: datetime, end: datetime) -> str:
        """Archive filename for a request: ``YYYYMMDD.parquet`` when the start
        and end days match, else ``YYYYMMDD_YYYYMMDD.parquet``."""
        s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        return f"{s}.parquet" if s == e else f"{s}_{e}.parquet"

    @staticmethod
    def _file_span(parquet: Path):
        """Day span a parquet filename encodes: (start_day, end_day) as
        ``YYYYMMDD`` strings. Single-day names repeat the day."""
        stem = parquet.stem
        if "_" in stem:
            a, b = stem.split("_", 1)
            return a, b
        return stem, stem

    def _covering_file(self, start: datetime, end: datetime) -> Optional[Path]:
        """An already-downloaded parquet whose day span fully contains the
        request (so a wider prior download is reused instead of re-fetching)."""
        s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        for parquet in sorted(self.archive_dir.glob("*.parquet")):
            fs, fe = self._file_span(parquet)
            if fs <= s and fe >= e:
                return parquet
        return None

    @staticmethod
    def _read(parquet: Path, start: datetime, end: datetime) -> pd.DataFrame:
        """Read the archive parquet and clip to the exact [start, end] window."""
        df = pd.read_parquet(parquet)
        df["value_time"] = pd.to_datetime(df["value_time"])
        df = df[(df["value_time"] >= start) & (df["value_time"] <= end)]
        return df.drop_duplicates(subset=["value_time", "feature_id"]).reset_index(drop=True)

    @staticmethod
    def _aggregate(df: pd.DataFrame, sortby: str) -> pd.DataFrame:
        funcs = {"maximum": "max", "minimum": "min", "median": "median", "mean": "mean"}
        if sortby not in funcs:
            raise ValueError(f"sortby must be one of {list(funcs)}, got {sortby!r}")
        return df.groupby("feature_id")["discharge"].agg(funcs[sortby]).reset_index()

    # robust rmtree (clears read-only bits, retries briefly)
    @staticmethod
    def _rmtree(path: Path, retries: int = 3, delay: float = 0.2) -> None:
        def _onerror(func, p, _exc):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
            except Exception:
                pass

        for i in range(retries):
            try:
                if path.exists():
                    shutil.rmtree(path, onerror=_onerror)
                return
            except Exception:
                if i == retries - 1:
                    raise
                time.sleep(delay)
