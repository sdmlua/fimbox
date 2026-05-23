"""
Author: Supath Dhital
Date Updated: May 2026

Run the complete branch loop and AOI-level cleanup for a single AOI.

Inputs
------
cfg (AOIProcessingConfig)
    Same config the lower-level ``process_branches`` consumes — branch list
    path, USGS gage gpkg, FEMA NFHL, parallelism, etc.
delete_deny_list (bool, default True)
    When ``True``, run AOI-level deny-list cleanup at the end of the branch
    loop. When ``False``, keep every intermediate file.
deny_unit_list (optional Path)
    Path to a deny-list text file applied to the AOI root after every branch
    finishes. Only consulted when ``delete_deny_list=True``. When ``None`` and
    ``delete_deny_list=True``, the function falls back to the bundled
    ``fimbox/config/deny_unit.lst``.
branch_ids_csv (optional Path)
    Where the per-AOI success registry goes. Defaults to ``<aoi_dir>/branch_ids.csv``.

Calibration
-----------
This function runs the **branch calculation only** — it does NOT invoke
``run_calibration``. After it returns (and any cleanup completes), call
``fimbox.run_calibration(aoi_dir, cfg)`` explicitly to produce the
AOI-level hydrotable.{csv,feather,parquet} and aggregated infrastructure
tables.

Outputs
-------
The branch-zero and non-zero branch outputs from CreateHAND (unchanged from
process_branches), plus:

    <aoi_dir>/branch_ids.csv
        Two-column ``<aoi_id>,<branch_id>`` rows for every branch that
        completed without error (branch zero is always the first row, then
        every successful non-zero branch).

After deny_unit cleanup the AOI root no longer contains the intermediate
rasters / vectors listed in the deny file (the production hydroTable,
crosswalked catchments, HAND raster, etc. are kept).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .outputs_cleanup import remove_deny_list_files
from .process_branches import (
    AOIProcessingConfig,
    BranchResult,
    process_branches,
)

log = logging.getLogger(__name__)


def _bundled_deny_list(name: str) -> Optional[Path]:
    """Return the path to a deny-list shipped under ``fimbox/config/``.

    Tries two roots: a sibling ``config/`` next to the fimbox package (for
    editable installs that keep config files in the repo root) and the
    project root directly. Returns ``None`` when neither exists.
    """
    pkg_root = Path(__file__).resolve().parents[2]
    for candidate in (
        pkg_root.parent.parent / "config" / name,    # repo_root/config/...
        pkg_root.parent / "config" / name,           # alt layout
        pkg_root / "config" / name,                  # in-package fallback
    ):
        if candidate.is_file():
            return candidate
    return None


@dataclass
class AllBranchesResult:
    """Aggregate result of one ``calculate_allbranches`` call."""

    branch_results: list[BranchResult]
    branch_ids_csv: Path
    n_branch_zero_recorded: int     # 0 or 1
    n_non_zero_recorded: int        # number of successful non-zero branches written
    n_unit_files_removed: int       # AOI-level deny-list cleanup count (0 when skipped)


def calculate_allbranches(
    cfg: AOIProcessingConfig,
    *,
    delete_deny_list: bool = True,
    deny_unit_list: Optional[Path] = None,
    branch_ids_csv: Optional[Path] = None,
) -> AllBranchesResult:
    """Run the per-AOI branch loop + AOI-level cleanup.

    Equivalent to running this shell sequence by hand::

        generate_branch_list_csv  -o branch_ids.csv  -u <aoi_id>  -b 0
        process_branch.sh    <huc> <branch_id>         (in parallel for every non-0 branch)
        # ↑ each successful branch also appends to branch_ids.csv
        outputs_cleanup.py   -d <aoi_dir>  -l deny_unit_list  -b <aoi_id>

    Returns an :class:`AllBranchesResult` summarising what got recorded and
    cleaned up.
    """
    aoi_dir = Path(cfg.aoi_dir)
    branch_ids_csv = (
        Path(branch_ids_csv) if branch_ids_csv else aoi_dir / "branch_ids.csv"
    )

    log.info(f"=== calculate_allbranches: {cfg.aoi_id} ===")

    # register branch zero in the success CSV before launching the
    # parallel loop. Branch zero's outputs are assumed to be produced by an
    # earlier ``BranchZero`` + ``CreateHAND`` pass (test_branchprocessing's Z1
    # step or the same modules called directly).
    n_b0_recorded = _append_branch_id(
        branch_ids_csv, aoi_id=cfg.aoi_id, branch_id=cfg.branch_zero_id
    )

    # parallel non-zero branch loop. process_branches runs them
    # through ProcessPoolExecutor and returns one BranchResult per branch.
    # Calibration is NOT invoked here — call ``fimbox.run_calibration``
    # separately after this function (and any deny-list cleanups) finish.
    results = process_branches(cfg)

    # Per-branch success registry. The shell wraps each parallel worker with
    # process_branch.sh which only appends on err_exists==0; the equivalent
    # here is "status == ok" (61/64/65 codes are mapped by _classify_branch_error).
    n_non_zero_recorded = 0
    for r in results:
        if r.status == "ok":
            n_non_zero_recorded += _append_branch_id(
                branch_ids_csv, aoi_id=cfg.aoi_id, branch_id=r.branch_id
            )

    # AOI-level deny-list cleanup. Default behaviour deletes the AOI
    # intermediates listed in ``deny_unit.lst``; pass ``delete_deny_list=False``
    # to keep every file. When ``delete_deny_list=True`` and no explicit path
    # is supplied, fall back to the deny list shipped under fimbox/config/.
    n_removed = 0
    if not delete_deny_list:
        log.info("AOI deny-list cleanup disabled (delete_deny_list=False)")
    else:
        if deny_unit_list is None:
            deny_unit_list = _bundled_deny_list("deny_unit.lst")
            if deny_unit_list is None:
                log.warning(
                    "delete_deny_list=True but no deny_unit_list supplied and "
                    "fimbox/config/deny_unit.lst not found — skipping cleanup."
                )
        if deny_unit_list is not None and Path(deny_unit_list).is_file():
            log.info(f"--- AOI deny-list cleanup ({Path(deny_unit_list).name}) ---")
            try:
                n_removed = remove_deny_list_files(
                    src_dir=aoi_dir,
                    deny_list=deny_unit_list,
                    branch_id=cfg.aoi_id,
                    verbose=True,
                )
            except Exception as exc:
                log.warning(f"AOI deny-list cleanup failed: {exc}")
        elif deny_unit_list is not None:
            log.warning(
                f"deny_unit_list does not exist, skipping: {deny_unit_list}"
            )

    # summary line.
    n_ok = sum(1 for r in results if r.status == "ok")
    log.info(
        f"calculate_allbranches: {cfg.aoi_id} | "
        f"branch_zero registered={n_b0_recorded} | "
        f"non-zero branches ok={n_ok}/{len(results)} | "
        f"unit files removed={n_removed} ==="
    )

    return AllBranchesResult(
        branch_results=results,
        branch_ids_csv=branch_ids_csv,
        n_branch_zero_recorded=n_b0_recorded,
        n_non_zero_recorded=n_non_zero_recorded,
        n_unit_files_removed=n_removed,
    )


def _append_branch_id(csv_path: Path, *, aoi_id: str, branch_id: str) -> int:
    """Append a ``<aoi_id>,<branch_id>`` row to ``csv_path``. Creates the file
    when it doesn't exist. Returns 1 on success (matches the shell's
    generate_branch_list_csv.py semantics)."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "a") as fh:
        fh.write(f"{aoi_id},{branch_id}\n")
    return 1


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Run the per-AOI branch loop + AOI-level deny-list cleanup. "
            "Wraps process_branches and adds the branch_ids.csv success "
            "registry. Calibration is NOT invoked — run "
            "``python -m fimbox.preprocessing.calibrate.pipeline`` separately."
        )
    )
    parser.add_argument("--aoi-dir", required=True)
    parser.add_argument("--aoi-id", required=True)
    parser.add_argument("--branch-list", default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--deny-unit-list", default=None,
        help=(
            "Path to deny_unit.lst (AOI-level cleanup). When --no-delete-deny-list "
            "is not set and this is omitted, fimbox/config/deny_unit.lst is used."
        ),
    )
    parser.add_argument(
        "--no-delete-deny-list", action="store_true",
        help=(
            "Skip AOI-level deny-list cleanup and keep every intermediate file. "
            "Default behaviour (no flag) deletes the files listed in deny_unit.lst."
        ),
    )
    args = parser.parse_args()

    aoi_dir = Path(args.aoi_dir)
    cfg = AOIProcessingConfig(
        aoi_dir=aoi_dir,
        aoi_id=args.aoi_id,
        branch_list_path=(
            Path(args.branch_list) if args.branch_list else aoi_dir / "branch_list.csv"
        ),
        n_workers=args.workers,
    )
    deny = Path(args.deny_unit_list) if args.deny_unit_list else None
    calculate_allbranches(
        cfg,
        delete_deny_list=not args.no_delete_deny_list,
        deny_unit_list=deny,
    )
