"""
Author: Supath Dhital
Date Updated: August 2026

One writer per AOI.

Branch processing and calibration both rewrite the per-branch SRCs and
hydroTables in place. Two runs over the same AOI interleave their writes and
leave spliced CSVs behind — files that parse cleanly for a few thousand rows and
then change shape, or keep their shape and carry another writer's numbers. The
damage is silent at write time and only surfaces much later as a tokenizing
error, so the guard belongs at the entry point of anything that writes an AOI.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)

LOCK_NAME = ".fimbox-writer.lock"


@contextmanager
def aoi_write_lock(aoi_dir: Union[str, Path], label: str = "run"):
    """Hold an exclusive OS lock on ``aoi_dir`` for the duration of the block.

    The lock lives in the AOI directory and is held by the process, so it is
    released on a crash or a kill and never needs clearing by hand. Raises
    RuntimeError rather than waiting: a second run means someone started two by
    mistake, and blocking would only hide that."""
    lock_path = Path(aoi_dir) / LOCK_NAME
    try:
        import fcntl
    except ImportError:  # non-POSIX: nothing to take, carry on unguarded
        yield
        return

    try:
        fh = open(lock_path, "w")
    except OSError as exc:  # read-only mount, missing dir — not worth failing over
        log.debug(f"AOI lock unavailable ({exc}); continuing unguarded")
        yield
        return

    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                f"Another fimbox {label} is already writing {aoi_dir}. Running "
                f"two at once corrupts the branch SRC and hydroTable files, so "
                f"this one is stopping. Wait for the other to finish; the lock "
                f"({lock_path.name}) clears itself when that process exits."
            ) from None
        try:
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
