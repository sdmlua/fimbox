"""
Author: Supath Dhital
Date Updated: May 2026

Delete files in a branch directory according to a deny-list

Deny-list format
----------------
Plain text file, one filename pattern per line:

    # comment lines (start with #) are skipped
    bridge_elev_diff_meters_{}.tif      # {} is replaced with branch_id
    catch_list_{}.txt                   # ...
    coordFile_{}.txt
    demDerived_reaches_{}.shp
    foo.tif                             # literal name (no {})
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def remove_deny_list_files(
    src_dir: PathLike,
    deny_list: PathLike,
    branch_id: str,
    *,
    identifier: str = "nwm",
    verbose: bool = False,
) -> int:
    src_dir = Path(src_dir)
    branch_id = str(branch_id).strip()
    if not branch_id:
        raise ValueError("branch_id must not be empty")

    # The bash wrapper passes the literal string "NONE" when the user wants to disable cleanup entirely.
    if str(deny_list).upper() == "NONE":
        log.info("outputs_cleanup: deny list = 'NONE', skipping cleanup")
        return 0

    if not src_dir.is_dir():
        raise NotADirectoryError(f"src_dir does not exist: {src_dir}")
    deny_list = Path(deny_list)
    if not deny_list.is_file():
        raise FileNotFoundError(f"deny list not found: {deny_list}")

    patterns = _read_deny_list(deny_list, branch_id, identifier)
    log.info(
        f"outputs_cleanup: applying {len(patterns)} deny-list patterns "
        f"under {src_dir.name}"
    )

    n_removed = 0
    for pattern in patterns:
        for found in src_dir.rglob(pattern):
            if found.is_file():
                if verbose:
                    log.info(f"  rm {found.relative_to(src_dir)}")
                found.unlink()
                n_removed += 1
    log.info(f"outputs_cleanup: removed {n_removed} files")
    return n_removed


def _read_deny_list(path: Path, branch_id: str, identifier: str = "nwm") -> list[str]:
    """Parse the deny-list text file into filename patterns, with ``{}``
    substituted for the branch id.

    Source-derived files carry the AOI identifier prefix (``nwm`` by default,
    ``nwmmr`` for medium-range, etc.). Deny entries are written with the
    legacy ``nwm`` prefix, so a leading ``nwm_`` is rewritten to the AOI's
    actual ``identifier`` so the files are matched whatever the prefix is."""
    out: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.replace("{}", branch_id)
        if identifier != "nwm" and line.startswith("nwm_"):
            line = f"{identifier}_" + line[len("nwm_") :]
        out.append(line)
    return out


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="Remove deny-listed files from a branch directory."
    )
    parser.add_argument("-d", "--src-dir", required=True)
    parser.add_argument(
        "-l",
        "--deny-list",
        required=True,
        help='Path to deny-list file, or "NONE" to skip cleanup.',
    )
    parser.add_argument("-b", "--branch-id", required=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    remove_deny_list_files(
        src_dir=args.src_dir,
        deny_list=args.deny_list,
        branch_id=args.branch_id,
        verbose=args.verbose,
    )
