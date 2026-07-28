"""
Author: Supath Dhital
Date Created: July 2026

Adapter over ``ngiab_data_preprocess`` (the NGIAB data preprocessor).

That package is the reference implementation of the ngen hydrofabric, so
anything it already does correctly is imported rather than reimplemented:

    FilePaths                the authoritative bucket / cache locations
    download_from_s3         their S3 fetcher, used for the network graph
    get_upstream_ids         the upstream walk, including the outlet-nexus hop
    get_graph                the cached igraph network

What is *not* reused is anything reached only through the 4.9 GB
``conus_nextgen.gpkg``: ``validate_hydrofabric`` (which downloads it, and
prompts interactively), the table subsetters, and the gage/feature-id lookups.
fimbox reads those from the parquet mirror instead — see :mod:`.hydrofabric`.

``get_graph`` silently rebuilds the graph from that CONUS GeoPackage when the
pickle is missing, so :func:`ensure_network_graph` always places the 42 MB
pickle first and raises if it cannot.

ngiab is **not** a declared fimbox dependency — it cannot be co-resolved with
``teehr==0.5.0`` (see the note in ``pyproject.toml``). So this module is an
opportunistic integration: every entry point returns None when the package is
absent, and :mod:`.hydrofabric` defaults to its own parquet walk. Pass
``use_ngiab=True`` to prefer ngiab's traversal where it is installed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger(__name__)

# Fallback base URL, used when ngiab is not installed to read it from FilePaths.
_HF_BUCKET = "https://communityhydrofabric.s3.us-east-1.amazonaws.com/"

# Community bucket layout for the igraph network pickle.
_GRAPH_BUCKET = "communityhydrofabric"
_GRAPH_KEY = "hydrofabrics/community/conus_igraph_network.gpickle"


def has_ngiab() -> bool:
    """True when ``ngiab_data_preprocess`` is importable."""
    try:
        import data_processing.file_paths  # noqa: F401
    except ImportError:
        return False
    return True


def hf_bucket() -> str:
    """The community hydrofabric bucket URL, from ngiab when available."""
    try:
        from data_processing.file_paths import FilePaths

        return str(FilePaths.hf_bucket)
    except ImportError:
        return _HF_BUCKET


def ensure_network_graph() -> Optional[Path]:
    """Place ngiab's network graph pickle in its cache, downloading if needed.

    Returns the path, or None when ngiab is unavailable. Raises if the pickle is
    missing and cannot be fetched — falling through would make ``get_graph``
    rebuild it from the CONUS GeoPackage, which is the multi-GB path this whole
    module exists to avoid.
    """
    try:
        from data_processing.file_paths import FilePaths
        from data_sources.source_validation import download_from_s3
    except ImportError:
        return None

    path = Path(FilePaths.hydrofabric_graph)
    if path.is_file():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Fetching ngen network graph --> {path}")
    if not download_from_s3(str(path), bucket=_GRAPH_BUCKET, key=_GRAPH_KEY):
        raise RuntimeError(
            f"could not download the ngen network graph to {path}. Refusing to "
            "continue: ngiab would rebuild it from the 4.9 GB CONUS GeoPackage."
        )
    return path


def upstream(
    seeds: Sequence[str], include_outlet: bool = True
) -> Optional[tuple[list[str], list[str]]]:
    """Upstream ``(wb_ids, divide_ids)`` for ``seeds`` via ngiab's graph.

    Returns None when ngiab is unavailable, so the caller can fall back. The
    ``include_outlet`` semantics are ngiab's own ``--subset_type``.
    """
    try:
        from data_processing.graph_utils import get_graph, get_upstream_ids
    except ImportError:
        return None

    ensure_network_graph()
    graph = get_graph()
    names = get_upstream_ids(list(seeds), include_outlet)

    # One pass to map node name -> divide id; graph.vs.find() per node would be
    # a linear scan each time.
    cat_of = dict(zip(graph.vs["name"], graph.vs["cat"]))
    wb_ids = sorted(n for n in names if n and n.startswith("wb-"))
    divide_ids = sorted({cat_of.get(n) for n in wb_ids if cat_of.get(n)})
    return wb_ids, divide_ids
