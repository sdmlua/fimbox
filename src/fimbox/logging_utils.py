"""
Shared logging setup
All modules use `log = logging.getLogger(__name__)` (so every logger sits
under the `fimbox.*` namespace). When a pipeline run starts, the orchestrator
calls `attach_case_log(case_dir)` which adds two handlers to the root
`fimbox` logger:

    1. a FileHandler writing to <case_dir>/preprocess.log
    2. a StreamHandler writing to stdout

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
    filename: str = "preprocess.log",
    level: int = logging.INFO,
    stream: bool = True,
) -> logging.Logger:
    """Attach a per-case file handler (and optional stream handler) to the
    `fimbox` root logger.

    Safe to call multiple times: handlers are tagged so duplicate attachments
    for the same case_dir are skipped.
    """
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    log_path = case_dir / filename

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
