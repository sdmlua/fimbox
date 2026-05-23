"""
Author: Supath Dhital
Date Updated: May 2026

Build the two text files for the synthetic rating curve
generator, reads:

- stage_{id}.txt
    A column of stage heights (m). The header is the literal word ``Stage``;
    subsequent rows are stages from ``stages_min`` to ``stages_max`` inclusive
    at ``stages_interval`` spacing. Used as the discretization for the SRC.

- catch_list_{id}.txt
    Tabular per-HydroID metadata consumed alongside the stage column. First
    row is the count of HydroIDs. Each remaining row is
    ``HydroID S0 LengthKm areasqkm``, in the same order as the catchments
    file. Used to assemble per-reach hydraulic-table rows.

Inputs
------
flows_gpkg      : demDerived_reaches_split_filtered_{id}.gpkg
catchments_gpkg : gw_catchments_reaches_filtered_addedAttributes_{id}.gpkg

Both inputs are joined on HydroID to drop rows that survived one filter but
not the other, then written out in HydroID order.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

import geopandas as gpd
import numpy as np

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def make_stages_and_catchlist(
    flows_gpkg: PathLike,
    catchments_gpkg: PathLike,
    out_stages: PathLike,
    out_catchlist: PathLike,
    stages_min: float = 0.0,
    stages_interval: float = 0.3048,
    stages_max: float = 25.2984,
) -> tuple[Path, Path]:
    """
    Write the stage ladder and catchment list text files used by the SRC step.

    Defaults reproduce the FIM ``params_template.env`` settings:
    0 m to 25.2984 m in 0.3048 m (1 ft) increments.
    """
    flows_gpkg = Path(flows_gpkg)
    catchments_gpkg = Path(catchments_gpkg)
    out_stages = Path(out_stages)
    out_catchlist = Path(out_catchlist)
    out_stages.parent.mkdir(parents=True, exist_ok=True)

    flows = gpd.read_file(str(flows_gpkg), engine="fiona")
    catchments = gpd.read_file(str(catchments_gpkg), engine="fiona")

    # Keep only HydroIDs present in both files (the SRC step requires both
    # the geometry-derived area and the flowline-derived slope/length).
    flows = flows.merge(catchments[["HydroID"]], on="HydroID", how="inner")
    catchments = catchments.merge(flows[["HydroID"]], on="HydroID", how="inner")

    # Stage list: include both endpoints, rounded to suppress np.arange drift.
    stages = np.round(
        np.arange(stages_min, stages_max + stages_interval, stages_interval),
        4,
    )

    hydro_ids = flows["HydroID"].tolist()
    slopes = flows["S0"].tolist()
    length_km = flows["LengthKm"].tolist()

    if "areasqkm" in catchments.columns:
        area_sq_km = catchments["areasqkm"].tolist()
    else:
        area_sq_km = (catchments.geometry.area / 1e6).tolist()

    with out_stages.open("w") as f:
        f.write("Stage\n")
        for s in stages:
            f.write(f"{s}\n")

    with out_catchlist.open("w") as f:
        f.write(f"{len(hydro_ids)}\n")
        for h, s, l, a in zip(hydro_ids, slopes, length_km, area_sq_km):
            f.write(f"{h} {s} {l} {a}\n")

    log.info(
        "stages_catchlist: %d hydroIDs, %d stages --> %s",
        len(hydro_ids),
        len(stages),
        out_catchlist.name,
    )
    return out_stages, out_catchlist
