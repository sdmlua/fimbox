"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date created: Jan 2026
Date updated: May 2026

Description: Downloads NWM Flowlines, Catchments, and Lakes from ArcGIS FeatureServer
endpoints. Uses intersect (not clip) so polygon boundaries are never cut. Pages are
fetched in parallel with dask.delayed for I/O-bound speed.
"""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import dask
import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import box
from shapely.ops import unary_union

logger = logging.getLogger(__name__)


# ArcGIS FeatureServer downloader
@dataclass
class ArcGISDownloader:
    """
    - Uses esriSpatialRelIntersects so features that touch the boundary are
      returned whole (no mid-polygon clipping).
    - Pages are fetched in parallel via dask.delayed.
    - Boundary may be a file path (shp/gpkg/geojson), GeoDataFrame, shapely
      geometry, or a (xmin, ymin, xmax, ymax) bbox tuple.
    """

    layer_url: str
    out_sr: int = 5070
    page_size: int = 2000
    timeout: int = 120
    n_workers: int = 8

    # boundary helpers

    def _load_geometry(
        self,
        boundary,
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[int] = None,
    ):
        """Return a single shapely geometry in EPSG:4326."""
        if isinstance(boundary, (str, Path)):
            bp = Path(boundary)
            if bp.suffix.lower() == ".gpkg":
                layer = boundary_layer or gpd.list_layers(bp).iloc[0]["name"]
                gdf = gpd.read_file(bp, layer=layer)
            else:
                gdf = gpd.read_file(bp)
            geom = unary_union(gdf.to_crs(4326).geometry)

        elif isinstance(boundary, (gpd.GeoDataFrame, gpd.GeoSeries)):
            geom = unary_union(boundary.to_crs(4326).geometry)

        elif isinstance(boundary, (tuple, list)) and len(boundary) == 4:
            geom = box(*boundary)
            if boundary_crs:
                geom = gpd.GeoSeries([geom], crs=boundary_crs).to_crs(4326).iloc[0]

        else:
            geom = boundary
            if boundary_crs:
                geom = gpd.GeoSeries([geom], crs=boundary_crs).to_crs(4326).iloc[0]

        try:
            n = len(geom.exterior.coords)
            if n > 1000:
                geom = geom.simplify(0.0001, preserve_topology=True)
        except AttributeError:
            pass

        return geom

    def _to_esri_geom(self, geom) -> dict:
        """Convert a shapely geometry to an ESRI JSON geometry dict."""
        from shapely.geometry import MultiPolygon

        if isinstance(geom, MultiPolygon):
            rings = []
            for poly in geom.geoms:
                rings.append([[float(x), float(y)] for x, y in poly.exterior.coords])
                for interior in poly.interiors:
                    rings.append([[float(x), float(y)] for x, y in interior.coords])
        else:
            rings = [[[float(x), float(y)] for x, y in geom.exterior.coords]]
            for interior in geom.interiors:
                rings.append([[float(x), float(y)] for x, y in interior.coords])

        return {"rings": rings}

    # HTTP
    def _post(self, params: dict) -> dict:
        url = f"{self.layer_url}/query"
        resp = requests.post(url, data=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                f"ArcGIS error {data['error']['code']}: {data['error']['message']}"
            )
        return data

    # page fetching
    def _fetch_page(
        self, base_params: dict, offset: int, page: int, total_pages: int
    ) -> gpd.GeoDataFrame:
        """Fetch a single page (called in parallel via dask)."""
        params = {
            **base_params,
            "resultOffset": offset,
            "resultRecordCount": self.page_size,
        }
        data = self._post(params)
        features = data.get("features", [])
        if not features:
            return gpd.GeoDataFrame()
        gdf = gpd.GeoDataFrame.from_features(features, crs=f"EPSG:{self.out_sr}")
        print(f"  Page {page}/{total_pages}: {len(gdf)} records")
        return gdf

    # public API
    def download(
        self,
        boundary,
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[int] = None,
        where: str = "1=1",
        out_dir: Optional[Union[str, Path]] = None,
        out_name: str = "nwm_data.gpkg",
        out_layer: str = "data",
    ) -> gpd.GeoDataFrame:
        """
        Parameters
        ----------
        boundary : file path, GeoDataFrame, shapely geometry, or (xmin,ymin,xmax,ymax) tuple
        boundary_layer : layer name when boundary is a GeoPackage
        boundary_crs : CRS of boundary when it is a shapely geometry or bbox
        where : SQL filter (default "1=1" = all records)
        out_dir : output directory; if None the result is returned but not saved
        out_name : output filename
        out_layer : layer name inside the GeoPackage
        """
        geom = self._load_geometry(boundary, boundary_layer, boundary_crs)
        esri_geom = self._to_esri_geom(geom)

        base_params = {
            "f": "geojson",
            "where": where,
            "outFields": "*",
            "returnGeometry": "true",
            "geometry": json.dumps(esri_geom),
            "geometryType": "esriGeometryPolygon",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": 4326,
            "outSR": self.out_sr,
        }

        # Count
        count_data = self._post({**base_params, "f": "json", "returnCountOnly": "true"})
        total = count_data.get("count", 0)
        if total == 0:
            print("No records found.")
            return gpd.GeoDataFrame()

        n_pages = math.ceil(total / self.page_size)
        print(
            f"Total records: {total}  |  Pages: {n_pages}  |  Workers: {min(n_pages, self.n_workers)}"
        )

        # Parallel page fetch with dask
        delayed_pages = [
            dask.delayed(self._fetch_page)(
                base_params, i * self.page_size, i + 1, n_pages
            )
            for i in range(n_pages)
        ]
        pages = dask.compute(
            *delayed_pages,
            scheduler="threads",
            num_workers=min(n_pages, self.n_workers),
        )

        chunks = [p for p in pages if not p.empty]
        if not chunks:
            print("No data downloaded.")
            return gpd.GeoDataFrame()

        result = gpd.GeoDataFrame(
            pd.concat(chunks, ignore_index=True), crs=f"EPSG:{self.out_sr}"
        )
        print(f"Downloaded {len(result)} total records.")

        if out_dir:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / out_name
            result.to_file(out_path, layer=out_layer, driver="GPKG")
            print(f"Saved → {out_path}")

        return result


# dataset downloaders
class NWMFlowlinesDownloader(ArcGISDownloader):
    """NWM Flowlines — ESRI FeatureServer layer 0."""

    def __init__(self, out_sr: int = 5070, n_workers: int = 8):
        super().__init__(
            layer_url=(
                "https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services"
                "/NWM_FlowLine/FeatureServer/0"
            ),
            out_sr=out_sr,
            n_workers=n_workers,
        )

    def download(
        self,
        boundary,
        boundary_layer=None,
        boundary_crs=None,
        where="1=1",
        out_dir=None,
        out_name="nwm_subset_streams.gpkg",
        out_layer="flowlines",
    ):
        return super().download(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            where=where,
            out_dir=out_dir,
            out_name=out_name,
            out_layer=out_layer,
        )


class NWMCatchmentsDownloader(ArcGISDownloader):
    """
    NWM Catchments — ArcGIS Online FeatureServer layer 0 (hosted by Supath Dhital).
    Catchments are returned whole (intersect, not clip) so watershed
    boundaries are never split.
    """

    def __init__(self, out_sr: int = 5070, n_workers: int = 8):
        super().__init__(
            layer_url=(
                "https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services"
                "/NWM_Catchments/FeatureServer/0"
            ),
            out_sr=out_sr,
            n_workers=n_workers,
        )

    def download(
        self,
        boundary,
        boundary_layer=None,
        boundary_crs=None,
        where="1=1",
        out_dir=None,
        out_name="nwm_catchments_proj_subset.gpkg",
        out_layer="catchments",
    ):
        return super().download(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            where=where,
            out_dir=out_dir,
            out_name=out_name,
            out_layer=out_layer,
        )


class NWMLakesDownloader(ArcGISDownloader):
    """
    NWM Lakes — ArcGIS Online FeatureServer layer 0 (hosted by Supath Dhital).
    Lakes are returned whole (intersect, not clip) so lake boundaries are
    never cut.
    """

    def __init__(self, out_sr: int = 5070, n_workers: int = 8):
        super().__init__(
            layer_url=(
                "https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services"
                "/NWM_Lakes/FeatureServer/0"
            ),
            out_sr=out_sr,
            n_workers=n_workers,
        )

    def download(
        self,
        boundary,
        boundary_layer=None,
        boundary_crs=None,
        where="1=1",
        out_dir=None,
        out_name="nwm_lakes_proj_subset.gpkg",
        out_layer="lakes",
    ):
        return super().download(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            where=where,
            out_dir=out_dir,
            out_name=out_name,
            out_layer=out_layer,
        )


# Unified entry point
def getNHDPlusData(
    boundary: Union[str, Path, gpd.GeoDataFrame],
    boundary_layer: Optional[str] = None,
    out_dir: Optional[Union[str, Path]] = None,
    epsg: int = 5070,
    download_flowlines: bool = True,
    download_catchments: bool = True,
    download_lakes: bool = True,
    n_workers: int = 8,
) -> dict:
    """
    Download NWM Flowlines, Catchments, and Lakes for a given boundary.

    Features that intersect the boundary are returned whole — no mid-polygon
    clipping. Use ``download_*`` flags to skip datasets you don't need.

    Parameters
    ----------
    boundary : file path, GeoDataFrame, or shapely geometry
    boundary_layer : layer name when boundary is a GeoPackage
    out_dir : directory to save outputs; if None data is returned but not saved
    epsg : output CRS (default 5070 CONUS Albers)
    download_flowlines : include NWM flowlines (default True)
    download_catchments : include NWM catchments (default True)
    download_lakes : include NWM lakes (default True)
    n_workers : max parallel page-fetch threads per dataset (default 8)

    Returns
    -------
    dict with keys "flowlines", "catchments", "lakes" (GeoDataFrames or None)
    """
    results = {"flowlines": None, "catchments": None, "lakes": None}

    common = dict(
        boundary=boundary,
        boundary_layer=boundary_layer,
        out_dir=out_dir,
    )

    if download_flowlines:
        print("\n=== NWM Flowlines ===")
        try:
            results["flowlines"] = NWMFlowlinesDownloader(
                out_sr=epsg, n_workers=n_workers
            ).download(**common)
        except Exception as exc:
            logger.error(f"Flowlines download failed: {exc}", exc_info=True)

    if download_catchments:
        print("\n=== NWM Catchments ===")
        try:
            results["catchments"] = NWMCatchmentsDownloader(
                out_sr=epsg, n_workers=n_workers
            ).download(**common)
        except Exception as exc:
            logger.error(f"Catchments download failed: {exc}", exc_info=True)

    if download_lakes:
        print("\n=== NWM Lakes ===")
        try:
            results["lakes"] = NWMLakesDownloader(
                out_sr=epsg, n_workers=n_workers
            ).download(**common)
        except Exception as exc:
            logger.error(f"Lakes download failed: {exc}", exc_info=True)

    return results


# OLD pipeline — EPA NHDPlus V2.1 S3 bucket

# import os, sys, py7zr, shutil, argparse
# from urllib.request import Request, urlopen
# from pynhd import WaterData
# from .utils import NHDBoundaryFinder, find_headwater_points
# from .dem_process import DEMProcessor
# from .nfhl_data import DownloadFEMANFHL
#
# class getNHDPlusData_OLD:
#     """
#     Downloads and processes NHDPlus V2.1 data from the EPA S3 bucket.
#     Fetches whole HUC6 VPU/RPU regions — slow and coarse for small study areas.
#     Superseded by getNHDPlusData() which uses ArcGIS FeatureServer endpoints.
#     """
#     def __init__(
#         self,
#         NHDglobalBoundary: str,     # Contains all NHDPlus VPU/RPU boundaries
#         inputs_dir=None,
#         boundary_path=None,
#         huc8=None,
#         epsg=None,
#         out_dir=None,
#         auto_run=True,
#     ):
#         self.boundary_path = Path(boundary_path) if boundary_path else None
#         self.NHDglobalBoundary = Path(NHDglobalBoundary)
#         self.huc8 = huc8
#         self.folder_name = self.boundary_path.stem if self.boundary_path else self.huc8
#         if out_dir:
#             self.output_root = Path(out_dir)
#         elif inputs_dir:
#             self.output_root = Path(inputs_dir) / self.folder_name
#         else:
#             self.output_root = Path.cwd() / self.folder_name
#         self.raw_zip_dir = self.output_root / "raw_zips"
#         self.unzipped_dir = self.output_root / "unzipped"
#         for d in [self.output_root, self.raw_zip_dir, self.unzipped_dir]:
#             d.mkdir(parents=True, exist_ok=True)
#         self._setup_logger()
#         if self.boundary_path:
#             finder_input = str(self.boundary_path)
#         elif self.huc8:
#             self.logger.info(f"Extracting HUC8 boundary for {self.huc8} via pynhd...")
#             wd = WaterData("wbd08")
#             self.user_gdf = wd.byfilter(f"huc8 = '{self.huc8}'")
#             finder_input = self.user_gdf
#         else:
#             raise ValueError("Either boundary_path or huc8 must be provided.")
#         finder = NHDBoundaryFinder(finder_input, str(self.NHDglobalBoundary))
#         self.vpus = finder.vpus
#         self.rpus = finder.rpus
#         if not self.vpus:
#             raise ValueError("Boundary does not intersect any NHDPlus VPU units.")
#         self.primary_drainage = self.vpus[0]['DrainageAreaID']
#         self.target_epsg = 5070 if self.primary_drainage != "PI" else (epsg or 6637)
#         if self.boundary_path:
#             self.user_gdf = gpd.read_file(self.boundary_path).to_crs(epsg=self.target_epsg)
#         else:
#             self.user_gdf = self.user_gdf.to_crs(epsg=self.target_epsg)
#             self.user_gdf.to_file(self.output_root / f"HUC8_{self.huc8}_boundary.gpkg", driver="GPKG")
#         self.working_boundary = self.user_gdf
#         if auto_run:
#             self.run_full_pipeline()
#
#     def _setup_logger(self):
#         log_file = self.output_root / f"{self.folder_name}_pipeline.log"
#         self.logger = logging.getLogger(self.folder_name)
#         self.logger.setLevel(logging.INFO)
#         if self.logger.hasHandlers():
#             self.logger.handlers.clear()
#         file_handler = logging.FileHandler(log_file, mode='w')
#         file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
#         self.logger.addHandler(file_handler)
#         for mod in ["DEMProcessor", "FEMADownloader", "pynhd"]:
#             sl = logging.getLogger(mod)
#             sl.setLevel(logging.INFO)
#             sl.addHandler(file_handler)
#             sl.propagate = False
#
#     def _url_exists(self, url):
#         try:
#             with urlopen(Request(url, method="HEAD"), timeout=10): return True
#         except: return False
#
#     def _get_nhd_url(self, drainage, unit_id, component):
#         base = "https://dmap-data-commons-ow.s3.amazonaws.com/NHDPlusV21/Data"
#         priority_versions = ["01", "02", "03", "04", "07", "09"]
#         prefixes = [f"{base}/NHDPlus{drainage}", f"{base}/NHDPlus{drainage}/NHDPlus{unit_id}"]
#         for vv in priority_versions:
#             for pre in prefixes:
#                 url = f"{pre}/NHDPlusV21_{drainage}_{unit_id}_{component}_{vv}.7z"
#                 try:
#                     with requests.head(url, timeout=5) as r:
#                         if r.status_code == 200: return url
#                 except: continue
#         for vv in range(30, 0, -1):
#             vv_str = f"{vv:02d}"
#             if vv_str in priority_versions: continue
#             for pre in prefixes:
#                 url = f"{pre}/NHDPlusV21_{drainage}_{unit_id}_{component}_{vv_str}.7z"
#                 try:
#                     with requests.head(url, timeout=2) as r:
#                         if r.status_code == 200: return url
#                 except: continue
#         return ""
#
#     def download_and_unzip(self, units, components):
#         for vpu in units:
#             for comp in components:
#                 url = self._get_nhd_url(vpu['DrainageAreaID'], vpu['UnitID'], comp)
#                 if not url: continue
#                 zip_path = self.raw_zip_dir / os.path.basename(url)
#                 if not zip_path.exists():
#                     self.logger.info(f"Downloading {url}...")
#                     with requests.get(url, stream=True) as r:
#                         r.raise_for_status()
#                         with open(zip_path, 'wb') as f:
#                             shutil.copyfileobj(r.raw, f)
#                 with py7zr.SevenZipFile(zip_path, mode='r') as z:
#                     z.extractall(path=self.unzipped_dir)
#
#     def _pick_field(self, df, candidates):
#         cols = {c.lower(): c for c in df.columns}
#         for cand in candidates:
#             if cand.lower() in cols: return cols[cand.lower()]
#         return ""
#
#     def _join_attributes(self, flowlines, unzipped_path):
#         vaa_p = list(unzipped_path.rglob("PlusFlowlineVAA.dbf"))
#         flow_p = list(unzipped_path.rglob("PlusFlow.dbf"))
#         if not vaa_p or not flow_p: return flowlines
#         vaa = pd.concat([gpd.read_file(p) for p in vaa_p])
#         flow = pd.concat([gpd.read_file(p) for p in flow_p])
#         c_f = self._pick_field(flowlines, ["ComID", "COMID"])
#         v_c_f = self._pick_field(vaa, ["ComID", "COMID"])
#         s_f = self._pick_field(vaa, ["StreamOrde"])
#         f_f = self._pick_field(flow, ["FROMCOMID"])
#         t_f = self._pick_field(flow, ["TOCOMID"])
#         flowlines = flowlines.merge(vaa[[v_c_f, s_f]], left_on=c_f, right_on=v_c_f, how='left')
#         flowlines = flowlines.merge(flow[[f_f, t_f]], left_on=c_f, right_on=f_f, how='left')
#         return flowlines.rename(columns={c_f: 'ID', t_f: 'to', s_f: 'order_'})
#
#     def process_catchments(self, intersect_only=True):
#         missing = [v for v in self.vpus if not list(
#             (self.unzipped_dir / f"NHDPlus{v['DrainageAreaID']}" / f"NHDPlus{v['UnitID']}").rglob("Catchment.shp")
#         )]
#         if missing: self.download_and_unzip(missing, ["NHDPlusCatchment"])
#         c_paths = list(self.unzipped_dir.rglob("Catchment.shp"))
#         if not c_paths: return
#         catchments = pd.concat([gpd.read_file(p) for p in c_paths]).to_crs(epsg=self.target_epsg)
#         if intersect_only:
#             catchments = gpd.sjoin(catchments, self.user_gdf, how="inner", predicate="intersects")
#             cols_to_drop = [c for c in catchments.columns if 'index' in c.lower() or c.lower() == 'fid']
#             catchments = catchments.drop(columns=cols_to_drop).reset_index(drop=True)
#         else:
#             catchments = gpd.clip(catchments, self.user_gdf)
#         catchments.to_file(self.output_root / "Catchments.gpkg", driver="GPKG")
#         self.working_boundary = catchments.dissolve()
#
#     def process_flowlines(self):
#         missing = [v for v in self.vpus if not list(
#             (self.unzipped_dir / f"NHDPlus{v['DrainageAreaID']}" / f"NHDPlus{v['UnitID']}").rglob("NHDFlowline.shp")
#         )]
#         if missing: self.download_and_unzip(missing, ["NHDPlusAttributes", "NHDSnapshot"])
#         f_paths = list(self.unzipped_dir.rglob("NHDFlowline.shp"))
#         if not f_paths: return
#         flowlines = pd.concat([gpd.read_file(p) for p in f_paths]).to_crs(epsg=self.target_epsg)
#         flowlines = gpd.clip(flowlines, self.working_boundary)
#         flowlines = self._join_attributes(flowlines, self.unzipped_dir)
#         flowlines.to_file(self.output_root / "Flowlines.gpkg", driver="GPKG")
#         find_headwater_points(flowlines).to_file(self.output_root / "Headwaters.gpkg", driver="GPKG")
#
#     def process_dem(self):
#         DEMProcessor(self.working_boundary, output_dir=str(self.output_root / "DEM"), epsg=self.target_epsg)
#
#     def run_fema(self):
#         DownloadFEMANFHL(
#             boundary=self.working_boundary,
#             output_path=str(self.output_root / "FEMA" / f"fema_nfhl_{self.folder_name}.gpkg")
#         )
#
#     def run_full_pipeline(self):
#         print(f"Starting optimized full pipeline for {self.folder_name}...")
#         try:
#             self.process_catchments(intersect_only=True)
#             self.process_dem()
#             self.process_flowlines()
#             self.run_fema()
#             print(f"Done. Outputs in {self.output_root}")
#         except Exception as e:
#             self.logger.error(f"Pipeline failed: {e}", exc_info=True)
