"""
Retrieve NextGen-in-a-Box discharge from the public CIROH community NextGen
DataStream bucket for a set of hydrofabric ``feature_id``s.

Output layout on the bucket::

    outputs/<model>/v2.2_hydrofabric/ngen.<YYYYMMDD>/<forecast>/<cycle>/VPU_<id>/
        ngen-run/outputs/troute/troute_output_<stamp>.parquet   (newer runs)
        ngen-run.tar.gz  ->  ngen-run/outputs/troute/*.nc         (older runs)

t-route output is the routed streamflow that drives FIM. It is keyed by
``feature_id`` (the integer of the ``wb-<n>`` flowpath id) with an hourly
``flow`` series in cubic metres per second. This module locates the run
(defaulting to model ``cfe_nom``, ``short_range``, and the latest available
date/cycle that has the requested VPU), reads the ``flow`` series for the
requested feature ids, and emits FIM-ready CSVs (``feature_id, discharge_cms``)
into ``<AOI>/discharge-inputs/``.
"""

from __future__ import annotations

import io
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from . import _common as C

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

DEFAULT_MODEL = "cfe_nom"
DEFAULT_FORECAST = "short_range"
_HF_SEG = f"{C.HF_VERSION}_hydrofabric"


@dataclass
class DischargeRun:
    """A located ngen datastream run."""

    model: str
    forecast: str
    date: str  # YYYYMMDD
    cycle: str  # e.g. "00"
    vpu: str

    @property
    def prefix(self) -> str:
        return (
            f"{C.OUTPUTS_PREFIX}/{self.model}/{_HF_SEG}/ngen.{self.date}/"
            f"{self.forecast}/{self.cycle}/{self.vpu}"
        )

    def __str__(self) -> str:
        return f"{self.model}/{self.forecast}/ngen.{self.date}/{self.cycle}/{self.vpu}"


class NextGenDatastream:
    """Locate and read NextGen datastream discharge for one VPU.

    Parameters
    ----------
    vpu : str
        Hydrofabric VPU id, e.g. ``"VPU_03N"``.
    model : str
        ngen model output set. Default ``"cfe_nom"``.
    forecast : str
        Forecast product: ``"short_range"`` (default), ``"medium_range"``,
        ``"analysis_assim_extend"``, ...
    date : str, optional
        Run date ``YYYYMMDD`` (or ``YYYY-MM-DD``). Default: latest available.
    cycle : str, optional
        Cycle hour, e.g. ``"06"``. Default: latest cycle that has ``vpu``.
    """

    def __init__(
        self,
        vpu: str,
        *,
        model: str = DEFAULT_MODEL,
        forecast: str = DEFAULT_FORECAST,
        date: Optional[str] = None,
        cycle: Optional[str] = None,
    ):
        self.vpu = vpu
        self.model = model
        self.forecast = forecast
        self._fs = C.s3()
        self.run = self._locate(date, cycle)

    def _model_root(self) -> str:
        return f"{C.OUTPUTS_PREFIX}/{self.model}/{_HF_SEG}"

    def _ls_names(self, prefix: str) -> list[str]:
        try:
            return [p.split("/")[-1] for p in self._fs.ls(prefix)]
        except FileNotFoundError:
            return []

    def _available_dates(self) -> list[str]:
        dates = [
            n.split("ngen.")[-1]
            for n in self._ls_names(self._model_root())
            if n.startswith("ngen.")
        ]
        return sorted(dates, reverse=True)

    def _locate(self, date: Optional[str], cycle: Optional[str]) -> DischargeRun:
        date = date.replace("-", "") if date else None

        def cycle_has_vpu(d: str, c: str) -> bool:
            base = f"{self._model_root()}/ngen.{d}/{self.forecast}/{c}/{self.vpu}"
            return self._fs.exists(base)

        if date and cycle:
            if not cycle_has_vpu(date, cycle):
                raise FileNotFoundError(
                    f"No {self.vpu} output for {self.model}/{self.forecast} "
                    f"ngen.{date} cycle {cycle}."
                )
            return DischargeRun(self.model, self.forecast, date, cycle, self.vpu)

        candidate_dates = [date] if date else self._available_dates()
        if not candidate_dates:
            raise FileNotFoundError(
                f"No dated runs under {self._model_root()} (model={self.model})."
            )
        for d in candidate_dates:
            cycles = self._ls_names(f"{self._model_root()}/ngen.{d}/{self.forecast}")
            wanted = [cycle] if cycle else sorted(cycles, reverse=True)
            for c in wanted:
                if c in cycles and cycle_has_vpu(d, c):
                    log.info(
                        "Located discharge run: %s/%s ngen.%s cycle %s %s",
                        self.model,
                        self.forecast,
                        d,
                        c,
                        self.vpu,
                    )
                    return DischargeRun(self.model, self.forecast, d, c, self.vpu)
        raise FileNotFoundError(
            f"No {self.forecast} run with {self.vpu} found for model "
            f"{self.model} (searched {len(candidate_dates)} date(s))."
        )

    def _troute_dir(self) -> str:
        return f"{self.run.prefix}/ngen-run/outputs/troute"

    def read_discharge(self, feature_ids: Optional[list[int]] = None) -> pd.DataFrame:
        """Return a long DataFrame ``[feature_id, time, flow]`` (flow in cms),
        filtered to ``feature_ids`` when given.

        Handles both the extracted parquet/netCDF layout (newer runs) and the
        ``ngen-run.tar.gz`` archive layout (older runs).
        """
        want = set(int(f) for f in feature_ids) if feature_ids is not None else None
        troute_dir = self._troute_dir()

        if self._fs.exists(troute_dir):
            files = self._fs.ls(troute_dir)
            df = self._read_extracted(files)
        elif self._fs.exists(f"{self.run.prefix}/ngen-run.tar.gz"):
            df = self._read_tarball(f"{self.run.prefix}/ngen-run.tar.gz")
        else:
            raise FileNotFoundError(
                f"No troute output (extracted or tarball) under {self.run.prefix}"
            )

        if want is not None:
            df = df[df["feature_id"].isin(want)].copy()
        df = df.sort_values(["feature_id", "time"]).reset_index(drop=True)
        log.info(
            "Discharge: %d reaches x %d timesteps from %s",
            df["feature_id"].nunique(),
            df["time"].nunique(),
            self.run,
        )
        return df

    def _read_extracted(self, files: list[str]) -> pd.DataFrame:
        parquet = [f for f in files if f.endswith(".parquet")]
        netcdf = [f for f in files if f.endswith(".nc")]
        if parquet:
            frames = [pd.read_parquet(self._fs.open(f)) for f in parquet]
            return self._normalize(pd.concat(frames, ignore_index=True))
        if netcdf:
            frames = [self._read_netcdf(self._fs.open(f)) for f in netcdf]
            return self._normalize(pd.concat(frames, ignore_index=True))
        raise FileNotFoundError("troute dir has no .parquet or .nc output")

    def _read_tarball(self, key: str) -> pd.DataFrame:
        log.info("Downloading archive %s ...", key.split("/")[-1])
        raw = self._fs.cat_file(key)
        frames = []
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            members = [
                m
                for m in tf.getmembers()
                if "outputs/troute" in m.name and m.name.endswith((".nc", ".parquet"))
            ]
            for m in members:
                fh = tf.extractfile(m)
                buf = io.BytesIO(fh.read())
                if m.name.endswith(".parquet"):
                    frames.append(pd.read_parquet(buf))
                else:
                    frames.append(self._read_netcdf(buf))
        if not frames:
            raise FileNotFoundError(f"{key} has no troute output members")
        return self._normalize(pd.concat(frames, ignore_index=True))

    @staticmethod
    def _read_netcdf(fileobj) -> pd.DataFrame:
        xr = C.require("xarray")
        # netCDF4 can't read a file-like object; materialise to a temp file
        import tempfile

        data = fileobj.read() if hasattr(fileobj, "read") else fileobj
        with tempfile.NamedTemporaryFile(suffix=".nc") as tmp:
            tmp.write(data)
            tmp.flush()
            with xr.open_dataset(tmp.name) as ds:
                keep = [v for v in ("flow", "velocity", "depth") if v in ds]
                df = ds[keep].to_dataframe().reset_index()
        return df

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        """Coerce whatever troute layout we read into ``[feature_id, time, flow]``."""
        cols = {c.lower(): c for c in df.columns}
        fid = cols.get("feature_id") or cols.get("featureid")
        flow = cols.get("flow") or cols.get("streamflow") or cols.get("q")
        time = cols.get("time") or cols.get("current_time")
        if fid is None or flow is None:
            raise ValueError(f"Unrecognized troute columns: {list(df.columns)}")
        # keep flowpaths (wb), drop nexus rows when a 'type' column is present
        if "type" in cols:
            df = df[df[cols["type"]].astype(str).str.lower() == "wb"]
        out = df.rename(columns={fid: "feature_id", flow: "flow"})
        if time is not None:
            out = out.rename(columns={time: "time"})
        else:
            out["time"] = pd.NaT
        out["feature_id"] = out["feature_id"].astype("int64")
        out["time"] = pd.to_datetime(out["time"])
        return out[["feature_id", "time", "flow"]]

    def to_fim_inputs(
        self,
        aoi_dir: PathLike,
        feature_ids: list[int],
        *,
        sortby: Optional[str] = "maximum",
        at_time: Optional[str] = None,
    ) -> list[Path]:
        """Write FIM-ready discharge CSV(s) to ``<AOI>/discharge-inputs/``.

        ``sortby`` in {``"maximum"``, ``"minimum"``, ``"mean"``} collapses the
        forecast horizon into one CSV of that statistic per reach (default
        ``"maximum"`` — the peak, most relevant for flood extent). ``at_time``
        selects a single timestamp instead. ``sortby=None`` and no ``at_time``
        writes one CSV per timestep.
        """
        C.attach_log(aoi_dir)
        df = self.read_discharge(feature_ids)
        if df.empty:
            log.warning("No discharge matched the requested feature_ids.")
            return []
        out_dir = C.discharge_inputs_dir(aoi_dir)
        tag = (
            f"nextgen_{self.model}_{self.run.forecast}_{self.run.date}{self.run.cycle}"
        )
        written: list[Path] = []

        if at_time is not None:
            ts = pd.to_datetime(at_time)
            sub = df[df["time"] == ts]
            if sub.empty:
                raise ValueError(
                    f"No timestep at {at_time}; available: "
                    f"{df['time'].min()}..{df['time'].max()}"
                )
            written.append(
                self._write_csv(sub, out_dir / f"{tag}_{C.DISCHARGE_COL}.csv")
            )
        elif sortby:
            agg = {"maximum": "max", "minimum": "min", "mean": "mean"}
            if sortby not in agg:
                raise ValueError("sortby must be maximum, minimum, mean, or None")
            g = df.groupby("feature_id")["flow"].agg(agg[sortby]).reset_index()
            written.append(self._write_csv(g, out_dir / f"{tag}_{sortby}.csv"))
        else:
            for ts, sub in df.groupby("time"):
                stamp = pd.to_datetime(ts).strftime("%Y%m%dT%H%M")
                written.append(self._write_csv(sub, out_dir / f"{tag}_{stamp}.csv"))
        return written

    @staticmethod
    def _write_csv(df: pd.DataFrame, path: Path) -> Path:
        out = df.rename(columns={"flow": C.DISCHARGE_COL})[
            ["feature_id", C.DISCHARGE_COL]
        ].copy()
        out["feature_id"] = out["feature_id"].astype("int64")
        out.to_csv(path, index=False)
        log.info("FIM-ready discharge (%d reaches) --> %s", len(out), path.name)
        return path
