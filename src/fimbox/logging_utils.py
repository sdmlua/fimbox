"""
Author: Supath Dhital
Date Updated: June 2026


Shared logging setup
All modules use `log = logging.getLogger(__name__)` (so every logger sits
under the `fimbox.*` namespace). When a pipeline run starts, the orchestrator
calls `attach_case_log(case_dir)` which adds two handlers to the root
`fimbox` logger:

    1. a FileHandler writing to <AOI_root>/processing.log
    2. a StreamHandler writing to stdout

The log always lands at the AOI root. Downstream stages (branch processing,
FIM generation, calibration) operate on the ``watershed-data/`` subfolder and
pass it as their ``aoi_dir``; ``attach_case_log`` detects that and hops up to
the parent so every stage writes to the single ``<AOI_root>/processing.log``.

Format (terminal + log file):
    HH:MM:SS [LEVEL] message

Conventions used across the codebase (enforce these in new code):
    - Use `--- Section Name ---` for top-level section banners.
    - For a written artifact, say `<thing> --> <filename>`.
    - For a no-op skip, say `SKIP (exists): <filename>`.
    - For a missing/empty service response, log at WARNING, not INFO.
    - Never include step numbers like "Step 1/5" — section banners are enough.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

_ROOT_NAME = "fimbox"
_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%H:%M:%S"

# Name of the watershed-data subfolder that holds all input data + branches.
WATERSHED_DIR_NAME = "watershed-data"

# Single fallback output folder used by every entry point when the caller does
# not pass an output directory, so outputs never scatter across CWD-relative
# folders. Resolved to an absolute path for predictability on macOS + Windows.
DEFAULT_OUTPUT_DIR_NAME = "out"


def default_output_dir() -> Path:
    """Absolute fallback output directory (``<cwd>/out``) for no-arg callers."""
    return (Path.cwd() / DEFAULT_OUTPUT_DIR_NAME).resolve()


def aoi_root(case_dir: Union[str, Path]) -> Path:
    """Resolve the AOI root from any directory a stage operates on.

    Preprocessing writes input data + ``branches/`` into
    ``<AOI_root>/watershed-data/`` and downstream stages take that subfolder
    (or something inside it, e.g. a branch dir) as their working dir. The
    single combined log, ``feature_id.csv``, ``discharge-inputs/`` and
    ``fim-outputs/`` all live at the AOI root. Return the parent of the first
    ``watershed-data`` component found while walking up from ``case_dir``;
    if there is none, return ``case_dir`` unchanged (legacy flat layouts and
    direct AOI-root callers still work).
    """
    p = Path(case_dir)
    if p.name == WATERSHED_DIR_NAME:
        return p.parent
    # A path nested under watershed-data (e.g. .../watershed-data/branches/0):
    # hop up to the AOI root that holds watershed-data.
    for parent in p.parents:
        if parent.name == WATERSHED_DIR_NAME:
            return parent.parent
    return p


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a logger under the `fimbox` namespace.

    Equivalent to `logging.getLogger(name)` but documents intent: every module
    that wants to log should call this (or `logging.getLogger(__name__)` —
    both end up propagating to the `fimbox` root once handlers are attached).
    """
    return logging.getLogger(name or _ROOT_NAME)


def attach_case_log(
    case_dir: Union[str, Path],
    *,
    filename: str = "processing.log",
    level: int = logging.INFO,
    stream: bool = True,
) -> logging.Logger:
    """Attach a per-case file handler (and optional stream handler) to the
    `fimbox` root logger.

    The log always lands at the AOI root (see :func:`aoi_root`): when
    ``case_dir`` is a ``watershed-data`` folder the handler writes one level up
    so every stage shares the same ``<AOI_root>/processing.log``.

    Safe to call multiple times: handlers are tagged so duplicate attachments
    for the same log file are skipped.
    """
    log_dir = aoi_root(case_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / filename

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(level)
    root.propagate = False

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    existing_files = {getattr(h, "_fimbox_logfile", None) for h in root.handlers}
    if str(log_path) not in existing_files:
        fh = logging.FileHandler(log_path, mode="a")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        fh._fimbox_logfile = str(log_path)  # type: ignore[attr-defined]
        root.addHandler(fh)

    if stream and not any(getattr(h, "_fimbox_stream", False) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(level)
        sh._fimbox_stream = True  # type: ignore[attr-defined]
        root.addHandler(sh)

    return root


def configure_cli_logging(level: int = logging.INFO) -> logging.Logger:
    """Attach only a stream handler — used by module CLI runners that run
    standalone outside the case-dir pipeline."""
    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(level)
    root.propagate = False
    if not any(getattr(h, "_fimbox_stream", False) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        sh.setLevel(level)
        sh._fimbox_stream = True  # type: ignore[attr-defined]
        root.addHandler(sh)
    return root
