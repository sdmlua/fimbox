"""
Author: Supath Dhital
Date Created: July 2026

NextGen (ngen) support for fimbox.

Hydrofabric
-----------
NgenHydrofabric   query the community hydrofabric parquet mirror for one AOI
NgenSelection     a resolved id set, before geometry is fetched
getNgenData       flowpaths + divides staged under the canonical filenames

Selectors are a HUC8/AOI boundary, ngen ``cat-id``s, NWM ``feature_id``s, or
USGS gage ids. Output uses the fimbox canonical stream schema, so the ngen
source is interchangeable with ``nwmmedium`` / ``nwmhigh`` downstream.

Streamflow and realization support land here as they are added.
"""

from __future__ import annotations

from .hydrofabric import (
    DEFAULT_IDENTIFIER,
    HF_CRS,
    HF_PARQUET_BASE,
    NgenHydrofabric,
    NgenSelection,
    getNgenData,
)

__all__ = [
    # classes
    "NgenHydrofabric",
    "NgenSelection",
    # function wrapper (FIMserv-style)
    "getNgenData",
    # constants
    "HF_PARQUET_BASE",
    "HF_CRS",
    "DEFAULT_IDENTIFIER",
]
