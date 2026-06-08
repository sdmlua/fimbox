"""
Author: Supath Dhital
Date Created: June 2026

NWM retrospective streamflow retrieval.

Given a feature_id list and a date (or date range), fetch NWM v3.0
retrospective hourly streamflow via teehr into a parquet archive under
``<AOI>/streamflow/nwm30_retrospective/``, then emit FIM-ready discharge CSVs
(``feature_id, discharge_cms``) into ``<AOI>/discharge-inputs/``:

  * a single timestamp           -> one CSV at that hour
  * a date range                 -> one CSV per hour in the range
  * a range + sortby aggregation -> one CSV of max/min/mean over the range
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence, Union

import pandas as pd

from . import _common as C

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

NWM_VERSION = "nwm30"
_LOC_PREFIX = f"{NWM_VERSION}-"


def getNWMretrospective(
    aoi_dir: PathLike,
    *,
    feature_ids: Optional[list] = None,
    feature_id_csv: Optional[PathLike] = None,
    date: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    sortby: Optional[str] = None,
) -> list[Path]:
    """Retrieve NWM retrospective streamflow into FIM-ready CSVs.

    Feature ids come from ``feature_ids`` (a list), ``feature_id_csv`` (a path),
    or the AOI's ``feature_id.csv`` (default). Date handling:

      * ``date``            -> one CSV at that instant/day
      * ``start`` + ``end`` -> one CSV per hour (continuous)
      * ``start`` + ``end`` + ``sortby`` -> one aggregated CSV (max/min/mean)
    """
    fid_csv = C.resolve_feature_id_csv(
        aoi_dir, feature_id_csv=feature_id_csv, feature_ids=feature_ids
    )
    retro = NWMRetrospective(aoi_dir, fid_csv)
    if date:
        return [retro.at(date)]
    if start and end:
        return retro.to_fim_inputs(start, end, sortby=sortby)
    raise ValueError("Provide date=, or start= and end=.")


class NWMRetrospective:
    """Fetch and slice NWM retrospective streamflow for an AOI's feature_ids."""

    def __init__(
        self,
        aoi_dir: PathLike,
        feature_id_csv: PathLike,
        *,
        nwm_version: str = NWM_VERSION,
        variable_name: str = "streamflow",
    ):
        self.aoi_dir = Path(aoi_dir)
        self.feature_id_csv = Path(feature_id_csv)
        self.nwm_version = nwm_version
        self.variable_name = variable_name
        self.archive_dir = C.streamflow_dir(aoi_dir, f"{nwm_version}_retrospective")
        C.attach_log(aoi_dir)

    # downloading
    def fetch(self, start_date: str, end_date: str) -> Path:
        """Download NWM retrospective for [start_date, end_date] (idempotent).

        teehr names its output by the request's day span: ``YYYYMMDD.parquet``
        when start and end fall on the same UTC day, else
        ``YYYYMMDD_YYYYMMDD.parquet``. We compute that exact name, skip the
        download when it already exists (or when an existing wider window
        already covers the request), and return the parquet path.
        """
        target = self.archive_dir / self._canonical_name(start_date, end_date)
        if target.exists() or self._covering_file(start_date, end_date) is not None:
            existing = target if target.exists() else self._covering_file(
                start_date, end_date
            )
            log.info("SKIP (exists): %s", existing.name)
            return existing

        nwm_retro = C.require("teehr.fetching.nwm.retrospective_points")
        location_ids = C.load_feature_ids(self.feature_id_csv)
        log.info(
            "NWM retrospective: %d reaches, %s -> %s",
            len(location_ids),
            start_date,
            end_date,
        )
        nwm_retro.nwm_retro_to_parquet(
            nwm_version=self.nwm_version,
            variable_name=self.variable_name,
            start_date=start_date,
            end_date=end_date,
            location_ids=location_ids,
            output_parquet_dir=self.archive_dir,
        )
        log.info(
            "NWM retrospective parquet --> %s",
            target.name if target.exists() else "(no data in window)",
        )
        return target

    # slicing into FIM-ready CSVs
    def to_fim_inputs(
        self,
        start_date: str,
        end_date: str,
        *,
        sortby: Optional[str] = None,
    ) -> list[Path]:
        """Fetch the range, then write FIM-ready CSVs into discharge-inputs/.

        sortby in {"maximum","minimum","mean"} collapses the whole range to a
        single aggregated CSV. Otherwise one CSV is written per hourly
        timestamp in the range.
        """
        self.fetch(start_date, end_date)
        df = self._read_range(start_date, end_date)
        out_dir = C.discharge_inputs_dir(self.aoi_dir)
        written: list[Path] = []

        if sortby:
            agg = self._aggregate(df, sortby)
            name = f"NWM_{C.stamp(start_date, False)}_{C.stamp(end_date, False)}_{sortby}.csv"
            written.append(C.write_fim_ready(agg, out_dir / name))
            return written

        # One CSV per hour in the range.
        for ts, group in df.groupby("value_time"):
            per = group.rename(columns={"value": "discharge"})[
                ["feature_id", "discharge"]
            ]
            written.append(
                C.write_fim_ready(per, out_dir / f"NWM_{C.stamp(ts)}.csv")
            )
        log.info("Wrote %d hourly FIM-ready CSVs --> %s", len(written), out_dir)
        return written

    def at(self, when: str) -> Path:
        """FIM-ready CSV for a single instant or day.

        A ``YYYY-MM-DD`` day is averaged to one value per reach; a
        ``YYYY-MM-DD HH:MM:SS`` instant takes that exact hour. A ±1 window is
        fetched so the instant is covered.
        """
        kind = C.parse_date_kind(when)
        if kind == "invalid":
            raise ValueError(f"Unparseable date: {when!r}")
        t = pd.to_datetime(when)

        if kind == "date":
            lag = (t - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            lead = (t + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            lag = (t - pd.Timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            lead = (t + pd.Timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        self.fetch(lag, lead)
        df = self._read_range(lag, lead)

        if kind == "date":
            sel = df[df["value_time"].dt.date == t.date()]
            per = (
                sel.groupby("feature_id")["value"].mean().reset_index()
            ).rename(columns={"value": "discharge"})
            name = f"NWM_{C.stamp(t, False)}.csv"
        else:
            sel = df[df["value_time"] == t]
            per = sel.rename(columns={"value": "discharge"})[
                ["feature_id", "discharge"]
            ]
            name = f"NWM_{C.stamp(t)}.csv"

        return C.write_fim_ready(per, C.discharge_inputs_dir(self.aoi_dir) / name)

    # selecting from already-downloaded data (no re-download)
    def select_from_archive(
        self,
        *,
        date: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        sortby: Optional[str] = None,
    ) -> list[Path]:
        """Slice FIM-ready CSVs out of the EXISTING parquet archive for a
        narrower date/range, without downloading again.

        Useful when a wide window was already fetched and the user later picks
        a specific instant or sub-range to generate FIM from. Raises if no
        archived parquet covers the request.
        """
        archive = self._load_archive()
        if archive.empty:
            raise FileNotFoundError(
                f"No archived NWM parquet under {self.archive_dir} — fetch first."
            )
        lo, hi = archive["value_time"].min(), archive["value_time"].max()
        out_dir = C.discharge_inputs_dir(self.aoi_dir)
        written: list[Path] = []

        if date:
            t = pd.to_datetime(date)
            kind = C.parse_date_kind(date)
            if kind == "date":
                sel = archive[archive["value_time"].dt.date == t.date()]
                self._require_coverage(sel, date, lo, hi)
                per = sel.groupby("feature_id")["value"].mean().reset_index()
                per = per.rename(columns={"value": "discharge"})
                written.append(C.write_fim_ready(per, out_dir / f"NWM_{C.stamp(t, False)}.csv"))
            else:
                sel = archive[archive["value_time"] == t]
                self._require_coverage(sel, date, lo, hi)
                per = sel.rename(columns={"value": "discharge"})[["feature_id", "discharge"]]
                written.append(C.write_fim_ready(per, out_dir / f"NWM_{C.stamp(t)}.csv"))
            return written

        if start and end:
            s, e = pd.to_datetime(start), pd.to_datetime(end)
            sel = archive[(archive["value_time"] >= s) & (archive["value_time"] <= e)]
            self._require_coverage(sel, f"{start}..{end}", lo, hi)
            if sortby:
                agg = self._aggregate(sel, sortby)
                name = f"NWM_{C.stamp(s, False)}_{C.stamp(e, False)}_{sortby}.csv"
                written.append(C.write_fim_ready(agg, out_dir / name))
                return written
            for ts, group in sel.groupby("value_time"):
                per = group.rename(columns={"value": "discharge"})[
                    ["feature_id", "discharge"]
                ]
                written.append(C.write_fim_ready(per, out_dir / f"NWM_{C.stamp(ts)}.csv"))
            log.info("Selected %d hourly FIM-ready CSVs from archive", len(written))
            return written

        raise ValueError("Provide date=, or start= and end=.")

    def _load_archive(self) -> pd.DataFrame:
        """Concatenate every parquet in the retrospective archive."""
        frames = []
        for parquet in sorted(self.archive_dir.glob("*.parquet")):
            df = pd.read_parquet(parquet)
            df["value_time"] = pd.to_datetime(df["value_time"])
            df["feature_id"] = (
                df["location_id"]
                .str.replace(_LOC_PREFIX, "", regex=False)
                .astype("int64")
            )
            frames.append(df[["value_time", "feature_id", "value"]])
        if not frames:
            return pd.DataFrame(columns=["value_time", "feature_id", "value"])
        return pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["value_time", "feature_id"]
        )

    @staticmethod
    def _require_coverage(sel: pd.DataFrame, requested: str, lo, hi) -> None:
        if sel.empty:
            raise ValueError(
                f"Requested {requested!r} is not covered by the archive "
                f"(available {lo} .. {hi}). Fetch that window first."
            )

    # internals
    @staticmethod
    def _canonical_name(start_date: str, end_date: str) -> str:
        """teehr's output filename for a request: ``YYYYMMDD.parquet`` when the
        start and end days match, else ``YYYYMMDD_YYYYMMDD.parquet``."""
        s = pd.to_datetime(start_date).strftime("%Y%m%d")
        e = pd.to_datetime(end_date).strftime("%Y%m%d")
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

    def _covering_file(self, start_date: str, end_date: str) -> Optional[Path]:
        """An already-downloaded parquet whose day span fully contains the
        request (so a wider prior download is reused instead of re-fetching)."""
        s = pd.to_datetime(start_date).strftime("%Y%m%d")
        e = pd.to_datetime(end_date).strftime("%Y%m%d")
        for parquet in sorted(self.archive_dir.glob("*.parquet")):
            fs, fe = self._file_span(parquet)
            if fs <= s and fe >= e:
                return parquet
        return None

    def _read_range(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Read the parquet covering [start_date, end_date] (the canonical file
        or a wider one that contains it) and clip to the exact window."""
        parquet = self.archive_dir / self._canonical_name(start_date, end_date)
        if not parquet.exists():
            parquet = self._covering_file(start_date, end_date)
        if parquet is None or not parquet.exists():
            raise FileNotFoundError(
                f"No NWM parquet under {self.archive_dir} for {start_date} .. {end_date}"
            )
        df = pd.read_parquet(parquet)
        df["value_time"] = pd.to_datetime(df["value_time"])
        df["feature_id"] = (
            df["location_id"].str.replace(_LOC_PREFIX, "", regex=False).astype("int64")
        )
        lo, hi = pd.to_datetime(start_date), pd.to_datetime(end_date)
        df = df[(df["value_time"] >= lo) & (df["value_time"] <= hi)]
        return df.drop_duplicates(subset=["value_time", "feature_id"]).reset_index(drop=True)

    @staticmethod
    def _aggregate(df: pd.DataFrame, sortby: str) -> pd.DataFrame:
        funcs = {"maximum": "max", "minimum": "min", "mean": "mean"}
        if sortby not in funcs:
            raise ValueError(f"sortby must be one of {list(funcs)}, got {sortby!r}")
        agg = (
            df.groupby("feature_id")["value"].agg(funcs[sortby]).reset_index()
        )
        return agg.rename(columns={"value": "discharge"})
