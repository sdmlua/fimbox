"""
Author: Supath Dhital
Date updated: July 2026
---------------------
Resolve the bridge GeoPackage the bridge-DEM stages read.

A missing bridge layer is recoverable: the AOI folder that lacks it still holds
the boundary the layer is derived from, so it is downloaded from OSM instead of
failing the stage.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger(__name__)

# Boundary written by preprocessing, best-first. The buffered one is what every
# other OSM download in the pipeline is clipped to.
_BOUNDARIES = ("wbd_buffered.gpkg", "wbd.gpkg", "wbd8_clp.gpkg")


def resolve_bridge_gpkg(
    bridge_gpkg: Union[str, Path],
    boundary: Optional[Union[str, Path]] = None,
    out_layer: str = "osm_bridges",
) -> Path:
    """Path to a bridge GeoPackage, downloaded from OSM if it is not there yet."""
    path = Path(bridge_gpkg)
    if path.exists():
        return path
    log.warning(f"Bridge GeoPackage not found: {path}")

    # The pipeline's other name for the same layer — reuse it before downloading.
    alt = path.parent / "osm_bridges.gpkg"
    if alt != path and alt.exists():
        log.info(f"Using the bridge layer already staged --> {alt.name}")
        return alt

    if boundary is None:
        boundary = next(
            (p for n in _BOUNDARIES if (p := path.parent / n).exists()), None
        )
    if boundary is None:
        raise FileNotFoundError(
            f"{path} is missing and {path.parent} holds no boundary "
            f"({', '.join(_BOUNDARIES)}) to download bridges for — "
            f"run preprocessing for this area first."
        )

    # Imported here so a run with the layer staged never loads the Overpass stack.
    from ..download_data.osm_data import DownloadOSMBridges

    log.info(f"Downloading OSM bridges for {Path(boundary).name} --> {path.name}")
    gdf = DownloadOSMBridges().download(
        boundary=boundary,
        out_dir=path.parent,
        out_name=path.name,
        out_layer=out_layer,
    )
    if gdf.empty:
        log.warning("OSM returned no bridges in this area.")
    log.info(f"{out_layer} --> {path.name}")
    return path
