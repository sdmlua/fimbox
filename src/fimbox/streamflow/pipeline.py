"""
Author: Supath Dhital
Date Created: June 2026

Streamflow pipeline, & CLI.

Given an AOI directory and its feature_id CSV, retrieve streamflow and route it:

  * archive   -> <AOI>/streamflow/...        (all sources, full series, parquet)
  * FIM-ready -> <AOI>/discharge-inputs/...   (feature_id, discharge_cms CSVs the
                                               FIM generator later consumes)

Retrospective is the default FIM source: a single datetime yields one CSV, a
date range yields one CSV per hour (or one aggregated CSV with --sortby).
Forecast (short/medium/long range) yields per-day aggregated CSVs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

from ..logging_utils import attach_case_log
from . import _common as C
from .nwm_forecast import NWMForecast
from .nwm_retrospective import NWMRetrospective

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


class StreamflowPipeline:
    """Orchestrate streamflow retrieval for an AOI and produce FIM-ready CSVs."""

    def __init__(
        self,
        aoi_dir: PathLike,
        feature_id_csv: Optional[PathLike] = None,
    ):
        self.aoi_dir = Path(aoi_dir)
        # Default to the AOI root's feature_id.csv (written by the FIM extract step).
        self.feature_id_csv = (
            Path(feature_id_csv)
            if feature_id_csv is not None
            else C.resolve_aoi(aoi_dir) / "feature_id.csv"
        )
        # Route every streamflow log line into the AOI's combined processing.log.
        attach_case_log(self.aoi_dir)
        log.info("Streamflow pipeline | AOI=%s", C.resolve_aoi(aoi_dir).name)

    # retrospective (default FIM source)
    def retrospective(
        self,
        *,
        date: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        sortby: Optional[str] = None,
    ) -> list[Path]:
        """Single ``date`` -> one CSV; ``start``/``end`` range -> one CSV per
        hour, or a single aggregated CSV when ``sortby`` is given."""
        if not self.feature_id_csv.exists():
            raise FileNotFoundError(f"feature_id CSV not found: {self.feature_id_csv}")
        retro = NWMRetrospective(self.aoi_dir, self.feature_id_csv)
        if date:
            return [retro.at(date)]
        if start and end:
            return retro.to_fim_inputs(start, end, sortby=sortby)
        raise ValueError("Provide date=, or start= and end=.")

    def select(
        self,
        *,
        date: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        sortby: Optional[str] = None,
    ) -> list[Path]:
        """Filter the ALREADY-downloaded retrospective archive to a narrower
        date/range and write FIM-ready CSVs — no new download. Use after a wide
        ``retrospective(...)`` fetch when you want to generate FIM for a subset."""
        return NWMRetrospective(self.aoi_dir, self.feature_id_csv).select_from_archive(
            date=date, start=start, end=end, sortby=sortby
        )

    # forecast
    def forecast(
        self,
        forecast_range: str,
        *,
        forecast_date: Optional[str] = None,
        hour: Optional[int] = None,
        sort_by: str = "maximum",
    ) -> list[Path]:
        """Operational short/medium/long-range forecast -> per-day FIM-ready CSVs."""
        if not self.feature_id_csv.exists():
            raise FileNotFoundError(f"feature_id CSV not found: {self.feature_id_csv}")
        return NWMForecast(self.aoi_dir, self.feature_id_csv).to_fim_inputs(
            forecast_range,
            forecast_date=forecast_date,
            hour=hour,
            sort_by=sort_by,
        )


# CLI
def _main(argv: Optional[list[str]] = None) -> None:
    import argparse

    from ..logging_utils import configure_cli_logging

    configure_cli_logging()
    p = argparse.ArgumentParser(
        description="Retrieve streamflow and write FIM-ready CSVs."
    )
    p.add_argument(
        "--aoi-dir", required=True, help="AOI directory (root or watershed-data)."
    )
    p.add_argument(
        "--feature-id-csv", default=None, help="Defaults to <AOI>/feature_id.csv."
    )
    p.add_argument(
        "--source", choices=["retrospective", "forecast"], default="retrospective"
    )
    p.add_argument(
        "--select",
        action="store_true",
        help="Filter the already-downloaded retrospective archive (no new "
        "download) using --date or --start/--end.",
    )
    # retrospective
    p.add_argument(
        "--date", default=None, help="Single instant/day (YYYY-MM-DD[ HH:MM:SS])."
    )
    p.add_argument("--start", default=None, help="Range start (YYYY-MM-DD).")
    p.add_argument("--end", default=None, help="Range end (YYYY-MM-DD).")
    p.add_argument(
        "--sortby",
        choices=["maximum", "minimum", "mean"],
        default=None,
        help="Aggregate a range to one CSV instead of per-hour.",
    )
    # forecast
    p.add_argument(
        "--range",
        dest="frange",
        choices=["shortrange", "mediumrange", "longrange"],
        default="shortrange",
    )
    p.add_argument(
        "--forecast-date", default=None, help="Forecast cycle date (YYYY-MM-DD)."
    )
    p.add_argument("--hour", type=int, default=None, help="Forecast cycle hour (UTC).")
    p.add_argument(
        "--forecast-sortby", choices=["maximum", "minimum", "median"], default="maximum"
    )
    args = p.parse_args(argv)

    pipe = StreamflowPipeline(args.aoi_dir, args.feature_id_csv)
    if args.select:
        out = pipe.select(
            date=args.date, start=args.start, end=args.end, sortby=args.sortby
        )
    elif args.source == "retrospective":
        out = pipe.retrospective(
            date=args.date, start=args.start, end=args.end, sortby=args.sortby
        )
    else:
        out = pipe.forecast(
            args.frange,
            forecast_date=args.forecast_date,
            hour=args.hour,
            sort_by=args.forecast_sortby,
        )
    print(
        f"Wrote {len(out)} FIM-ready CSV(s) to {C.discharge_inputs_dir(args.aoi_dir)}"
    )
    for f in out:
        print(f"  {f.name}")


if __name__ == "__main__":
    _main()
