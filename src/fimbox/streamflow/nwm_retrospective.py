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

    # downloading
    def fetch(self, start_date: str, end_date: str) -> Path:
        """Download the [start_date, end_date] hourly parquet (idempotent).
        Dates are ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM:SS``. Returns the parquet
        path."""
        parquet = self.archive_dir / self._parquet_name(start_date, end_date)
        if parquet.exists():
            log.info("SKIP (exists): %s", parquet.name)
            return parquet

        nwm_retro = C.require(
            "teehr.fetching.nwm.retrospective_points"
        )
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
        log.info("NWM retrospective parquet --> %s", parquet.name)
        return parquet

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
    def _parquet_name(self, start_date: str, end_date: str) -> str:
        return f"{start_date.replace('-', '')}_{end_date.replace('-', '')}.parquet"

    def _read_range(self, start_date: str, end_date: str) -> pd.DataFrame:
        parquet = self.archive_dir / self._parquet_name(start_date, end_date)
        if not parquet.exists():
            raise FileNotFoundError(f"NWM parquet not found: {parquet}")
        df = pd.read_parquet(parquet)
        df["value_time"] = pd.to_datetime(df["value_time"])
        df["feature_id"] = (
            df["location_id"].str.replace(_LOC_PREFIX, "", regex=False).astype("int64")
        )
        return df

    @staticmethod
    def _aggregate(df: pd.DataFrame, sortby: str) -> pd.DataFrame:
        funcs = {"maximum": "max", "minimum": "min", "mean": "mean"}
        if sortby not in funcs:
            raise ValueError(f"sortby must be one of {list(funcs)}, got {sortby!r}")
        agg = (
            df.groupby("feature_id")["value"].agg(funcs[sortby]).reset_index()
        )
        return agg.rename(columns={"value": "discharge"})
