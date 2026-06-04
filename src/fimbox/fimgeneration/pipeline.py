"""
Author: Supath Dhital
Date Updated: May 2026

Top-level FIM generation pipeline.

Outcomes
-----
    print(result.depth_path)   # /out/AOI/inundation_depth.tif
    print(result.extent_path)  # /out/AOI/inundation_extent.tif
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


# Dask is optional. When available the branch loop dispatches to the
# shared LocalCluster from fimbox._dask, which auto-sizes to the
# machine's CPU/RAM and reuses the same worker pool the preprocessing
# pipeline uses. When unavailable we fall back to ProcessPoolExecutor.
try:
    from .._dask import get_client as _get_dask_client
    from distributed import as_completed as _dask_as_completed
    _DASK_AVAILABLE = True
except ImportError:
    _DASK_AVAILABLE = False


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

    # If set, limit generation to these branches. Otherwise walk every subdirectory of <aoi_dir>/branches.
    branch_ids: Optional[Sequence[str]] = None

    # Whether to mosaic per-branch outputs into AOI-level rasters at the end. Default True.
    mosaic: bool = True

    # Parallel workers for the per-branch loop. 1 = serial. When use_dask
    # is enabled n_workers is ignored — the shared Dask LocalCluster sizes
    # itself from the machine.
    n_workers: int = 1

    # When True, dispatch the branch loop through the process-wide Dask
    # LocalCluster (auto-sized to the machine). When None, Dask is used
    # whenever it's installed. Set False to force ProcessPoolExecutor.
    use_dask: Optional[bool] = None

    # Tunable forwarded to Inundator.
    min_depth_m: float = 0.03
    drop_lakes: bool = True

    # When True, depth rasters are written as int16 millimetres instead of
    # float32 metres. Halves disk footprint and matches NOAA's int16 mode.
    int16_mode: bool = False

    depth_out: Optional[PathLike] = None
    extent_out: Optional[PathLike] = None

    # Directory where per-branch intermediates are written before mosaicking.
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

        # Per-branch intermediates land here. 
        tmp_dir = (
            Path(self.intermediate_dir)
            if self.intermediate_dir is not None
            else self.aoi_dir / "fimbox_output" / "tmp"
        )
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # Pick the dispatch backend. Dask is preferred when available
        # because the shared LocalCluster is sized to the machine and
        # reuses the worker pool the preprocessing pipeline already
        # warmed up. Fall back to ProcessPoolExecutor otherwise.
        use_dask = self.use_dask
        if use_dask is None:
            use_dask = _DASK_AVAILABLE and len(bids) > 1
        if use_dask and not _DASK_AVAILABLE:
            log.warning(
                "use_dask=True but distributed is not installed — "
                "falling back to ProcessPoolExecutor"
            )
            use_dask = False

        log.info(
            f"FimGenerator: AOI={self.aoi_dir.name} branches={len(bids)} "
            f"backend={'dask' if use_dask else ('serial' if self.n_workers <= 1 else 'process_pool')} "
            f"mosaic={self.mosaic} intermediate_dir={tmp_dir}"
        )

        if use_dask:
            results = self._run_with_dask(bids, branch_root, forecast_df, tmp_dir)
        elif self.n_workers <= 1:
            results = [
                _run_one_branch(
                    branch_root / bid, bid, forecast_df,
                    self.min_depth_m, self.drop_lakes, self.int16_mode, tmp_dir,
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
                        self.min_depth_m, self.drop_lakes, self.int16_mode, tmp_dir,
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

    def _run_with_dask(
        self,
        bids: Sequence[str],
        branch_root: Path,
        forecast_df: pd.DataFrame,
        tmp_dir: Path,
    ) -> list[InundationResult]:
        # Submit every branch to the shared LocalCluster. retries=0
        # mirrors the process_branches setup: an OOMed branch should
        # surface immediately as 'failed' so siblings finish instead
        # of cascading into a KilledWorker storm.
        client = _get_dask_client()
        log.info(
            "FimGenerator (dask): %d branches -> %d workers (dashboard %s)",
            len(bids),
            len(client.scheduler_info()["workers"]),
            client.dashboard_link,
        )
        futures = client.map(
            _run_one_branch,
            [branch_root / b for b in bids],
            list(bids),
            [forecast_df] * len(bids),
            [self.min_depth_m] * len(bids),
            [self.drop_lakes] * len(bids),
            [self.int16_mode] * len(bids),
            [tmp_dir] * len(bids),
            pure=False,
            retries=0,
        )
        fut_to_bid = {fut.key: bid for fut, bid in zip(futures, bids)}
        results: list[InundationResult] = []
        for fut in _dask_as_completed(futures):
            bid = fut_to_bid.get(fut.key, "?")
            try:
                results.append(fut.result())
            except Exception as exc:
                log.error(
                    f"FimGenerator (dask): branch {bid} crashed: {exc}",
                    exc_info=True,
                )
                results.append(InundationResult(
                    branch_id=bid,
                    extent_path=tmp_dir / f"inundation_extent_{bid}.tif",
                    depth_path=tmp_dir / f"inundation_depth_{bid}.tif",
                    n_hydroids_wet=0, n_pixels_wet=0, max_depth_m=0.0,
                    skipped=True,
                ))
        return results


def _run_one_branch(
    branch_dir: Path,
    branch_id: str,
    forecast: pd.DataFrame,
    min_depth_m: float,
    drop_lakes: bool,
    int16_mode: bool,
    out_dir: Path,
) -> InundationResult:
    return Inundator(
        branch_dir=branch_dir,
        branch_id=branch_id,
        forecast=forecast,
        min_depth_m=min_depth_m,
        drop_lakes=drop_lakes,
        int16_mode=int16_mode,
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



# CLI
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Two-step FIM generation: extract feature_ids, then run."
    )
    parser.add_argument("--aoi-dir", required=True)
    parser.add_argument(
        "--extract-only", action="store_true",
        help="Step 1 only: write <aoi_dir>/feature_id.csv and exit.",
    )
    parser.add_argument(
        "--forecast", default=None,
        help="Step 2: a single discharge CSV. When omitted, every CSV under "
             "<aoi_dir>/discharge_inputs/ is processed.",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--int16", action="store_true",
        help="Write depth raster as int16 millimetres.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    aoi_dir = Path(args.aoi_dir)

    if args.extract_only:
        out = extract_feature_ids(aoi_dir)
        print(f"feature_id list -> {out}")
    else:
        # Resolve which discharge CSVs to run.
        if args.forecast:
            csvs = [Path(args.forecast)]
        else:
            ddir = aoi_dir / "discharge_inputs"
            if not ddir.is_dir():
                parser.error(
                    f"{ddir} not found — run --extract-only first, drop your "
                    "discharge CSVs into it, then rerun."
                )
            csvs = sorted(ddir.glob("*.csv"))
            if not csvs:
                parser.error(f"No discharge CSVs in {ddir}")

        output_dir = aoi_dir / "fimbox_output"
        output_dir.mkdir(exist_ok=True)
        for csv in csvs:
            base = csv.stem
            print(f"\n=== {csv.name} ===")
            result = FimGenerator(
                aoi_dir=aoi_dir,
                forecast=csv,
                n_workers=args.workers,
                int16_mode=args.int16,
                depth_out=output_dir / f"{base}_depth.tif",
                extent_out=output_dir / f"{base}_inundation.tif",
            ).run()
            print(f"  depth  -> {result.depth_path}")
            print(f"  extent -> {result.extent_path}")
            if result.mosaic is not None:
                print(f"  {result.mosaic.n_wet_pixels:,} wet pixels, "
                      f"max depth {result.mosaic.max_depth_m:.2f} m")
