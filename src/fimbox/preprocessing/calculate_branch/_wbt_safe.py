"""
Author: Supath Dhital

Concurrency-safe WhiteboxTools invocation.

WhiteboxTools' Python wrapper (`WhiteboxTools.run_tool`) is unsafe to call
from branches that run concurrently:

  1. ``run_tool`` does ``os.chdir(self.exe_path)`` for the duration of the
     subprocess and ``os.chdir`` back in a finally-block. ``os.chdir`` is
     *process-global*, so when two WBT tools overlap inside one process the
     working directory of the loser gets corrupted mid-run.
  2. ``run_tool`` returns 0 ("ok") as soon as the child's stdout closes — it
     never inspects the subprocess exit code. A run that produced no output
     file (transient FS contention, a half-downloaded shared binary, a
     non-zero WBT exit) still reports success, so callers log "written" for a
     file that isn't on disk. That is exactly the intermittent
     ``flowdir_d8_burned_filled_<bid>.tif: No such file or directory`` that
     struck one arbitrary branch per multi-branch run.
  3. ``WhiteboxTools.__init__`` calls ``download_wbt()`` and reads/writes a
     shared ``settings.json`` in the package directory every time it is
     constructed — another cross-process race on the shared binary.

This module bypasses ``run_tool`` entirely. It invokes the WBT binary by its
absolute path via ``subprocess`` (so ``os.chdir`` never runs), serialises the
one-time binary download behind a lock, and — critically — verifies the
expected output file exists after the run, retrying once before raising. The
pattern mirrors ``process_bridgedem/bridge_lidar_raster.py``, which already
solved this for the LiDAR IDW step.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from threading import Lock
from typing import Optional, Sequence

log = logging.getLogger(__name__)

_WBT_LOCK = Lock()
_WBT_SRC_READY = False
_WBT_REAL_DIR: str = ""  # the real (shared, read-only) WBT install directory
_WBT_EXE_NAME: str = ""  # binary file name, e.g. "whitebox_tools"

# Each WBT worker keeps its OWN private home (see _private_home). Thread-local
# so concurrent threads in one process never share a home; a forked process
# starts with an empty thread-local and builds its own. Built homes are tracked
# for atexit cleanup.
_WBT_TLS = threading.local()
_WBT_HOMES: list[str] = []

# Files the WBT binary CREATES/REWRITES at runtime inside its own directory.
# Each home must own its own copies (so concurrent runs don't corrupt a shared
# one); everything else is read-only and is shared via symlink.
_WBT_PER_HOME_FILES = ("settings.json", "config_file.json", "__pycache__")


def _ensure_wbt_source(wbt_path: Optional[str] = None) -> None:
    """Download (once) and locate the real WBT install dir + binary name.

    Serialised behind a process-wide lock so concurrent threads don't race to
    unzip the shared binary. This only *reads* the shared dir afterwards;
    nothing mutates it, so it is safe to share read-only across all homes.
    """
    global _WBT_SRC_READY, _WBT_REAL_DIR, _WBT_EXE_NAME
    if _WBT_SRC_READY:
        return
    with _WBT_LOCK:
        if _WBT_SRC_READY:
            return
        import whitebox as _wbt_mod

        inst = _wbt_mod.WhiteboxTools()  # triggers download_wbt() exactly once
        inst.set_verbose_mode(False)
        wbt_dir = wbt_path or os.environ.get("WBT_PATH")
        if wbt_dir:
            inst.set_whitebox_dir(wbt_dir)
        _WBT_REAL_DIR = inst.exe_path
        _WBT_EXE_NAME = inst.exe_name
        _WBT_SRC_READY = True


def _private_home(wbt_path: Optional[str] = None) -> tuple[str, str]:
    """Return ``(home_dir, exe_path)`` for a PRIVATE WBT home owned by the
    calling thread, building it on first use.

    Why a private home (the core scale fix)
    ---------------------------------------
    The WBT binary reads and *rewrites* ``config_file.json`` / ``settings.json``
    in its own executable directory on every run. When many branches execute
    WBT concurrently — across Dask worker *processes* AND across threads within
    a process — those writes interleave and corrupt the shared JSON, so the
    binary panics for *every* concurrent caller with::

        whitebox-common/src/configs/mod.rs: Failed to parse config_file.json
        file.: Error("trailing characters", ...)

    Copying the ~170 MB install dir per worker is wasteful, so each home
    *symlinks* the large read-only pieces (the binary, ``plugins/``, ``WBT/``,
    ``img/`` …) and owns only its small mutable JSONs. No mutable file is ever
    shared, so the corruption race cannot occur regardless of how many branches
    run at once. Verified clean at 200 jobs / 16-way thread concurrency and
    across the live Dask branch loop. The home is built once per thread and
    reused for every later WBT call on that thread.
    """
    home = getattr(_WBT_TLS, "home", None)
    if home is not None:
        return home, getattr(_WBT_TLS, "exe")

    _ensure_wbt_source(wbt_path)

    import atexit
    import shutil

    home = tempfile.mkdtemp(prefix="fimbox_wbt_home_")
    for entry in os.listdir(_WBT_REAL_DIR):
        if entry in _WBT_PER_HOME_FILES:
            continue  # the binary recreates these per-home
        src = os.path.join(_WBT_REAL_DIR, entry)
        dst = os.path.join(home, entry)
        try:
            os.symlink(src, dst)
        except OSError as exc:
            # Fall back to a copy if symlinks aren't permitted (rare).
            log.debug("WBT symlink %s failed (%s); copying", entry, exc)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)

    exe = os.path.join(home, _WBT_EXE_NAME)
    _WBT_TLS.home = home
    _WBT_TLS.exe = exe
    with _WBT_LOCK:
        _WBT_HOMES.append(home)
        if len(_WBT_HOMES) == 1:
            atexit.register(_cleanup_homes)
    return home, exe


def _cleanup_homes() -> None:
    import shutil

    for h in _WBT_HOMES:
        shutil.rmtree(h, ignore_errors=True)


def _ensure_wbt_ready(wbt_path: Optional[str] = None) -> str:
    """Back-compat shim: return this thread's private WBT binary path."""
    _, exe = _private_home(wbt_path)
    return exe


def _resolve_max_procs(max_procs: Optional[int]) -> int:
    """Threads each WBT subprocess may use internally. Defaults to 1.

    Branch-level parallelism comes from the Dask worker pool running many
    branches at once. If each branch's WBT call ALSO fans out across every
    core (WBT's default ``max_procs=-1``), the machine is wildly
    oversubscribed and WBT's internal parallel routines race — FillDepressions
    panics at its output stage ("Error unwrapping 'output'", exit 101) and
    D8Pointer/others intermittently produce nothing. Pinning each WBT
    invocation to a single thread removes that race entirely with no
    measurable slowdown on per-branch DEMs (they are small). Advanced users on
    a single-branch run can raise it via ``FIMBOX_WBT_MAX_PROCS``.
    """
    if max_procs is not None:
        return max_procs
    env = os.environ.get("FIMBOX_WBT_MAX_PROCS")
    if env:
        try:
            return int(env)
        except ValueError:
            log.warning("FIMBOX_WBT_MAX_PROCS=%r is not an int; using 1", env)
    return 1


def run_wbt_tool(
    tool_name: str,
    args: Sequence[str],
    out_path: Path,
    *,
    wbt_path: Optional[str] = None,
    timeout: Optional[float] = 600.0,
    retries: int = 2,
    max_procs: Optional[int] = None,
) -> Path:
    """Run one WBT tool by absolute binary path, single-threaded, and verify
    its output exists.

    This is the single choke point every branch-pipeline WBT call must go
    through. It hardens four independent failure modes seen under the parallel
    Dask branch loop / hundreds-of-branches scale (see module docstring and
    _ensure_wbt_ready):

      * private per-process WBT home          → no shared config_file.json /
                                                 settings.json corruption (the
                                                 dominant failure at scale)
      * absolute-path binary, no ``os.chdir`` → no cross-branch cwd corruption
      * verify output exists, else retry      → no silent "wrote nothing"
      * ``--max_procs=1``                      → no WBT-internal parallel race

    Parameters
    ----------
    tool_name : WBT tool name passed to ``--run`` (e.g. "D8Pointer").
    args      : tool flags, already formatted (e.g. ["--dem=a.tif",
                "--output=b.tif"]). Always pass ABSOLUTE paths — the binary
                runs with its cwd at the private WBT home, so relative names
                would not resolve to the branch directory.
    out_path  : the file the tool is expected to produce. The call is treated
                as failed (and retried, then raised) if this file is absent or
                empty after the subprocess returns — closing the silent
                "returned 0 but wrote nothing" gap in WBT's own wrapper.
    timeout   : per-attempt subprocess timeout in seconds.
    retries   : extra attempts after the first if the output is missing
                (escalating backoff between attempts).
    max_procs : WBT internal thread cap (default 1; see _resolve_max_procs).

    Returns the output path on success; raises RuntimeError otherwise.
    """
    home, exe = _private_home(wbt_path)
    out_path = Path(out_path)
    procs = _resolve_max_procs(max_procs)
    cmd = [
        exe,
        f"--run={tool_name}",
        *args,
        # Pin WBT's working directory to the output's parent so the binary
        # never falls back to a working_directory left in its home settings.json.
        # Absolute --output already wins; this is belt-and-suspenders.
        f"--wd={out_path.parent.resolve()}",
        f"--max_procs={procs}",
        "-v=false",
        "--compress_rasters=False",
    ]

    last_err = ""
    for attempt in range(retries + 1):
        # A stale/partial output from a prior attempt would make the existence
        # check pass spuriously — clear it first.
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass

        # Run with cwd at this process's PRIVATE WBT home so the binary finds
        # its (symlinked) plugins and writes its own config_file.json there,
        # never a sibling's. The binary is addressed by absolute path.
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=home,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last_err = f"WBT {tool_name} timed out after {timeout}s"
            log.warning("%s (attempt %d/%d)", last_err, attempt + 1, retries + 1)
            continue

        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path

        tail = (proc.stdout or b"").decode("utf-8", "replace").strip()[-500:]
        last_err = (
            f"WBT {tool_name} returned {proc.returncode} but produced no "
            f"output at {out_path.name}. WBT said: {tail or '(no output)'}"
        )
        log.warning("%s (attempt %d/%d)", last_err, attempt + 1, retries + 1)
        # Escalating backoff lets any transient contention clear before retry.
        # The private-home + single-thread fixes make failures rare; the retry
        # is a final safety net for unforeseen transient FS hiccups at scale.
        time.sleep(0.2 * (attempt + 1))

    raise RuntimeError(last_err)
