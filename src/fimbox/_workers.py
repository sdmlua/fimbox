"""
One place that answers "how many workers can this machine actually run?".

Callers hand over whatever the user gave them — nothing, ``None``, ``0``, or a
number picked optimistically — and get back a count the box can sustain:

* ``None`` / ``0`` / negative -> auto-size to the machine (the common case)
* ``1``                       -> serial, always honoured; it's how you debug
* anything bigger             -> honoured up to what CPU + RAM allow, then clamped
* never more workers than there are branches to chew through

RAM is the binding constraint, not core count. Every branch worker is its own
process holding its own rasters and tables, so 16 workers on a 16 GB laptop buy
swap thrash instead of speed — auto-sizing is ``min(cpu, RAM / per_worker)``.
The per-worker budget differs by workload, hence the three constants below.

Advanced overrides:
- ``FIMBOX_WORKERS``        — pin the count for every pool (still capped by task count)
- ``FIMBOX_RAM_PER_WORKER`` — GB to budget per worker when auto-sizing
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Per-worker RAM budgets, by how raster-hungry the step is. Branch processing
# peaks hardest (AGREE DEM conditioning on a HUC8 at 10m), inundation loads a
# HAND + catchment raster pair, and the SRC/calibration steps are pandas tables.
RAM_PER_WORKER_BRANCH_GB = 8.0
RAM_PER_WORKER_FIM_GB = 4.0
RAM_PER_WORKER_TABLE_GB = 2.0


def system_ram_gb() -> float:
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


def _cpu_count() -> int:
    # process_cpu_count honours cgroup/affinity limits (containers, SLURM);
    # cpu_count reports the whole host and would oversubscribe a cpu-limited job.
    getter = getattr(os, "process_cpu_count", None)
    return max(1, (getter() if getter else os.cpu_count()) or 1)


def max_supported_workers(ram_per_worker_gb: float = RAM_PER_WORKER_FIM_GB) -> int:
    """Most workers this machine can feed without going to swap.

    Floored at 2 so a modest laptop still gets some parallelism, then capped at
    the core count so we never oversubscribe CPUs.
    """
    per_worker = float(os.environ.get("FIMBOX_RAM_PER_WORKER", ram_per_worker_gb))
    by_ram = max(2, int(system_ram_gb() // max(per_worker, 0.5)))
    return min(_cpu_count(), by_ram)


def resolve_workers(
    requested: Optional[int],
    *,
    n_tasks: Optional[int] = None,
    ram_per_worker_gb: float = RAM_PER_WORKER_FIM_GB,
    label: str = "workers",
) -> int:
    """Turn a user-supplied worker count into one this machine can honour.

    ``1`` is passed straight through — an explicit request for serial is a
    debugging tool, not a number to second-guess. Everything else is bounded by
    :func:`max_supported_workers` and by ``n_tasks``, since idle processes still
    cost a spawn and a full package re-import.
    """
    env = os.environ.get("FIMBOX_WORKERS")
    if env:
        try:
            pinned = int(env)
            if pinned > 0:
                requested = pinned
        except ValueError:
            log.warning("FIMBOX_WORKERS=%r is not an int; ignoring", env)

    if requested == 1:
        return 1

    ceiling = max_supported_workers(ram_per_worker_gb)

    if requested is None or requested <= 0:
        n = ceiling
        why = f"auto-sized (cpu={_cpu_count()}, ram={system_ram_gb():.0f} GB)"
    elif requested > ceiling:
        n = ceiling
        why = f"requested {requested}, clamped to what this machine supports"
    else:
        n = requested
        why = ""  # honouring the given number needs no explanation

    if n_tasks is not None:
        n = max(1, min(n, n_tasks))

    log.info("%s: %d worker(s)%s", label, n, f" — {why}" if why else "")
    return n
