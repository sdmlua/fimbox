"""
Author: Supath Dhital
Date Created: June 2026

NWM operational forecast streamflow retrieval (short / medium / long range).

Downloads channel_rt netCDF forecast files from the public NWM bucket for the
most recent complete cycle on/before a requested date+hour, filters them to the
AOI's feature_ids, and writes aggregated FIM-ready discharge CSVs into
``<AOI>/discharge-inputs/``. Raw netCDF/CSV staging is cleaned up afterward.

Forecast ranges and cadence:
  short_range   hourly,  f001..f017
  medium_range  3-hourly, f003..f237 (mem1 outside 2018-09-17..2019-06-18)
  long_range    6-hourly, f006..f714 (mem1)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from . import _common as C

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

_URL_BASE = "https://storage.googleapis.com/national-water-model"
_MEM1_START = datetime(2018, 9, 17)
_MEM1_END = datetime(2019, 6, 18)

# range -> (valid cycle hours, retry step, max attempts)
_RANGE_CFG = {
    "shortrange": (list(range(24)), 1, 24),
    "mediumrange": ([0, 3, 6, 9, 12, 15, 18, 21], 3, 16),
    "longrange": ([0, 6, 12, 18], 6, 8),
}


class NWMForecast:
    """Fetch NWM operational forecast streamflow for an AOI's feature_ids."""

    def __init__(self, aoi_dir: PathLike, feature_id_csv: PathLike):
        self.aoi_dir = Path(aoi_dir)
        self.feature_id_csv = Path(feature_id_csv)

    def to_fim_inputs(
        self,
        forecast_range: str,
        *,
        forecast_date: Optional[str] = None,
        hour: Optional[int] = None,
        sort_by: str = "maximum",
    ) -> list[Path]:
        """Download the latest complete forecast cycle and write per-day
        aggregated FIM-ready CSVs into discharge-inputs/.

        forecast_range: 'shortrange' | 'mediumrange' | 'longrange'.
        forecast_date:  'YYYY-MM-DD' (default: today UTC).
        hour:           cycle hour; snapped to a valid value for the range.
        sort_by:        per-day aggregation 'maximum' | 'minimum' | 'median'.
        """
        if forecast_range not in _RANGE_CFG:
            raise ValueError(
                f"forecast_range must be one of {list(_RANGE_CFG)}, got {forecast_range!r}"
            )
        requests = C.require("requests")
        nc = C.require("netCDF4")

        work = C.streamflow_dir(self.aoi_dir, f"{forecast_range}_forecast")
        netcdf_root = work / "netCDF"
        csv_stage = work / "csvFiles"
        out_dir = C.discharge_inputs_dir(self.aoi_dir)

        date_str = (
            datetime.strptime(forecast_date, "%Y-%m-%d").strftime("%Y%m%d")
            if forecast_date
            else datetime.now(timezone.utc).strftime("%Y%m%d")
        )
        cycle_hour = self._snap_hour(hour, forecast_range)

        nc_dir = self._download_latest_cycle(
            requests, date_str, cycle_hour, forecast_range, netcdf_root
        )
        if nc_dir is None:
            log.warning(
                "No complete %s cycle found near %s — try an earlier day/hour.",
                forecast_range,
                date_str,
            )
            self._rmtree(netcdf_root)
            return []

        filt = pd.read_csv(self.feature_id_csv)[["feature_id"]]
        csv_stage.mkdir(parents=True, exist_ok=True)
        for f in sorted(nc_dir.glob("*.nc")):
            self._netcdf_to_csv(nc, f, filt, csv_stage)

        written = self._aggregate_daily(
            csv_stage, date_str, cycle_hour, forecast_range, sort_by, out_dir
        )

        for d in (csv_stage, netcdf_root):
            self._rmtree(d)
        log.info("Wrote %d forecast FIM-ready CSVs --> %s", len(written), out_dir)
        return written

    # cycle download
    def _download_latest_cycle(
        self, requests, date_str, cycle_hour, forecast_range, netcdf_root
    ) -> Optional[Path]:
        """Walk back cycle-by-cycle until a complete file set downloads."""
        _, step, max_attempts = _RANGE_CFG[forecast_range]
        cur_date, cur_hour = date_str, cycle_hour

        for attempt in range(max_attempts):
            log.info(
                "Forecast attempt %d/%d: %s %02dZ (%s)",
                attempt + 1,
                max_attempts,
                cur_date,
                cur_hour,
                forecast_range,
            )
            nc_dir = self._download_cycle(
                requests, cur_date, cur_hour, forecast_range, netcdf_root
            )
            if nc_dir is not None:
                log.info("Complete %s cycle: %s %02dZ", forecast_range, cur_date, cur_hour)
                return nc_dir
            prev = cur_hour
            cur_hour = (cur_hour - step) % 24
            if cur_hour > prev:
                cur_date = (
                    datetime.strptime(cur_date, "%Y%m%d") - timedelta(days=1)
                ).strftime("%Y%m%d")
        return None

    def _download_cycle(
        self, requests, date_str, cycle_hour, forecast_range, netcdf_root
    ) -> Optional[Path]:
        forecast_type = self._forecast_type(date_str, forecast_range)
        url = f"{_URL_BASE}/nwm.{date_str}/{forecast_type}/"
        expected = self._expected_files(forecast_type, cycle_hour)
        if not expected:
            return None

        dest = netcdf_root / date_str
        dest.mkdir(parents=True, exist_ok=True)
        got = 0
        for fname in expected:
            path = dest / fname
            try:
                r = requests.get(url + fname)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                path.write_bytes(r.content)
                if path.exists() and path.stat().st_size > 0:
                    got += 1
            except requests.exceptions.RequestException as exc:
                log.debug("download failed %s: %s", fname, exc)

        if got == len(expected):
            return dest
        self._rmtree(dest)
        return None

    @staticmethod
    def _forecast_type(date_str: str, forecast_range: str) -> str:
        date_obj = datetime.strptime(date_str, "%Y%m%d")
        if forecast_range == "shortrange":
            return "short_range"
        if forecast_range == "mediumrange":
            return (
                "medium_range"
                if _MEM1_START <= date_obj <= _MEM1_END
                else "medium_range_mem1"
            )
        return "long_range_mem1"

    @staticmethod
    def _expected_files(forecast_type: str, cycle_hour: int) -> list[str]:
        h = cycle_hour
        if forecast_type == "short_range":
            rng, var = range(1, 18), "short_range.channel_rt"
        elif forecast_type == "medium_range":
            rng, var = range(3, 240, 3), "medium_range.channel_rt"
        elif forecast_type == "medium_range_mem1":
            rng, var = range(3, 240, 3), "medium_range.channel_rt_1"
        elif forecast_type == "long_range_mem1":
            rng, var = range(6, 720, 6), "long_range.channel_rt_1"
        else:
            return []
        return [f"nwm.t{h:02d}z.{var}.f{i:03d}.conus.nc" for i in rng]

    @staticmethod
    def _snap_hour(hour: Optional[int], forecast_range: str) -> int:
        valid, _, _ = _RANGE_CFG[forecast_range]
        if hour is None:
            hour = datetime.now(timezone.utc).hour
        hour = max(0, min(int(hour), 23))
        return max([h for h in valid if h <= hour] or [valid[0]])

    # netCDF -> per-file CSV
    @staticmethod
    def _netcdf_to_csv(nc, nc_path: Path, filt_df: pd.DataFrame, out_dir: Path) -> None:
        try:
            ds = nc.Dataset(str(nc_path), "r")
            flow = ds.variables["streamflow"][:]
            fids = ds.variables["feature_id"][:]
            ds.close()
        except Exception as exc:
            log.debug("unreadable netCDF %s: %s", nc_path.name, exc)
            return
        if len(flow) == 0 or len(fids) == 0:
            return
        data = pd.DataFrame({"feature_id": fids, "discharge": flow})
        merged = filt_df[["feature_id"]].merge(data, on="feature_id")
        merged.to_csv(out_dir / f"{nc_path.stem}.csv", index=False)

    # daily aggregation -> FIM-ready CSVs
    def _aggregate_daily(
        self, csv_stage, date_str, cycle_hour, forecast_range, sort_by, out_dir
    ) -> list[Path]:
        prefix_map = {
            "shortrange": "short_range",
            "mediumrange": "medium_range",
            "longrange": "long_range",
        }
        prefix = f"nwm.t{cycle_hour:02d}z.{prefix_map[forecast_range]}"
        files = sorted(p for p in csv_stage.glob("*.csv") if p.name.startswith(prefix))
        if not files:
            return []

        fhour = re.compile(r"\.f(\d{3})\.")
        base = datetime.strptime(date_str, "%Y%m%d")
        groups: dict[str, list[Path]] = {}
        for f in files:
            m = fhour.search(f.name)
            if not m:
                continue
            day_offset = (int(m.group(1)) + cycle_hour) // 24
            key = (base + timedelta(days=day_offset)).strftime("%Y%m%d")
            groups.setdefault(key, []).append(f)

        agg = {"minimum": "min", "median": "median"}.get(sort_by, "max")
        written: list[Path] = []
        for day, day_files in sorted(groups.items()):
            combined = pd.concat([pd.read_csv(f) for f in day_files], ignore_index=True)
            per = (
                combined.groupby("feature_id")["discharge"].agg(agg).reset_index()
            )
            name = f"{cycle_hour:02d}UTC_{forecast_range}_{day}.csv"
            written.append(C.write_fim_ready(per, out_dir / name))
        return written

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
