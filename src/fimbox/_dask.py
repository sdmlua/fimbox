"""
Lazy Dask LocalCluster singleton sized to the running machine.

fimbox uses a single ``distributed.Client`` per Python process so the
branch loop and the chunked raster operations all hit the same worker
pool. The cluster is created the first time ``get_client()`` is called
and reused thereafter; tests / scripts that want a hand-tuned cluster
can pass their own client into ``set_client()`` before any consumer
asks for one.

Worker sizing strategy
----------------------
Each branch worker loads big rasters into NumPy arrays — easily 1-4 GB
per worker on a HUC8 with 10m DEM. Defaulting to ``cpu_count()``
workers then ``memory_limit="auto"`` (which splits total RAM evenly)
left each worker with too little headroom and the Dask memory governor
started pausing/killing workers mid-task.

The new defaults:

1. ``n_workers`` is the minimum of CPU count and (free RAM GB / 6),
   floored at 2. On a 32 GB / 8-core box that gives 5 workers with
   ~6 GB each — enough for a HUC8 branch with margin.
2. Dask's "pause-at-80% / terminate-at-95%" governor is disabled.
   Branches run with stable RSS once the rasters are loaded; the
   transient peaks were causing Dask to pause workers, which made the
   scheduler reassign tasks and pile *additional* memory pressure on
   the next victim. We let workers use their full slice and rely on
   process isolation to keep one branch's RAM out of another's.
3. ``allowed_failures=0`` on the cluster so a OOM/KilledWorker doesn't
   silently retry the same branch on 3 more siblings (which is what
   was making the failure cascade you saw in the logs).

Env-var overrides for advanced users:
- ``FIMBOX_DASK_WORKERS``        — explicit worker count
- ``FIMBOX_DASK_MEM_PER_WORKER`` — explicit per-worker memory cap
- ``FIMBOX_DASK_RAM_PER_BRANCH`` — GB to budget per worker for the
  auto-sizer (default 6)
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)

_lock = threading.Lock()
_client = None  # type: Optional["distributed.Client"]
_cluster = None  # type: Optional["distributed.LocalCluster"]

# Default RAM headroom assumed per branch worker. Tuned for HUC8-sized
# AOIs at 10m where the AGREE DEM step peaks around 5-7 GB transient.
# 8 GB per branch keeps that step well clear of the worker cap on a
# 64 GB machine (8 workers) and prevents the KilledWorker cascade we
# saw with a 6 GB budget. Override via FIMBOX_DASK_RAM_PER_BRANCH.
_DEFAULT_RAM_PER_BRANCH_GB = 8.0


def _system_ram_gb() -> float:
    """Best-effort total RAM in GB. Returns 8.0 if it can't be detected."""
    try:
        import psutil  # type: ignore

        return psutil.virtual_memory().total / (1024**3)
    except Exception:
        pass
    # POSIX fallback (Linux + macOS): sysconf
    try:
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024**3)
    except (ValueError, AttributeError, OSError):
        return 8.0


def _resolve_n_workers() -> int:
    """Pick worker count that fits comfortably in RAM.

    Explicit env override wins. Otherwise: min(cpu_count, RAM_GB /
    RAM_PER_BRANCH_GB), floored at 2 so very small machines still get
    some parallelism, capped at cpu_count so we don't oversubscribe
    cores.
    """
    env = os.environ.get("FIMBOX_DASK_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            log.warning("FIMBOX_DASK_WORKERS=%r is not an int; auto-sizing", env)

    cpu = max(1, os.cpu_count() or 1)
    ram_gb = _system_ram_gb()

    per_branch = float(
        os.environ.get("FIMBOX_DASK_RAM_PER_BRANCH", _DEFAULT_RAM_PER_BRANCH_GB)
    )
    by_ram = max(2, int(ram_gb // per_branch))
    n = min(cpu, by_ram)
    log.info(
        "Dask worker sizing: cpu_count=%d, system_ram=%.1f GB, "
        "ram_per_branch=%.1f GB => n_workers=%d",
        cpu,
        ram_gb,
        per_branch,
        n,
    )
    return n


def _resolve_memory_limit():
    """Per-worker memory cap. 'auto' lets distributed divide system RAM
    evenly among workers (which, combined with the conservative
    n_workers above, gives each worker its full RAM budget)."""
    env = os.environ.get("FIMBOX_DASK_MEM_PER_WORKER")
    return env if env else "auto"


def _silence_memory_governor() -> None:
    """Disable Dask's worker memory pause/terminate behaviour.

    Branches blow past the default 80% pause / 95% terminate thresholds
    during legitimate raster loads. The pause triggers task
    reassignment, which makes the *next* worker hit the same threshold,
    cascading into the KilledWorker storm seen in real runs. Setting
    these to ``False`` keeps the OS as the only authority on memory;
    process isolation prevents one branch from eating another's RAM.
    """
    try:
        import dask

        dask.config.set(
            {
                "distributed.worker.memory.target": False,
                "distributed.worker.memory.spill": False,
                "distributed.worker.memory.pause": False,
                "distributed.worker.memory.terminate": False,
                # When a worker DOES die unexpectedly, don't retry the task
                # on 3 more workers and OOM each in turn — fail fast so the
                # branch loop records it as failed and moves on.
                "distributed.scheduler.allowed-failures": 0,
            }
        )
    except Exception as exc:
        log.warning("Could not adjust dask config: %s", exc)


def get_client(n_workers: Optional[int] = None):
    """Return the process-wide Dask client, creating it lazily.

    Pass ``n_workers`` to override the env/CPU default on first call
    only — once the cluster exists, later calls return the same client
    regardless of the argument.
    """
    global _client, _cluster

    if _client is not None:
        return _client

    with _lock:
        if _client is not None:
            return _client

        _silence_memory_governor()

        from distributed import Client, LocalCluster

        n = n_workers if n_workers else _resolve_n_workers()
        mem = _resolve_memory_limit()

        log.info(
            "Starting Dask LocalCluster: n_workers=%d, threads_per_worker=1, "
            "memory_limit=%s, memory governor=disabled",
            n,
            mem,
        )
        _cluster = LocalCluster(
            n_workers=n,
            threads_per_worker=1,
            memory_limit=mem,
            processes=True,
            dashboard_address=":0",
        )
        _client = Client(_cluster)
        log.info("Dask dashboard: %s", _client.dashboard_link)
        atexit.register(shutdown)
    return _client


def set_client(client) -> None:
    """Inject a pre-built client (tests, notebooks). Must be called before
    any consumer asks for one — does nothing if a cluster has already
    been spun up internally."""
    global _client
    with _lock:
        if _client is None:
            _client = client


def shutdown() -> None:
    """Tear down the cluster. Safe to call multiple times."""
    global _client, _cluster
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
            _client = None
        if _cluster is not None:
            try:
                _cluster.close()
            except Exception:
                pass
            _cluster = None
