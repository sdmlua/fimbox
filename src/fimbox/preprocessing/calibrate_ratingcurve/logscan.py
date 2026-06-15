"""
Author: Supath Dhital
Date Updated: June 2026

Scan an AOI's logs/ folder for "error" / "warning" lines and collect them
into per-AOI summary files. This is the Python port of the tail end of
inundation-mapping's ``calibrate_rating_curves.sh`` (the grep block):

  * normal run  -> scan every file under logs/
  * rerun       -> scan only logs/src_calibrations/ (the subdir a rerun
                   touches), and name the outputs ``*_calib_rerun.log``

The error summary is *appended* (the FIM pipeline may have started writing
it earlier from fim_process_huc.sh), while the warning summary is replaced.
Empty summaries are not written.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._common import PathLike, aoi_id_of, resolve_aoi_dir

log = logging.getLogger(__name__)


@dataclass
class LogScanner:
    aoi_dir: PathLike
    calibration_rerun: bool = False

    # Folder (relative to the AOI dir) that holds the per-stage log files.
    logs_subdir: str = "logs"
    # When a rerun, only this subfolder of logs/ is scanned.
    rerun_subdir: str = "src_calibrations"
    # Case-insensitive terms scanned for, each mapped to an output suffix.
    error_term: str = "error"
    warning_term: str = "warning"

    def run(self) -> dict[str, Optional[Path]]:
        aoi_dir = resolve_aoi_dir(self.aoi_dir)
        aoi_id = aoi_id_of(aoi_dir)
        logs_dir = aoi_dir / self.logs_subdir
        if not logs_dir.is_dir():
            log.info(f"SKIP log scan: no {logs_dir}")
            return {"errors": None, "warnings": None}

        # Rerun scans only the src_calibrations subdir; normal scans all logs.
        scan_root = logs_dir / self.rerun_subdir if self.calibration_rerun else logs_dir
        suffix = "_calib_rerun" if self.calibration_rerun else ""

        err_name = f"huc_{aoi_id}_errors{suffix}.log"
        warn_name = f"huc_{aoi_id}_warnings{suffix}.log"
        err_path = logs_dir / err_name
        warn_path = logs_dir / warn_name

        log.info(f"--- LogScanner: scanning {scan_root} ---")

        # Errors: append (the FIM pipeline may have begun this file earlier).
        err_hits = self._scan(scan_root, self.error_term, {err_name, warn_name})
        if err_hits:
            with err_path.open("a") as fh:
                fh.write(err_hits)
            log.info(f"Errors reported --> {err_path}")
        else:
            log.info("No errors found")

        # Warnings: replace.
        warn_hits = self._scan(scan_root, self.warning_term, {err_name, warn_name})
        if warn_hits:
            warn_path.write_text(warn_hits)
            log.info(f"Warnings reported --> {warn_path}")
        elif warn_path.is_file():
            warn_path.unlink()

        log.info("scan of log files done")
        return {
            "errors": err_path if err_hits else None,
            "warnings": warn_path if warn_hits else None,
        }

    @staticmethod
    def _scan(root: Path, term: str, exclude_names: set[str]) -> str:
        # grep -H -R -i -n <term>: every matching line tagged with
        # <path>:<lineno>:<line>, recursing under root, excluding the summary
        # files themselves so a re-scan doesn't fold prior summaries in.
        if not root.is_dir():
            return ""
        needle = term.lower()
        out_lines: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name in exclude_names or path.name.endswith(".tmp"):
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    out_lines.append(f"{path}:{lineno}:{line}")
        return "\n".join(out_lines) + ("\n" if out_lines else "")


def scan_logs(
    aoi_dir: Optional[PathLike] = None,
    *,
    huc_dir: Optional[PathLike] = None,
    calibration_rerun: bool = False,
) -> dict[str, Optional[Path]]:
    return LogScanner(
        aoi_dir=resolve_aoi_dir(aoi_dir, huc_dir),
        calibration_rerun=calibration_rerun,
    ).run()
