"""
Author: Supath Dhital
Date Updated: May 2026

Top-level FIM generation pipeline.

Given an AOI directory plus a forecast file or DataFrame of (feature_id, discharge_cms), walk
every branch and produce per-branch inundation extent + depth rasters, then mosaic them into AOI-wide outputs.

Usage
-----
    from fimbox.fimgeneration import FimGenerator
    result = FimGenerator(aoi_dir=Path("out/HUC08060202"),
                          forecast="forecasts/nwm_2025_03_15.csv").run()
    print(result.depth_path)   # /out/HUC08060202/inundation_depth.tif
    print(result.extent_path)  # /out/HUC08060202/inundation_extent.tif

Forecast file format
--------------------
CSV with at minimum two columns:
    feature_id     int  (NWM reach id)
    discharge_cms  float (cubic meters per second)

The column 'discharge' or 'flow_cms' is also accepted and renamed
automatically.
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

import pandas as pd

from .inundator import InundationResult, Inundator, NoForecastMatch
from .mosaic import BranchMosaic, MosaicResult

PathLike = Union[str, Path]

log = logging.getLogger(__name__)


@dataclass
class FimGenerationResult:
    aoi_dir: Path
    branch_results: list[InundationResult]
    mosaic: Optional[MosaicResult]

    @property
    def depth_path(self) -> Optional[Path]:
        return self.mosaic.depth_path if self.mosaic else None

    @property
    def extent_path(self) -> Optional[Path]:
        return self.mosaic.extent_path if self.mosaic else None

    @property
    def n_branches_ok(self) -> int:
        return sum(1 for r in self.branch_results if not r.skipped)

    @property
    def n_branches_skipped(self) -> int:
        return sum(1 for r in self.branch_results if r.skipped)


@dataclass
class FimGenerator:
    aoi_dir: PathLike
    forecast: Union[PathLike, pd.DataFrame]

    # If set, limit generation to these branches. Otherwise walk every
    # subdirectory of <aoi_dir>/branches.
    branch_ids: Optional[Sequence[str]] = None

    # Whether to mosaic per-branch outputs into AOI-level rasters at the
    # end. Default True.
    mosaic: bool = True

    # Parallel workers for the per-branch loop. 1 = serial.
    n_workers: int = 1

    # Tunable forwarded to Inundator.
    min_depth_m: float = 0.03
    drop_lakes: bool = True

    # AOI-level mosaic output paths. When None, default to
    # <aoi_dir>/inundation_depth.tif and inundation_extent.tif.
    depth_out: Optional[PathLike] = None
    extent_out: Optional[PathLike] = None

    # Directory where per-branch intermediates are written before
    # mosaicking. Default = <aoi_dir>/fimbox_output/tmp. When
    # cleanup_intermediates=True (the default) this directory is wiped
    # after the mosaic succeeds so it doesn't pollute the AOI tree.
    intermediate_dir: Optional[PathLike] = None
    cleanup_intermediates: bool = True

    def __post_init__(self) -> None:
        self.aoi_dir = Path(self.aoi_dir)

    def run(self) -> FimGenerationResult:
        if not self.aoi_dir.is_dir():
            raise NotADirectoryError(self.aoi_dir)

        branch_root = self.aoi_dir / "branches"
        if not branch_root.is_dir():
            raise NotADirectoryError(branch_root)

        bids = self.branch_ids or sorted(
            p.name for p in branch_root.iterdir() if p.is_dir()
        )
        if not bids:
            raise FileNotFoundError("No branches under {self.aoi_dir}")

        # Normalise the forecast once so each worker doesn't re-read it.
        forecast_df = _load_forecast(self.forecast)

        # Per-branch intermediates land here. Default: <aoi_dir>/fimbox_output/tmp.
        tmp_dir = (
            Path(self.intermediate_dir)
            if self.intermediate_dir is not None
            else self.aoi_dir / "fimbox_output" / "tmp"
        )
        tmp_dir.mkdir(parents=True, exist_ok=True)

        log.info(
            f"FimGenerator: AOI={self.aoi_dir.name} branches={len(bids)} "
            f"workers={self.n_workers} mosaic={self.mosaic} "
            f"intermediate_dir={tmp_dir}"
        )

        if self.n_workers <= 1:
            results = [
                _run_one_branch(
                    branch_root / bid, bid, forecast_df,
                    self.min_depth_m, self.drop_lakes, tmp_dir,
                )
                for bid in bids
            ]
        else:
            results = []
            with ProcessPoolExecutor(max_workers=self.n_workers) as pool:
                fut_to_bid = {
                    pool.submit(
                        _run_one_branch,
                        branch_root / bid, bid, forecast_df,
                        self.min_depth_m, self.drop_lakes, tmp_dir,
                    ): bid
                    for bid in bids
                }
                for fut in as_completed(fut_to_bid):
                    bid = fut_to_bid[fut]
                    try:
                        results.append(fut.result())
                    except Exception as exc:
                        log.error(
                            f"FimGenerator: branch {bid} crashed: {exc}",
                            exc_info=True,
                        )
                        results.append(InundationResult(
                            branch_id=bid,
                            extent_path=tmp_dir / f"inundation_extent_{bid}.tif",
                            depth_path=tmp_dir / f"inundation_depth_{bid}.tif",
                            n_hydroids_wet=0, n_pixels_wet=0, max_depth_m=0.0,
                            skipped=True,
                        ))

        results.sort(key=lambda r: r.branch_id)

        mosaic_result: Optional[MosaicResult] = None
        if self.mosaic:
            ok_bids = [r.branch_id for r in results if not r.skipped]
            if not ok_bids:
                log.warning(
                    "FimGenerator: no successful branches — skipping mosaic"
                )
            else:
                mosaic_result = BranchMosaic(
                    aoi_dir=self.aoi_dir,
                    branch_ids=ok_bids,
                    depth_out=self.depth_out,
                    extent_out=self.extent_out,
                    sources_dir=tmp_dir,
                ).run()

        # Clean up per-branch intermediates once the mosaic is done.
        if self.cleanup_intermediates and self.mosaic and mosaic_result is not None:
            import shutil
            try:
                shutil.rmtree(tmp_dir)
                log.info(f"Cleaned up intermediates: {tmp_dir}")
            except OSError as exc:
                log.warning(f"Could not remove {tmp_dir}: {exc}")

        return FimGenerationResult(
            aoi_dir=self.aoi_dir,
            branch_results=results,
            mosaic=mosaic_result,
        )


def _run_one_branch(
    branch_dir: Path,
    branch_id: str,
    forecast: pd.DataFrame,
    min_depth_m: float,
    drop_lakes: bool,
    out_dir: Path,
) -> InundationResult:
    return Inundator(
        branch_dir=branch_dir,
        branch_id=branch_id,
        forecast=forecast,
        min_depth_m=min_depth_m,
        drop_lakes=drop_lakes,
        out_dir=out_dir,
    ).run()


def _load_forecast(src: Union[PathLike, pd.DataFrame]) -> pd.DataFrame:
    # Read once at orchestrator level so workers receive a DataFrame.
    if isinstance(src, pd.DataFrame):
        return src.copy()
    p = Path(src)
    if not p.is_file():
        raise FileNotFoundError(f"forecast file not found: {p}")
    if p.suffix.lower() in (".parquet", ".pq"):
        return pd.read_parquet(p)
    return pd.read_csv(p)


def extract_feature_ids(
    aoi_dir: PathLike,
    out_csv: Optional[PathLike] = None,
) -> Path:
    # Scan every per-branch hydroTable in the AOI and write
    # <aoi_dir>/feature_id.csv (single column) listing every unique
    # feature_id the AOI knows about. The user can then author a
    # discharge CSV (feature_id,discharge_cms) and drop it into
    # <aoi_dir>/discharge_inputs/ before running FimGenerator.
    #
    # Pass out_csv to override the default location. Returns the path
    # to the written CSV.
    import glob

    aoi = Path(aoi_dir)
    pattern = str(aoi / "branches" / "*" / "hydroTable_*.csv")
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"No hydroTable_*.csv files under {aoi}/branches/")

    frames = [pd.read_csv(p, usecols=["feature_id"]) for p in paths]
    fids = (
        pd.concat(frames, ignore_index=True)["feature_id"]
        .drop_duplicates()
        .sort_values()
        .reset_index(drop=True)
    )
    target = Path(out_csv) if out_csv is not None else (aoi / "feature_id.csv")
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"feature_id": fids}).to_csv(target, index=False)
    log.info(f"extract_feature_ids: {len(fids)} ids --> {target}")
    return target
