"""
Author: Manjila Singh
Date Updated: July 2026

NextGen-in-a-Box integration for fimbox.

Map an AOI to the NOAA/OWP NextGen v2.2 hydrofabric and the
ngen/t-route discharge published on the public CIROH community NextGen
DataStream bucket (https://ciroh-community-ngen-datastream.s3.amazonaws.com),
so the resulting streamflow can drive FIM generation.

Resolution
----------
NextGenHydrofabric   AOI (shp/gpkg) -> intersecting catchments + network-id crosswalk
NextGenDatastream    NextGen network ids -> ngen/t-route discharge (S3)

Orchestration
-------------
NextGenAOI / getNextGenAOI   AOI -> catchments + discharge in one call, written
                             into the standard fimbox AOI layout.

Example
-------
    import fimbox

    res = fimbox.getNextGenAOI("my_aoi.shp", out_dir="out")
    res.catchments_path   # <AOI>/hydrofabric/aoi_catchments.gpkg
    res.feature_ids       # NextGen feature_ids (== streamflow network ids)
    res.discharge_csvs    # FIM-ready <AOI>/discharge-inputs/*.csv
"""

from __future__ import annotations

from .datastream import DischargeRun, NextGenDatastream
from .hydrofabric import AOIHydrofabric, NextGenHydrofabric, build_vpu_index
from .pipeline import NextGenAOI, NextGenResult, getNextGenAOI

__all__ = [
    "NextGenHydrofabric",
    "AOIHydrofabric",
    "NextGenDatastream",
    "DischargeRun",
    "NextGenAOI",
    "NextGenResult",
    "getNextGenAOI",
    "build_vpu_index",
]
