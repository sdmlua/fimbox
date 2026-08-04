"""
NextGen-in-a-Box -> fimbox bridge.

One entry point takes an area of interest (shapefile / GeoPackage) and returns
everything downstream FIM generation needs from the NextGen ecosystem:

  1. the hydrofabric catchments (``divides``) that intersect the AOI,
  2. the NextGen network ids (``wb-*`` flowpaths / integer ``feature_id``s) and
     their NWM ``hf_id`` crosswalk, and
  3. the ngen/t-route discharge for those reaches, pulled from the public
     CIROH community NextGen DataStream S3 bucket and written as FIM-ready
     ``feature_id, discharge_cms`` CSVs.

Everything lands in the standard fimbox AOI layout::

    <AOI_root>/
      feature_id.csv                 -- unique NextGen feature_ids
      hydrofabric/
        aoi_catchments.gpkg          -- intersecting catchment polygons
        aoi_flowpaths.gpkg           -- their flowpaths
        network_crosswalk.csv        -- divide_id, wb_id, feature_id, hf_id, vpu
      discharge-inputs/
        nextgen_<model>_<forecast>_<date><cycle>_<agg>.csv
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import pandas as pd

from . import _common as C
from .datastream import DEFAULT_FORECAST, DEFAULT_MODEL, NextGenDatastream
from .hydrofabric import AOIHydrofabric, NextGenHydrofabric

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


@dataclass
class NextGenResult:
    """Everything the AOI resolution produced.

    Attributes
    ----------
    aoi_dir : Path
        AOI root the outputs were written under.
    hydrofabric : AOIHydrofabric
        Catchments, flowpaths, and the network crosswalk.
    catchments_path : Path
        Saved catchment GeoPackage (the hydrofabric "shapefile").
    crosswalk_path : Path
        Saved ``network_crosswalk.csv``.
    discharge : DataFrame
        Long ``[feature_id, time, flow]`` discharge series (cms).
    discharge_csvs : list[Path]
        FIM-ready ``feature_id, discharge_cms`` CSVs.
    run : str
        The datastream run used (``model/forecast/ngen.date/cycle/VPU``).
    """

    aoi_dir: Path
    hydrofabric: AOIHydrofabric
    catchments_path: Path
    crosswalk_path: Path
    discharge: pd.DataFrame
    discharge_csvs: list[Path]
    run: str

    @property
    def feature_ids(self) -> list[int]:
        return self.hydrofabric.feature_ids

    @property
    def network_ids(self) -> list[str]:
        return self.hydrofabric.network_ids

    @property
    def catchments(self) -> gpd.GeoDataFrame:
        return self.hydrofabric.catchments


def getNextGenAOI(
    aoi: Union[PathLike, gpd.GeoDataFrame],
    out_dir: Optional[PathLike] = None,
    *,
    aoi_layer: Optional[str] = None,
    predicate: str = "intersects",
    nwm_crosswalk: bool = False,
    model: str = DEFAULT_MODEL,
    forecast: str = DEFAULT_FORECAST,
    date: Optional[str] = None,
    cycle: Optional[str] = None,
    sortby: Optional[str] = "maximum",
    at_time: Optional[str] = None,
    fetch_discharge: bool = True,
) -> NextGenResult:
    """Resolve an AOI to NextGen catchments + discharge in one call.

    Parameters
    ----------
    aoi : path or GeoDataFrame
        Area of interest (shapefile / GeoPackage / GeoJSON, or a GeoDataFrame).
    out_dir : path, optional
        Root output directory; an AOI folder ``<out_dir>/<aoi_stem>`` is created
        underneath. Defaults to ``./out``.
    aoi_layer : str, optional
        Layer name when the AOI is a multi-layer GeoPackage.
    predicate : str
        ``"intersects"`` (default) or ``"within"`` for catchment selection.
    nwm_crosswalk : bool
        Also resolve each reach's NWM/NHD ``hf_id`` (COMID) from the hydrofabric
        ``network`` table. Off by default because that read scans the whole
        table over S3.
    model, forecast, date, cycle
        Discharge selection on the datastream bucket. Defaults: ``cfe_nom`` /
        ``short_range`` / latest date / latest cycle that has the AOI's VPU.
    sortby : str or None
        Horizon aggregation for the FIM-ready CSV: ``"maximum"`` (default),
        ``"minimum"``, ``"mean"``, or ``None`` for one CSV per timestep.
    at_time : str, optional
        Single timestamp to slice instead of aggregating.
    fetch_discharge : bool
        Set False to resolve catchments/ids only (no S3 discharge download).

    Returns
    -------
    NextGenResult
    """
    return NextGenAOI(
        aoi,
        out_dir,
        aoi_layer=aoi_layer,
        predicate=predicate,
        nwm_crosswalk=nwm_crosswalk,
    ).run(
        model=model,
        forecast=forecast,
        date=date,
        cycle=cycle,
        sortby=sortby,
        at_time=at_time,
        fetch_discharge=fetch_discharge,
    )


class NextGenAOI:
    """AOI -> NextGen hydrofabric catchments + datastream discharge."""

    def __init__(
        self,
        aoi: Union[PathLike, gpd.GeoDataFrame],
        out_dir: Optional[PathLike] = None,
        *,
        aoi_layer: Optional[str] = None,
        predicate: str = "intersects",
        nwm_crosswalk: bool = False,
    ):
        self.aoi = aoi
        self.aoi_layer = aoi_layer
        self.predicate = predicate
        self.nwm_crosswalk = nwm_crosswalk

        stem = Path(aoi).stem if not isinstance(aoi, gpd.GeoDataFrame) else "aoi"
        root = Path(out_dir) if out_dir else (Path.cwd() / "out")
        self.aoi_dir = (root / stem).resolve()
        self.aoi_dir.mkdir(parents=True, exist_ok=True)
        C.attach_log(self.aoi_dir)

    def run(
        self,
        *,
        model: str = DEFAULT_MODEL,
        forecast: str = DEFAULT_FORECAST,
        date: Optional[str] = None,
        cycle: Optional[str] = None,
        sortby: Optional[str] = "maximum",
        at_time: Optional[str] = None,
        fetch_discharge: bool = True,
    ) -> NextGenResult:
        log.info("=== NextGen AOI: %s ===", self.aoi_dir.name)

        log.info("--- Hydrofabric (AOI -> catchments) ---")
        hf = NextGenHydrofabric(
            self.aoi,
            aoi_layer=self.aoi_layer,
            predicate=self.predicate,
            nwm_crosswalk=self.nwm_crosswalk,
        )
        result = hf.resolve_and_save(self.aoi_dir)
        cat_path = C.hydrofabric_dir(self.aoi_dir) / "aoi_catchments.gpkg"
        xwalk_path = C.hydrofabric_dir(self.aoi_dir) / "network_crosswalk.csv"

        discharge = pd.DataFrame(columns=["feature_id", "time", "flow"])
        csvs: list[Path] = []
        run_label = "(discharge not fetched)"

        if fetch_discharge:
            if len(result.vpus) > 1:
                log.warning(
                    "AOI spans %d VPUs (%s); fetching discharge from each.",
                    len(result.vpus),
                    ", ".join(result.vpus),
                )
            parts, labels = [], []
            for vpu in result.vpus:
                log.info("--- Discharge (%s) ---", vpu)
                fids = [
                    int(f)
                    for f in result.crosswalk.loc[
                        result.crosswalk["vpu"] == vpu, "feature_id"
                    ].dropna()
                ]
                ds = NextGenDatastream(
                    vpu, model=model, forecast=forecast, date=date, cycle=cycle
                )
                parts.append(ds.read_discharge(fids))
                csvs += ds.to_fim_inputs(
                    self.aoi_dir, fids, sortby=sortby, at_time=at_time
                )
                labels.append(str(ds.run))
            if parts:
                discharge = pd.concat(parts, ignore_index=True)
            run_label = "; ".join(labels)

        log.info("=== DONE: %s ===", self.aoi_dir.name)
        return NextGenResult(
            aoi_dir=self.aoi_dir,
            hydrofabric=result,
            catchments_path=cat_path,
            crosswalk_path=xwalk_path,
            discharge=discharge,
            discharge_csvs=csvs,
            run=run_label,
        )


def _main() -> None:
    import argparse

    from ..logging_utils import configure_cli_logging

    configure_cli_logging()

    p = argparse.ArgumentParser(
        description="Resolve an AOI to NextGen hydrofabric catchments and pull "
        "ngen/t-route discharge from the CIROH community datastream bucket."
    )
    p.add_argument("aoi", help="AOI shapefile / GeoPackage / GeoJSON path")
    p.add_argument("--aoi-layer", default=None, help="AOI layer (multi-layer gpkg)")
    p.add_argument("--out-dir", default="out", help="Root output dir (default ./out)")
    p.add_argument(
        "--predicate",
        default="intersects",
        choices=["intersects", "within"],
        help="Catchment selection test (default intersects)",
    )
    p.add_argument(
        "--nwm-crosswalk",
        action="store_true",
        help="Also resolve NWM/NHD hf_id (COMID) crosswalk (slower S3 read)",
    )
    p.add_argument(
        "--model", default=DEFAULT_MODEL, help="ngen model (default cfe_nom)"
    )
    p.add_argument(
        "--forecast",
        default=DEFAULT_FORECAST,
        help="Forecast product (short_range, medium_range, analysis_assim_extend)",
    )
    p.add_argument("--date", default=None, help="Run date YYYYMMDD (default latest)")
    p.add_argument("--cycle", default=None, help="Cycle hour, e.g. 06 (default latest)")
    p.add_argument(
        "--sortby",
        default="maximum",
        help="Horizon aggregation: maximum|minimum|mean|none (default maximum)",
    )
    p.add_argument("--at-time", default=None, help="Single timestamp to slice")
    p.add_argument(
        "--no-discharge",
        action="store_true",
        help="Resolve catchments/ids only; skip discharge download",
    )
    args = p.parse_args()

    sortby = None if str(args.sortby).lower() == "none" else args.sortby
    res = getNextGenAOI(
        args.aoi,
        args.out_dir,
        aoi_layer=args.aoi_layer,
        predicate=args.predicate,
        nwm_crosswalk=args.nwm_crosswalk,
        model=args.model,
        forecast=args.forecast,
        date=args.date,
        cycle=args.cycle,
        sortby=sortby,
        at_time=args.at_time,
        fetch_discharge=not args.no_discharge,
    )
    log.info(
        "AOI %s: %d catchments, %d feature_ids, VPU(s) %s",
        res.aoi_dir.name,
        len(res.catchments),
        len(res.feature_ids),
        ", ".join(res.hydrofabric.vpus),
    )
    log.info("Discharge run(s): %s", res.run)
    log.info("Catchments: %s", res.catchments_path)
    log.info("Crosswalk:  %s", res.crosswalk_path)
    for c in res.discharge_csvs:
        log.info("Discharge:  %s", c)


if __name__ == "__main__":
    _main()
