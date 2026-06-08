"""
Author: Supath Dhital
Date Updated: May 2026

Top-level FIM generation pipeline.

Layout
------
The branch HAND outputs live in the AOI's ``watershed-data/`` subfolder, while
the discharge inputs and FIM outputs live at the AOI root::

    <AOI_root>/
      feature_id.csv               # written by extract_feature_ids
      watershed-data/branches/...   # per-branch HAND outputs (read here)
      discharge-inputs/*.csv        # discharge forecasts (FIM input)
      fim-outputs/*.tif             # final depth / extent rasters

``FimGenerator`` accepts either the AOI root or its ``watershed-data/`` folder
as ``aoi_dir`` — it resolves ``branches/`` from watershed-data and writes the
mosaic to the AOI root either way.

Outcomes
-----
    print(result.depth_path)   # /out/AOI/fim-outputs/<name>_depth.tif
    print(result.extent_path)  # /out/AOI/fim-outputs/<name>_inundation.tif
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

import pandas as pd

from ..logging_utils import WATERSHED_DIR_NAME, aoi_root
from .inundator import InundationResult, Inundator, NoForecastMatch
from .mosaic import BranchMosaic, MosaicResult

PathLike = Union[str, Path]

log = logging.getLogger(__name__)


def _resolve_watershed_dir(aoi_dir: Path) -> Path:
    """Return the folder that holds ``branches/`` for an AOI.

    Accepts either the AOI root (in which case ``watershed-data/`` is used when
    present) or the ``watershed-data/`` folder itself. Falls back to ``aoi_dir``
    unchanged for legacy flat layouts where ``branches/`` sits at the root.
    """
    if aoi_dir.name == WATERSHED_DIR_NAME:
        return aoi_dir
    candidate = aoi_dir / WATERSHED_DIR_NAME
    if (candidate / "branches").is_dir():
        return candidate
    return aoi_dir


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
    # float32 metres. Halves the disk footprint.
    int16_mode: bool = True

    depth_out: Optional[PathLike] = None
    extent_out: Optional[PathLike] = None

    # Directory where per-branch intermediates are written before mosaicking.
    intermediate_dir: Optional[PathLike] = None
    cleanup_intermediates: bool = True

    def __post_init__(self) -> None:
        self.aoi_dir = Path(self.aoi_dir)
        # branches/ live in watershed-data; outputs live at the AOI root.
        self.watershed_dir = _resolve_watershed_dir(self.aoi_dir)
        self.aoi_root = aoi_root(self.watershed_dir)

    def run(self) -> FimGenerationResult:
        if not self.aoi_dir.is_dir():
            raise NotADirectoryError(self.aoi_dir)

        branch_root = self.watershed_dir / "branches"
        if not branch_root.is_dir():
            raise NotADirectoryError(branch_root)

        bids = self.branch_ids or sorted(
            p.name for p in branch_root.iterdir() if p.is_dir()
        )
        if not bids:
            raise FileNotFoundError(f"No branches under {branch_root}")

        # Normalise the forecast once so each worker doesn't re-read it.
        forecast_df = _load_forecast(self.forecast)

        # Per-branch intermediates land under the AOI's fim-outputs folder.
        tmp_dir = (
            Path(self.intermediate_dir)
            if self.intermediate_dir is not None
            else self.aoi_root / "fim-outputs" / "tmp"
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
                    branch_root / bid,
                    bid,
                    forecast_df,
                    self.min_depth_m,
                    self.drop_lakes,
                    self.int16_mode,
                    tmp_dir,
                )
                for bid in bids
            ]
        else:
            results = []
            with ProcessPoolExecutor(max_workers=self.n_workers) as pool:
                fut_to_bid = {
                    pool.submit(
                        _run_one_branch,
                        branch_root / bid,
                        bid,
                        forecast_df,
                        self.min_depth_m,
                        self.drop_lakes,
                        self.int16_mode,
                        tmp_dir,
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
                        results.append(
                            InundationResult(
                                branch_id=bid,
                                extent_path=tmp_dir / f"inundation_extent_{bid}.tif",
                                depth_path=tmp_dir / f"inundation_depth_{bid}.tif",
                                n_hydroids_wet=0,
                                n_pixels_wet=0,
                                max_depth_m=0.0,
                                skipped=True,
                            )
                        )

        results.sort(key=lambda r: r.branch_id)

        mosaic_result: Optional[MosaicResult] = None
        if self.mosaic:
            ok_bids = [r.branch_id for r in results if not r.skipped]
            if not ok_bids:
                log.warning("FimGenerator: no successful branches — skipping mosaic")
            else:
                # Default mosaic outputs land in the AOI's fim-outputs folder;
                # explicit depth_out/extent_out (CLI) override this.
                fim_out_dir = self.aoi_root / "fim-outputs"
                fim_out_dir.mkdir(parents=True, exist_ok=True)
                mosaic_result = BranchMosaic(
                    aoi_dir=self.watershed_dir,
                    branch_ids=ok_bids,
                    depth_out=self.depth_out or fim_out_dir / "inundation_depth.tif",
                    extent_out=self.extent_out
                    or fim_out_dir / "inundation_extent.tif",
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
                results.append(
                    InundationResult(
                        branch_id=bid,
                        extent_path=tmp_dir / f"inundation_extent_{bid}.tif",
                        depth_path=tmp_dir / f"inundation_depth_{bid}.tif",
                        n_hydroids_wet=0,
                        n_pixels_wet=0,
                        max_depth_m=0.0,
                        skipped=True,
                    )
                )
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
        df = src.copy()
    else:
        p = Path(src)
        if not p.is_file():
            raise FileNotFoundError(f"forecast file not found: {p}")
        df = pd.read_parquet(p) if p.suffix.lower() in (".parquet", ".pq") else pd.read_csv(p)
    # Accept a FIMserv-style 'discharge' column as an alias for 'discharge_cms'.
    if "discharge_cms" not in df.columns and "discharge" in df.columns:
        df = df.rename(columns={"discharge": "discharge_cms"})
    return df


def extract_feature_ids(
    aoi_dir: PathLike,
    out_csv: Optional[PathLike] = None,
) -> Path:
    import glob

    aoi = Path(aoi_dir)
    # branches/ live in watershed-data/; feature_id.csv lands at the AOI root.
    watershed = _resolve_watershed_dir(aoi)
    pattern = str(watershed / "branches" / "*" / "hydroTable_*.csv")
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise FileNotFoundError(
            f"No hydroTable_*.csv files under {watershed}/branches/"
        )

    frames = [pd.read_csv(p, usecols=["feature_id"]) for p in paths]
    fids = (
        pd.concat(frames, ignore_index=True)["feature_id"]
        .drop_duplicates()
        .sort_values()
        .reset_index(drop=True)
    )
    target = (
        Path(out_csv) if out_csv is not None else (aoi_root(watershed) / "feature_id.csv")
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"feature_id": fids}).to_csv(target, index=False)
    log.info(f"extract_feature_ids: {len(fids)} ids --> {target}")
    return target


def _select_discharge_csvs(
    csvs: Sequence[Path],
    *,
    date: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> list[Path]:
    """Filter discharge CSVs by the date stamp embedded in their filename.

    Names carry a ``YYYYMMDD`` (and optional ``THHMM``) token, e.g.
    ``NWM_20200520T1200.csv`` / ``NWM_20200520.csv``. ``date`` keeps names
    containing the stamp ``YYYYMMDD`` (or ``YYYYMMDDTHHMM`` when a time is
    given); ``start``/``end`` keep names whose first ``YYYYMMDD`` token is in
    the inclusive range. With no filter, all CSVs are returned.
    """
    import re

    if date is not None:
        t = pd.to_datetime(date)
        has_time = " " in str(date) or "T" in str(date)
        token = t.strftime("%Y%m%dT%H%M") if has_time else t.strftime("%Y%m%d")
        return [p for p in csvs if token in p.name]

    if start is not None and end is not None:
        lo = pd.to_datetime(start).strftime("%Y%m%d")
        hi = pd.to_datetime(end).strftime("%Y%m%d")
        day = re.compile(r"(\d{8})")
        out = []
        for p in csvs:
            m = day.search(p.name)
            if m and lo <= m.group(1) <= hi:
                out.append(p)
        return out

    return list(csvs)


@dataclass
class generateFIM:
    """Default pipeline: streamflow -> FIM-ready discharge CSVs -> rasters.

    Input modes (pick one method): retrieve NWM retrospective for a date/range,
    select a narrower window from the already-downloaded archive, run a specific
    discharge CSV, or run a selection from <AOI>/discharge-inputs/. Each
    FIM-ready CSV becomes an inundation extent raster in <AOI>/fim-outputs/
    named after the CSV; the depth raster is produced only when ``depth=True``.
    Streamflow retrieval lives in the ``fimbox.streamflow`` subpackage and is
    imported lazily so its heavy deps stay optional.
    """

    aoi_dir: PathLike
    feature_id_csv: Optional[PathLike] = None
    n_workers: int = 4
    int16_mode: bool = True
    depth: bool = False

    def __post_init__(self) -> None:
        self.aoi_dir = Path(self.aoi_dir)
        self._root = aoi_root(_resolve_watershed_dir(self.aoi_dir))
        self.feature_id_csv = (
            Path(self.feature_id_csv)
            if self.feature_id_csv is not None
            else self._root / "feature_id.csv"
        )

    def _streamflow(self):
        from ..streamflow.pipeline import StreamflowPipeline

        return StreamflowPipeline(self.aoi_dir, self.feature_id_csv)

    def from_retrospective(self, **kwargs) -> list[FimGenerationResult]:
        """Fetch NWM retrospective (date=, or start=/end=, optional sortby=)."""
        return self.generate(self._streamflow().retrospective(**kwargs))

    def from_archive(self, **kwargs) -> list[FimGenerationResult]:
        """Filter the already-downloaded archive (date=, or start=/end=, sortby=)."""
        return self.generate(self._streamflow().select(**kwargs))

    def from_csv(self, discharge_csv: PathLike) -> list[FimGenerationResult]:
        """Run a single user-supplied discharge CSV."""
        return self.generate([Path(discharge_csv)])

    def from_discharge_inputs(
        self,
        *,
        csv: Optional[PathLike] = None,
        date: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> list[FimGenerationResult]:
        """Generate FIM from the CSVs already in <AOI>/discharge-inputs/.

        Selection (in precedence order):
          * ``csv``           -> just that file
          * ``date``          -> CSVs whose name contains that day/instant stamp
                                 (``YYYYMMDD`` or ``YYYYMMDDTHHMM``)
          * ``start``+``end`` -> CSVs whose ``YYYYMMDD`` token is in [start, end]
          * none              -> every CSV in the folder
        """
        if csv is not None:
            return self.generate([Path(csv)])

        ddir = self._root / "discharge-inputs"
        all_csvs = sorted(ddir.glob("*.csv"))
        if not all_csvs:
            raise FileNotFoundError(f"No discharge CSVs in {ddir}")

        selected = _select_discharge_csvs(all_csvs, date=date, start=start, end=end)
        if not selected:
            raise FileNotFoundError(
                f"No discharge CSVs in {ddir} match "
                f"{dict(csv=csv, date=date, start=start, end=end)}"
            )
        log.info(
            "Selected %d/%d discharge CSV(s): %s",
            len(selected),
            len(all_csvs),
            ", ".join(p.name for p in selected),
        )
        return self.generate(selected)

    def generate(self, discharge_csvs: Sequence[PathLike]) -> list[FimGenerationResult]:
        """Run FimGenerator for each CSV. Outputs land in <AOI>/fim-outputs/
        named after the input CSV: an inundation extent always, plus a depth
        raster only when ``self.depth`` is True."""
        out_dir = self._root / "fim-outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        results: list[FimGenerationResult] = []
        for csv in discharge_csvs:
            csv = Path(csv)
            base = csv.stem  # output names carry the input basename
            log.info(f"--- FIM generation: {csv.name} (depth={self.depth}) ---")
            result = FimGenerator(
                aoi_dir=self.aoi_dir,
                forecast=csv,  # _load_forecast handles the discharge alias
                n_workers=self.n_workers,
                int16_mode=self.int16_mode,
                depth_out=out_dir / f"{base}_depth.tif",
                extent_out=out_dir / f"{base}_inundation.tif",
            ).run()
            # The mosaic always writes depth; drop it unless the user wants it.
            if not self.depth and result.depth_path and Path(result.depth_path).exists():
                Path(result.depth_path).unlink()
            results.append(result)
        return results


# CLI
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Two-step FIM generation: extract feature_ids, then run."
    )
    parser.add_argument("--aoi-dir", required=True)
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="Step 1 only: write <aoi_dir>/feature_id.csv and exit.",
    )
    parser.add_argument(
        "--forecast",
        default=None,
        help="Step 2: a single discharge CSV. When omitted, every CSV under "
        "<aoi_dir>/discharge-inputs/ is processed.",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--int16",
        action="store_true",
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
        # discharge-inputs/ and fim-outputs/ live at the AOI root, alongside
        # watershed-data/ (which holds the branches).
        root = aoi_root(_resolve_watershed_dir(aoi_dir))

        # Resolve which discharge CSVs to run.
        if args.forecast:
            csvs = [Path(args.forecast)]
        else:
            ddir = root / "discharge-inputs"
            if not ddir.is_dir():
                parser.error(
                    f"{ddir} not found — run --extract-only first, drop your "
                    "discharge CSVs into it, then rerun."
                )
            csvs = sorted(ddir.glob("*.csv"))
            if not csvs:
                parser.error(f"No discharge CSVs in {ddir}")

        output_dir = root / "fim-outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
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
                print(
                    f"  {result.mosaic.n_wet_pixels:,} wet pixels, "
                    f"max depth {result.mosaic.max_depth_m:.2f} m"
                )
