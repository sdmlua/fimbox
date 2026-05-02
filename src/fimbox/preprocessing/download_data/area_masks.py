"""
Author: Supath Dhital
Updated = May 2026

Download static area mask layers used by FIM preprocessing.
These services are clipped/intersected against the study buffer before the
remaining data downloads are run.
"""

from __future__ import annotations
from typing import Optional, Union
from pathlib import Path
import geopandas as gpd
from .nhdplus import ArcGISDownloader


# get the land/sea mask for the buffered area
class DownloadLandSea(ArcGISDownloader):
    """Land, sea, and Great Lakes mask polygons."""

    def __init__(self, out_sr: int = 5070, n_workers: int = 8):
        super().__init__(
            layer_url=(
                "https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services"
                "/Land_Sea_and_Great_Lakes_/FeatureServer/0"
            ),
            out_sr=out_sr,
            n_workers=n_workers,
        )

    def download(
        self,
        boundary,
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[int] = None,
        where: str = "1=1",
        out_dir: Optional[Union[str, Path]] = None,
        out_name: str = "LandSea_subset.gpkg",
        out_layer: str = "LandSea_subset",
    ) -> gpd.GeoDataFrame:
        return super().download(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            where=where,
            out_dir=out_dir,
            out_name=out_name,
            out_layer=out_layer,
        )


# get the DEM Domain
class DownloadDEMDomain(ArcGISDownloader):
    """USGS 3DEP DEM coverage domain polygons."""

    def __init__(self, out_sr: int = 5070, n_workers: int = 8):
        super().__init__(
            layer_url=(
                "https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services"
                "/USGS_DEM_Domain/FeatureServer/0"
            ),
            out_sr=out_sr,
            n_workers=n_workers,
        )

    def download(
        self,
        boundary,
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[int] = None,
        where: str = "1=1",
        out_dir: Optional[Union[str, Path]] = None,
        out_name: str = "DEM_Domain.gpkg",
        out_layer: str = "DEM_Domain",
    ) -> gpd.GeoDataFrame:
        return super().download(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            where=where,
            out_dir=out_dir,
            out_name=out_name,
            out_layer=out_layer,
        )
