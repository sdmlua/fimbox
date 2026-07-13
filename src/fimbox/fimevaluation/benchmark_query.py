"""
Author: Supath Dhital
Date Created: July 2026

Benchmark FIM discovery and download via the FIMbench database.

Thin fimbox-flavoured wrapper around :func:`fimbench.query.benchFIMquery`
(https://github.com/sdmlua/fimbench). FIMbench is SDML's multi-tier,
multi-source benchmark FIM database; its catalog and assets are served as
public, anonymous S3 reads, so no AWS credentials are needed.

The wrapper adds the fimbox AOI conventions on top of the raw query:

* the candidate raster defaults to the newest inundation extent in
  ``<aoi>/fim-outputs/`` (produced by :mod:`fimbox.fimgeneration`), and
* downloaded benchmark assets (GeoTIFF + AOI GeoPackage) land in
  ``<aoi>/benchmark-data/``, where :mod:`fimbox.fimevaluation.evaluate`
  picks them up automatically.

Usage::

    import fimbox

    result = fimbox.queryBenchmarkFIM("out/my_basin", area=True, download=True)
    print(result)                      # human-readable match summary
    print(result.benchmark_rasters)    # local .tif paths ready for evaluation
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

PathLike = Union[str, Path]

log = logging.getLogger(__name__)

# fimbox AOI layout pieces this module reads / writes.
FIM_OUTPUTS_DIR = "fim-outputs"
BENCHMARK_DIR = "benchmark-data"


def _require_fimbench():
    """Import :func:`fimbench.query.benchFIMquery`, with an actionable error."""
    try:
        from fimbench.query import benchFIMquery
    except ImportError as exc:  # pragma: no cover - broken environment
        raise ImportError(
            "fimbench (a fimbox dependency) is not importable. Repair the "
            "environment with:  pip install fimbench  (or reinstall fimbox)"
        ) from exc
    return benchFIMquery


def latest_fim_extent(aoi_dir: PathLike) -> Optional[Path]:
    """Return the newest inundation-extent raster in ``<aoi>/fim-outputs/``.

    Depth rasters (``*depth*.tif``) are ignored — the extent raster is the
    natural spatial footprint to match benchmarks against. Returns ``None``
    when the folder is absent or holds no extent raster.
    """
    fim_dir = Path(aoi_dir) / FIM_OUTPUTS_DIR
    if not fim_dir.is_dir():
        return None
    tifs = [p for p in fim_dir.glob("*.tif") if "depth" not in p.stem.lower()]
    return max(tifs, key=lambda p: p.stat().st_mtime) if tifs else None


@dataclass
class BenchmarkQueryResult:
    """Outcome of one benchmark query, wrapping the raw fimbench response.

    The raw response is a plain dict (``status``, ``message``, ``matches``,
    ``printable``); the properties below expose the pieces an evaluation
    pipeline actually consumes.
    """

    response: dict[str, Any]
    out_dir: Optional[Path] = None  # where downloads were placed (if any)

    @property
    def status(self) -> Optional[str]:
        return self.response.get("status")

    @property
    def message(self) -> Optional[str]:
        return self.response.get("message")

    @property
    def matches(self) -> list[dict[str, Any]]:
        return list(self.response.get("matches") or [])

    @property
    def records(self) -> list[dict[str, Any]]:
        """Catalog record of every match (source, tier, dates, footprint...)."""
        return [m.get("record", {}) for m in self.matches]

    @property
    def downloads(self) -> list[Path]:
        """Local paths of every downloaded asset (rasters + GeoPackages)."""
        paths: list[Path] = []
        for m in self.matches:
            for p in (m.get("downloads") or {}).values():
                if p:
                    paths.append(Path(p))
        return paths

    @property
    def benchmark_rasters(self) -> list[Path]:
        """Downloaded benchmark GeoTIFFs, ready to feed an evaluator."""
        return [p for p in self.downloads if p.suffix.lower() in (".tif", ".tiff")]

    def __bool__(self) -> bool:
        return bool(self.matches)

    def __str__(self) -> str:
        return self.response.get("printable") or str(self.response)


@dataclass
class BenchmarkQuery:
    """Query the FIMbench database for benchmark FIMs covering an AOI / event.

    All filters are optional and combinable — exactly the surface of
    ``benchFIMquery``. With only ``aoi_dir`` set, the newest fimbox flood
    extent in ``<aoi>/fim-outputs/`` is used as the spatial search footprint.
    """

    # fimbox AOI root. Optional: pass raster_path/boundary_path/file_name
    # instead for a fully manual query.
    aoi_dir: Optional[PathLike] = None

    # Spatial filters — a predicted-FIM raster or an AOI boundary vector.
    # Default: newest extent raster in <aoi>/fim-outputs/ (when aoi_dir given).
    raster_path: Optional[PathLike] = None
    boundary_path: Optional[PathLike] = None

    # Catalog filters.
    huc8: Optional[str] = None  # 8-digit HUC code
    tier: Optional[str] = None  # benchmark tier, flexible ("HWM", "tier_1", ...)
    event_date: Optional[str] = None  # exact event date (YYYY-MM-DD)
    start_date: Optional[str] = None  # date-range start (YYYY-MM-DD)
    end_date: Optional[str] = None  # date-range end (YYYY-MM-DD)
    file_name: Optional[str] = None  # direct lookup by exact catalog filename

    # Behaviour toggles.
    area: bool = True  # add overlap % / km^2 to every AOI match
    download: bool = False  # fetch matched GeoTIFF + GeoPackage locally
    out_dir: Optional[PathLike] = None  # default <aoi>/benchmark-data/

    def run(self) -> BenchmarkQueryResult:
        benchFIMquery = _require_fimbench()

        aoi = Path(self.aoi_dir) if self.aoi_dir else None
        raster = Path(self.raster_path) if self.raster_path else None

        # Default spatial footprint: the AOI's newest flood extent raster.
        if (
            raster is None
            and self.boundary_path is None
            and self.file_name is None
            and aoi is not None
        ):
            raster = latest_fim_extent(aoi)
            if raster is None:
                raise FileNotFoundError(
                    f"no extent raster found under {aoi / FIM_OUTPUTS_DIR} — "
                    "generate a FIM first (fimbox.generateFIM) or pass "
                    "raster_path / boundary_path / file_name explicitly."
                )
            log.info(f"benchmark query footprint: {raster.name}")

        out_dir = Path(self.out_dir) if self.out_dir else None
        if out_dir is None and aoi is not None:
            out_dir = aoi / BENCHMARK_DIR

        kwargs: dict[str, Any] = {}
        if raster is not None:
            kwargs["raster_path"] = str(raster)
        if self.boundary_path is not None:
            kwargs["boundary_path"] = str(self.boundary_path)
        for name in ("huc8", "tier", "event_date", "start_date", "end_date",
                     "file_name"):
            value = getattr(self, name)
            if value is not None:
                kwargs[name] = value
        if self.area:
            kwargs["area"] = True
        if self.download:
            kwargs["download"] = True
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                kwargs["out_dir"] = str(out_dir)

        log.info(f"benchFIMquery({', '.join(f'{k}=...' for k in kwargs)})")
        response = benchFIMquery(**kwargs)

        result = BenchmarkQueryResult(response=response, out_dir=out_dir)
        log.info(
            f"benchmark query: {len(result.matches)} match(es)"
            + (f", {len(result.downloads)} file(s) downloaded" if self.download else "")
        )
        return result


def queryBenchmarkFIM(
    aoi_dir: Optional[PathLike] = None,
    *,
    raster_path: Optional[PathLike] = None,
    boundary_path: Optional[PathLike] = None,
    huc8: Optional[str] = None,
    tier: Optional[str] = None,
    event_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    file_name: Optional[str] = None,
    area: bool = True,
    download: bool = False,
    out_dir: Optional[PathLike] = None,
) -> BenchmarkQueryResult:
    """Query (and optionally download) benchmark FIMs from FIMbench.

    Functional wrapper around :class:`BenchmarkQuery` — see the class for the
    parameter reference. Typical calls::

        # What benchmarks cover my generated flood map?
        result = queryBenchmarkFIM("out/my_basin", area=True)

        # Match an event and pull the assets into <aoi>/benchmark-data/
        result = queryBenchmarkFIM(
            "out/my_basin", event_date="2017-08-30", download=True
        )
    """
    return BenchmarkQuery(
        aoi_dir=aoi_dir,
        raster_path=raster_path,
        boundary_path=boundary_path,
        huc8=huc8,
        tier=tier,
        event_date=event_date,
        start_date=start_date,
        end_date=end_date,
        file_name=file_name,
        area=area,
        download=download,
        out_dir=out_dir,
    ).run()
