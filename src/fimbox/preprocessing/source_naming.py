"""
Author: Supath Dhital
Date Created: June, 2026

Source-data filename conventions, parameterised by an identifier prefix.

Files derived from a hydrography source are named ``{identifier}<suffix>``. The
default identifier is ``"nwm"`` (the NWM hydrofabric), which preserves the legacy
filenames exactly. A custom identifier lets users stage flowlines/catchments from
any source without the ``nwm_`` labels being misleading.

The suffix is stable and unique per kind, so a file can always be resolved by
suffix-glob even when the identifier is unknown — this keeps every downstream
step robust whether or not the identifier was threaded to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

DEFAULT_IDENTIFIER = "nwm"

# kind -> stable suffix 
SOURCE_SUFFIXES: dict[str, str] = {
    "streams": "_subset_streams.gpkg",
    "catchments": "_catchments_proj_subset.gpkg",
    "lakes": "_lakes_proj_subset.gpkg",
    "headwaters_points": "_headwater_points_subset.gpkg",
    "headwaters": "_headwaters.gpkg",
    "lp_streams": "_subset_streams_levelPaths.gpkg",
    "lp_streams_dissolved": "_subset_streams_levelPaths_dissolved.gpkg",
    "lp_streams_extended": "_subset_streams_levelPaths_extended.gpkg",
    "lp_streams_dissolved_headwaters": "_subset_streams_levelPaths_dissolved_headwaters.gpkg",
    "lp_catchments": "_catchments_proj_subset_levelPaths.gpkg",
}


def source_name(
    kind: str,
    identifier: str = DEFAULT_IDENTIFIER,
    branch_id: Optional[Union[str, int]] = None,
) -> str:
    """Build a source filename, e.g. ``source_name("streams", "myid")`` ->
    ``"myid_subset_streams.gpkg"``. When ``branch_id`` is given it is inserted
    before the extension (per-branch derivative), e.g.
    ``"myid_subset_streams_levelPaths_3.gpkg"``."""
    suffix = SOURCE_SUFFIXES[kind]
    if branch_id is None:
        return f"{identifier}{suffix}"
    stem, _, ext = suffix.rpartition(".")
    return f"{identifier}{stem}_{branch_id}.{ext}"


def _kind_of(name: str) -> Optional[str]:
    """Return the kind a filename belongs to: the kind whose suffix is the
    *longest* one the name ends with. This disambiguates overlapping suffixes
    (e.g. ``_headwaters.gpkg`` is also a tail of
    ``_subset_streams_levelPaths_dissolved_headwaters.gpkg``)."""
    best_kind, best_len = None, -1
    for kind, suffix in SOURCE_SUFFIXES.items():
        if name.endswith(suffix) and len(suffix) > best_len:
            best_kind, best_len = kind, len(suffix)
    return best_kind


def detect_identifier(directory: Union[str, Path], default: str = DEFAULT_IDENTIFIER) -> str:
    """Infer the identifier prefix from the staged streams file in ``directory``.

    Falls back to ``default`` when no streams file is present.
    """
    suffix = SOURCE_SUFFIXES["streams"]
    for match in sorted(Path(directory).glob(f"*{suffix}")):
        if _kind_of(match.name) == "streams":
            return match.name[: -len(suffix)]
    return default


def resolve_source(
    directory: Union[str, Path],
    kind: str,
    identifier: Optional[str] = None,
    branch_id: Optional[Union[str, int]] = None,
) -> Path:
    """Return the path to a source file in ``directory`` for ``kind``.

    Resolution order: the exact ``{identifier}<suffix>`` when ``identifier`` is
    given and exists; otherwise the lone file matching ``*<suffix>`` (preferring
    the legacy ``nwm`` prefix on ties). When nothing exists yet, returns the
    ``{identifier or "nwm"}<suffix>`` path so callers can still create it.
    """
    directory = Path(directory)
    suffix = SOURCE_SUFFIXES[kind]

    if branch_id is not None:
        # per-branch derivatives are written deterministically, never globbed.
        ident = identifier or detect_identifier(directory)
        return directory / source_name(kind, ident, branch_id)

    if identifier:
        exact = directory / source_name(kind, identifier)
        if exact.exists():
            return exact

    # a file belongs to ``kind`` only if ``suffix`` is the LONGEST known suffix
    # it ends with — prevents a short suffix matching a more-specific filename.
    matches = [m for m in sorted(directory.glob(f"*{suffix}")) if _kind_of(m.name) == kind]
    if not matches:
        return directory / source_name(kind, identifier or DEFAULT_IDENTIFIER)
    for m in matches:
        if m.name == source_name(kind, DEFAULT_IDENTIFIER):
            return m
    return matches[0]
