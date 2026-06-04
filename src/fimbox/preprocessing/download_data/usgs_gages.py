"""
Author: Supath Dhital
Date Updated: May 2026

Download USGS gauge points (CONUS) from the ArcGIS Online FeatureServer.

Service
-------
https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services
    /USGS_Gauge_Sites_CONUS/FeatureServer/0
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import pandas as pd

from .nhdplus import ArcGISDownloader

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

_SERVICE_URL = (
    "https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services"
    "/USGS_Gauge_Sites_CONUS/FeatureServer/0"
)


class DownloadUSGSGages(ArcGISDownloader):
    """USGS gauge points (CONUS) — ArcGIS Online FeatureServer layer 0."""

    def __init__(self, out_sr: int = 5070, n_workers: int = 8):
        super().__init__(
            layer_url=_SERVICE_URL,
            out_sr=out_sr,
            n_workers=n_workers,
        )

    def download(
        self,
        boundary,
        *,
        aoi_id: str,
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[int] = None,
        where: str = "1=1",
        out_dir: Optional[PathLike] = None,
        out_name: str = "usgs_gages.gpkg",
        out_layer: str = "usgs_gages",
    ) -> gpd.GeoDataFrame:
        """Download USGS gages that intersect ``boundary`` and rename the columns
        to the schema ``assign_gages_to_branches`` expects.

        Parameters
        ----------
        boundary : path / GeoDataFrame / shapely geometry / (xmin,ymin,xmax,ymax)
            Region to filter gages to (ESRI ``esriSpatialRelIntersects``).
        aoi_id : str
            User-supplied AOI identifier written into every gage row's
            ``aoi_id`` column. Use the same value you pass to
            ``AOIProcessingConfig(aoi_id=...)``.
        out_dir : optional
            When provided, writes the result to ``<out_dir>/<out_name>`` as a
            GeoPackage and returns the in-memory GeoDataFrame as well.

        Returns
        -------
        GeoDataFrame
            Empty when no gages fall inside the boundary.
        """
        gdf = super().download(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            where=where,
            out_dir=None,
            out_name=out_name,
            out_layer=out_layer,
        )

        if gdf.empty:
            log.warning(f"USGS gages: no gauges found inside AOI {aoi_id!r}")
            return gdf

        gdf = _normalize_gage_schema(gdf, aoi_id=str(aoi_id))
        log.info(f"USGS gages: {len(gdf)} gauges in AOI {aoi_id!r}")

        if out_dir:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / out_name
            gdf.to_file(out_path, layer=out_layer, driver="GPKG", index=False)
            log.info(f"USGS gages --> {out_name}")

        return gdf


def _normalize_gage_schema(gdf: gpd.GeoDataFrame, *, aoi_id: str) -> gpd.GeoDataFrame:
    """Rename USGS FeatureServer columns to fimbox's gage-crosswalk schema."""
    gdf = gdf.copy()

    # USGS site_id --> location_id (the gage ID used by every downstream tool)
    if "site_id" in gdf.columns and "location_id" not in gdf.columns:
        gdf = gdf.rename(columns={"site_id": "location_id"})
    if "location_id" in gdf.columns:
        gdf["location_id"] = gdf["location_id"].astype(str)

    # NHDPlus COMID --> NWM feature_id (cast to nullable int so missing values
    # survive the merge in assign_gages_to_branches).
    if "COMID" in gdf.columns and "feature_id" not in gdf.columns:
        gdf = gdf.rename(columns={"COMID": "feature_id"})
    if "feature_id" in gdf.columns:
        gdf["feature_id"] = pd.to_numeric(gdf["feature_id"], errors="coerce").astype(
            "Int64"
        )
    gdf["aoi_id"] = str(aoi_id)
    gdf["source"] = "usgs_gage"

    return gdf


# CLI
if __name__ == "__main__":
    import argparse
    from ...logging_utils import configure_cli_logging

    configure_cli_logging()
    parser = argparse.ArgumentParser(
        description="Download USGS gauges that intersect a boundary."
    )
    parser.add_argument(
        "--boundary", required=True, help="Boundary file (gpkg/shp/geojson)"
    )
    parser.add_argument("--boundary-layer", default=None)
    parser.add_argument(
        "--aoi-id", required=True, help="AOI identifier tagged on every row"
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--out-name", default="usgs_gages.gpkg")
    parser.add_argument("--out-sr", type=int, default=5070)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    DownloadUSGSGages(out_sr=args.out_sr, n_workers=args.workers).download(
        boundary=args.boundary,
        boundary_layer=args.boundary_layer,
        aoi_id=args.aoi_id,
        out_dir=args.out_dir,
        out_name=args.out_name,
    )
