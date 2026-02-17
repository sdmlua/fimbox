"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date created: Jan 2026
Data updated: Feb 9,2025

Description: Main pipeline to download, process, and prepare NHDPlus data.
Optimized for speed S3 retrieval and hydrological boundary alignment.
"""

import os
import sys
import py7zr
import shutil
import logging
import argparse
import requests
from pynhd import WaterData
import geopandas as gpd
import pandas as pd
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Sequence, Union, Literal, Any
from urllib.request import Request, urlopen
import math
import json
from dataclasses import dataclass
from shapely.geometry import Polygon, MultiPolygon, box
from shapely.ops import unary_union

# Importing Utilities
from .utils import *
from .dem_process import DEMProcessor

# Import NFHL downloader
from .nfhl_data import DownloadFEMANFHL


# THIS IS THE CODE WHICH DOWNLOAD AND PROCESSED DATA FROM THE Environmental Protection Agency (EPA) National Hydrography Dataset Plus (NHDPlus) Version 2.1,
class getNHDPlusData:
    def __init__(
        self,
        NHDglobalBoundary: str,  # Contains all NHDPlus VPU/RPU boundaries
        inputs_dir: Optional[str] = None,
        boundary_path: Optional[str] = None,
        huc8: Optional[str] = None,
        epsg: Optional[int] = None,
        out_dir: Optional[str] = None,
        auto_run: bool = True,
    ):
        self.boundary_path = Path(boundary_path) if boundary_path else None
        self.NHDglobalBoundary = Path(NHDglobalBoundary)
        self.huc8 = huc8

        # Create output folder based on boundary name
        self.folder_name = self.boundary_path.stem if self.boundary_path else self.huc8

        # Logic for Output Root: Priority -> out_dir, then inputs_dir/folder, then CWD/folder
        if out_dir:
            self.output_root = Path(out_dir)
        elif inputs_dir:
            self.output_root = Path(inputs_dir) / self.folder_name
        else:
            self.output_root = Path.cwd() / self.folder_name

        self.raw_zip_dir = self.output_root / "raw_zips"
        self.unzipped_dir = self.output_root / "unzipped"

        for d in [self.output_root, self.raw_zip_dir, self.unzipped_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Setup Logger
        self._setup_logger()

        # Identify boundary and units
        if self.boundary_path:
            finder_input = str(self.boundary_path)
        elif self.huc8:
            self.logger.info(f"Extracting HUC8 boundary for {self.huc8} via pynhd...")
            # Getting geometry file for the HUC8 from the WBD layers using WaterData service
            wd = WaterData("wbd08")
            self.user_gdf = wd.byfilter(f"huc8 = '{self.huc8}'")
            finder_input = self.user_gdf
        else:
            raise ValueError("Either boundary_path or huc8 must be provided.")

        finder = NHDBoundaryFinder(finder_input, str(self.NHDglobalBoundary))
        self.vpus = finder.vpus
        self.rpus = finder.rpus

        if not self.vpus:
            self.logger.error("No intersecting VPUs found for the provided boundary.")
            raise ValueError("Boundary does not intersect any NHDPlus VPU units.")

        # Determine target CRS
        self.primary_drainage = self.vpus[0]["DrainageAreaID"]
        self.target_epsg = 5070 if self.primary_drainage != "PI" else (epsg or 6637)

        # Load user boundary for initial spatial filtering
        if self.boundary_path:
            self.user_gdf = gpd.read_file(self.boundary_path).to_crs(
                epsg=self.target_epsg
            )
        else:
            self.user_gdf = self.user_gdf.to_crs(epsg=self.target_epsg)
            # Save the HUC8 boundary for future reference
            huc_save_path = self.output_root / f"HUC8_{self.huc8}_boundary.gpkg"
            self.user_gdf.to_file(huc_save_path, driver="GPKG")

        # Internal state for processing logic (Working boundary updated after catchment extraction)
        self.working_boundary = self.user_gdf

        # Trigger the full pipeline in normal situations
        if auto_run:
            self.run_full_pipeline()

    def _setup_logger(self):
        """Initializes logging to a single file and captures sub-module logs."""
        log_file = self.output_root / f"{self.folder_name}_pipeline.log"
        self.logger = logging.getLogger(self.folder_name)
        self.logger.setLevel(logging.INFO)

        if self.logger.hasHandlers():
            self.logger.handlers.clear()

        # File handler in 'w' mode replaces the log file on each run
        file_handler = logging.FileHandler(log_file, mode="w")
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)

        # Consolidate logs: Attach this file handler to sub-module loggers
        sub_modules = ["DEMProcessor", "FEMADownloader", "pynhd"]
        for module_name in sub_modules:
            sub_logger = logging.getLogger(module_name)
            sub_logger.setLevel(logging.INFO)
            sub_logger.addHandler(file_handler)
            sub_logger.propagate = False

    def _url_exists(self, url: str) -> bool:
        try:
            req = Request(url, method="HEAD")
            with urlopen(req, timeout=10):
                return True
        except:
            return False

    def _get_nhd_url(self, drainage: str, unit_id: str, component: str) -> str:
        """Rocket-optimized URL finder. Stops immediately when a valid link is found."""
        base = "https://dmap-data-commons-ow.s3.amazonaws.com/NHDPlusV21/Data"
        # Most frequent versions found in NHDPlus V21
        priority_versions = ["01", "02", "03", "04", "07", "09"]

        # Check primary directory then subfolder directory
        prefixes = [
            f"{base}/NHDPlus{drainage}",
            f"{base}/NHDPlus{drainage}/NHDPlus{unit_id}",
        ]

        # First check priority versions (Fast Path)
        for vv in priority_versions:
            for pre in prefixes:
                url = f"{pre}/NHDPlusV21_{drainage}_{unit_id}_{component}_{vv}.7z"
                try:
                    with requests.head(url, timeout=5) as r:
                        if r.status_code == 200:
                            return url
                except:
                    continue

        # Fallback: full iteration if priority fails
        for vv in range(30, 0, -1):
            vv_str = f"{vv:02d}"
            if vv_str in priority_versions:
                continue
            for pre in prefixes:
                url = f"{pre}/NHDPlusV21_{drainage}_{unit_id}_{component}_{vv_str}.7z"
                try:
                    with requests.head(url, timeout=2) as r:
                        if r.status_code == 200:
                            return url
                except:
                    continue
        return ""

    def download_and_unzip(self, units: List[Dict], components: List[str]):
        """Streams downloads for specific units/components."""
        for vpu in units:
            for comp in components:
                url = self._get_nhd_url(vpu["DrainageAreaID"], vpu["UnitID"], comp)
                if not url:
                    continue

                zip_path = self.raw_zip_dir / os.path.basename(url)
                if not zip_path.exists():
                    self.logger.info(f"Downloading {url}...")
                    with requests.get(url, stream=True) as r:
                        r.raise_for_status()
                        with open(zip_path, "wb") as f:
                            shutil.copyfileobj(r.raw, f)  # Fastest streaming method

                self.logger.info(f"Extracting {zip_path.name}...")
                with py7zr.SevenZipFile(zip_path, mode="r") as z:
                    z.extractall(path=self.unzipped_dir)

    def _pick_field(self, df, candidates: List[str]) -> str:
        """Case-insensitive column picker."""
        cols = {c.lower(): c for c in df.columns}
        for cand in candidates:
            if cand.lower() in cols:
                return cols[cand.lower()]
        return ""

    def _join_attributes(
        self, flowlines: gpd.GeoDataFrame, unzipped_path: Path
    ) -> gpd.GeoDataFrame:
        """Joins DBF attributes to flowlines."""
        vaa_p = list(unzipped_path.rglob("PlusFlowlineVAA.dbf"))
        flow_p = list(unzipped_path.rglob("PlusFlow.dbf"))
        if not vaa_p or not flow_p:
            return flowlines
        vaa = pd.concat([gpd.read_file(p) for p in vaa_p])
        flow = pd.concat([gpd.read_file(p) for p in flow_p])
        c_f, v_c_f = self._pick_field(flowlines, ["ComID", "COMID"]), self._pick_field(
            vaa, ["ComID", "COMID"]
        )
        s_f, f_f, t_f = (
            self._pick_field(vaa, ["StreamOrde"]),
            self._pick_field(flow, ["FROMCOMID"]),
            self._pick_field(flow, ["TOCOMID"]),
        )
        flowlines = flowlines.merge(
            vaa[[v_c_f, s_f]], left_on=c_f, right_on=v_c_f, how="left"
        )
        flowlines = flowlines.merge(
            flow[[f_f, t_f]], left_on=c_f, right_on=f_f, how="left"
        )
        return flowlines.rename(columns={c_f: "ID", t_f: "to", s_f: "order_"})

    def process_catchments(self, intersect_only: bool = True):
        """Processes catchments and updates the working boundary to the hydrological footprint."""
        missing = []
        for vpu in self.vpus:
            upath = (
                self.unzipped_dir
                / f"NHDPlus{vpu['DrainageAreaID']}"
                / f"NHDPlus{vpu['UnitID']}"
            )
            if not list(upath.rglob("Catchment.shp")):
                missing.append(vpu)
        if missing:
            self.download_and_unzip(missing, ["NHDPlusCatchment"])

        c_paths = list(self.unzipped_dir.rglob("Catchment.shp"))
        if not c_paths:
            return
        catchments = pd.concat([gpd.read_file(p) for p in c_paths]).to_crs(
            epsg=self.target_epsg
        )

        if intersect_only:
            # Get full catchments that touch the user boundary
            self.logger.info("Aligning catchments to Hydrological footprint...")
            catchments = gpd.sjoin(
                catchments, self.user_gdf, how="inner", predicate="intersects"
            )

            # FIX: Remove 'fid' and index columns to prevent GPKG save error
            cols_to_drop = [
                c
                for c in catchments.columns
                if "index" in c.lower() or c.lower() == "fid"
            ]
            catchments = catchments.drop(columns=cols_to_drop).reset_index(drop=True)
        else:
            catchments = gpd.clip(catchments, self.user_gdf)

        catchments.to_file(self.output_root / "Catchments.gpkg", driver="GPKG")

        # Update working boundary to the dissolved catchment outline for subsequent steps
        self.working_boundary = catchments.dissolve()
        self.logger.info("Working boundary updated to dissolved catchment footprint.")

    def process_flowlines(self):
        """Processes flowlines, downloading only missing unit data."""
        missing = []
        for vpu in self.vpus:
            upath = (
                self.unzipped_dir
                / f"NHDPlus{vpu['DrainageAreaID']}"
                / f"NHDPlus{vpu['UnitID']}"
            )
            if not list(upath.rglob("NHDFlowline.shp")):
                missing.append(vpu)
        if missing:
            self.download_and_unzip(missing, ["NHDPlusAttributes", "NHDSnapshot"])

        f_paths = list(self.unzipped_dir.rglob("NHDFlowline.shp"))
        if not f_paths:
            return

        flowlines = pd.concat([gpd.read_file(p) for p in f_paths]).to_crs(
            epsg=self.target_epsg
        )
        # Clip to the catchment-dissolved footprint
        flowlines = gpd.clip(flowlines, self.working_boundary)
        flowlines = self._join_attributes(flowlines, self.unzipped_dir)
        flowlines.to_file(self.output_root / "Flowlines.gpkg", driver="GPKG")

        # Calculate and save headwaters
        hw = find_headwater_points(flowlines)
        hw.to_file(self.output_root / "Headwaters.gpkg", driver="GPKG")
        self.logger.info("Flowlines and Headwaters processed successfully.")

    def process_dem(self):
        """Standalone module to extract and process the DEM Dataset."""
        self.logger.info("Initiating DEM extraction for Hydrological footprint...")
        DEMProcessor(
            self.working_boundary,
            output_dir=str(self.output_root / "DEM"),
            epsg=self.target_epsg,
        )

    def run_fema(self):
        """Triggers FEMA NFHL download based on hydrological boundary."""
        self.logger.info("Initiating FEMA NFHL extraction...")
        DownloadFEMANFHL(
            boundary=self.working_boundary,
            output_path=str(
                self.output_root / "FEMA" / f"fema_nfhl_{self.folder_name}.gpkg"
            ),
        )

    def run_full_pipeline(self):
        """Main orchestrator that triggers all separate modules."""
        print(f"Starting optimized full pipeline for {self.folder_name}...")
        try:
            print("--> Extracting Intersecting Catchments")
            self.process_catchments(intersect_only=True)

            print("--> Processing DEM")
            self.process_dem()

            print("--> Processing Flowlines")
            self.process_flowlines()

            print("--> Downloading FEMA NFHL Data")
            self.run_fema()

            print(f"Done. Outputs in {self.output_root}")
            self.logger.info("Full pipeline completed successfully.")
        except Exception as e:
            self.logger.error(f"Pipeline failed: {str(e)}", exc_info=True)
            print(
                f"Error occurred. Refer to the log: {self.output_root / f'{self.folder_name}_pipeline.log'}"
            )


# CLI Support
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Acquire and Preprocess NHDPlus Data.")
    parser.add_argument(
        "-g",
        "--global_boundary",
        required=True,
        help="Path to NHDPlus Global BoundaryUnit.shp",
    )
    parser.add_argument(
        "-b",
        "--boundary",
        help="Path to user defined boundary file (e.g., .shp, .gpkg)",
    )
    parser.add_argument("-u", "--huc8", help="USGS HUC8 ID (8 digits)")
    parser.add_argument(
        "-i", "--inputs_dir", help="Main directory to store inputs/outputs (optional)"
    )
    parser.add_argument(
        "-o", "--out_dir", help="Direct output path for this specific run (optional)"
    )
    parser.add_argument("-e", "--epsg", type=int, help="Target EPSG code (optional)")

    args = parser.parse_args()

    if not args.boundary and not args.huc8:
        print("Error: You must provide either a --boundary path or a --huc8 ID.")
        sys.exit(1)

    getNHDPlusData(
        NHDglobalBoundary=args.global_boundary,
        inputs_dir=args.inputs_dir,
        boundary_path=args.boundary,
        huc8=args.huc8,
        epsg=args.epsg,
        out_dir=args.out_dir,
    )

# THIS IS THE CODE FROM THE ARCGIS ONLINE USING ARCGIS REST API, WHICH PROCESSES THE DATA DOWNLOAD MUCH FASTER
# National Water Model datasets
ClipMode = Literal["intersect", "hard"]


@dataclass
class ArcGISDownloader:
    """
    General ArcGIS FeatureServer layer downloader.

    Features:
      - boundary filter (server-side spatialRel)
      - paging via resultOffset/resultRecordCount
      - optional hard crop locally (gpd.clip)
      - saves to file if requested
    """

    layer_url: str
    layer_id: int = 0
    out_sr: int = 5070
    page_size: int = 2000
    timeout: int = 120
    debug: bool = False

    def log(self, msg: str) -> None:
        if self.debug:
            print(f"[ArcGISDownloader] {msg}")

    def _normalize_layer_url(self) -> str:
        u = self.layer_url.rstrip("/")
        if u.lower().endswith("/featureserver"):
            u = f"{u}/{self.layer_id}"
        return u

    @property
    def query_url(self) -> str:
        return f"{self._normalize_layer_url()}/query"

    def _read_boundary_file(
        self, p: Path, boundary_layer: Optional[str]
    ) -> gpd.GeoDataFrame:
        if p.suffix.lower() == ".gpkg":
            if boundary_layer:
                return gpd.read_file(p, layer=boundary_layer)
            layers = gpd.list_layers(p)
            if len(layers) == 0:
                raise ValueError(f"No layers found in {p}")
            return gpd.read_file(p, layer=layers.iloc[0]["name"])
        return gpd.read_file(p)

    def _boundary_to_geom4326(
        self,
        boundary: Any,
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[int] = None,
    ):
        """Return shapely (Polygon/MultiPolygon) in EPSG:4326."""
        if isinstance(boundary, (str, Path)):
            gdf = self._read_boundary_file(Path(boundary), boundary_layer)
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

        return geom

    def _geom_to_esri_rings(self, geom) -> Dict[str, Any]:
        """Convert Polygon/MultiPolygon to ESRI JSON rings (NO simplification)."""
        if geom.geom_type == "Polygon":
            rings = [[[float(x), float(y)] for x, y in geom.exterior.coords]]
            return {"rings": rings}

        if geom.geom_type == "MultiPolygon":
            rings = []
            for poly in geom.geoms:
                rings.append([[float(x), float(y)] for x, y in poly.exterior.coords])
            return {"rings": rings}

        raise ValueError(f"Unsupported boundary geometry type: {geom.geom_type}")

    def _post(self, params: dict) -> dict:
        self.log(f"POST {self.query_url}")
        r = requests.post(self.query_url, data=params, timeout=self.timeout)
        self.log(f"Status {r.status_code}")
        r.raise_for_status()
        return r.json()

    def _count(self, where: str, esri_geom: dict, spatial_rel: str) -> int:
        params = {
            "f": "json",
            "where": where,
            "geometry": json.dumps(esri_geom),
            "geometryType": "esriGeometryPolygon",
            "spatialRel": spatial_rel,
            "inSR": 4326,
            "returnCountOnly": "true",
        }
        return int(self._post(params).get("count", 0))

    def download(
        self,
        boundary: Any,
        *,
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[int] = None,
        where: str = "1=1",
        spatial_rel: str = "esriSpatialRelIntersects",
        out_fields: str = "*",
        return_geometry: bool = True,
        clip_mode: ClipMode = "intersect",  # Intersect for get full, Hard for get only intersected and clip anyhow
        out_dir: Optional[Union[str, Path]] = None,
        out_name: str = "features.gpkg",
        out_layer: str = "features",
        driver: str = "GPKG",
    ) -> gpd.GeoDataFrame:
        """
        Download features that satisfy `where` and intersect the boundary.
        - clip_mode="none": returns full service geometries (no crop)
        - clip_mode="hard": downloads full geometries, then crops with gpd.clip(boundary)
        """
        geom4326 = self._boundary_to_geom4326(boundary, boundary_layer, boundary_crs)
        esri_geom = self._geom_to_esri_rings(geom4326)

        total = self._count(where, esri_geom, spatial_rel)
        self.log(f"Total records: {total}")

        if total == 0:
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        n_pages = math.ceil(total / self.page_size)
        self.log(f"Pages: {n_pages} (page_size={self.page_size})")

        chunks = []
        for i in range(n_pages):
            offset = i * self.page_size
            self.log(f"Page {i+1}/{n_pages} offset={offset}")

            params = {
                "f": "geojson",
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "true" if return_geometry else "false",
                "geometry": json.dumps(esri_geom),
                "geometryType": "esriGeometryPolygon",
                "spatialRel": spatial_rel,
                "inSR": 4326,
                "outSR": self.out_sr,
                "resultOffset": offset,
                "resultRecordCount": self.page_size,
            }

            gj = self._post(params)
            feats = gj.get("features", [])
            if feats:
                gdf = gpd.GeoDataFrame.from_features(feats, crs=f"EPSG:{self.out_sr}")
                chunks.append(gdf)

        if not chunks:
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        result = gpd.GeoDataFrame(
            pd.concat(chunks, ignore_index=True), crs=chunks[0].crs
        )

        # Optional hard crop (local)
        if clip_mode == "hard":
            bnd_gdf = gpd.GeoDataFrame(geometry=[geom4326], crs="EPSG:4326").to_crs(
                self.out_sr
            )
            result = gpd.clip(result, bnd_gdf)

        # Save
        if out_dir:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / out_name
            if driver.upper() == "GPKG":
                result.to_file(out_path, layer=out_layer, driver=driver)
            else:
                result.to_file(out_path, driver=driver)
            self.log(f"Saved -> {out_path} (layer={out_layer})")

        return result


# Wrapper for the NWM Flowline
class NWMFlowlinesDownloader(ArcGISDownloader):
    def __init__(self, **kw):
        super().__init__(
            layer_url="https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services/NWM_FlowLine/FeatureServer",
            layer_id=0,
            out_sr=5070,
            **kw,
        )
    
    def download (self, boundary: Any, **kwargs) -> gpd.GeoDataFrame:
        kwargs.setdefault("clip_mode", "hard")
        kwargs.setdefault("out_name", "nwm_flowlines.gpkg")
        kwargs.setdefault("out_layer", "nwm_flowlines") 
        return super().download(boundary, **kwargs)


# Wrapper for the NWM Catchments
class NWMCatchmentsDownloader(ArcGISDownloader):
    def __init__(self, **kw):
        super().__init__(
            layer_url="https://services.arcgis.com/ts4gk3YgS68yLGFl/arcgis/rest/services/NWM_Catchments/FeatureServer",
            layer_id=0,
            out_sr=5070,
            **kw,
        )
    def download (self, boundary: Any, **kwargs) -> gpd.GeoDataFrame:
        kwargs.setdefault("clip_mode", "intersect")
        kwargs.setdefault("out_name", "nwm_catchments.gpkg")
        kwargs.setdefault("out_layer", "nwm_catchments") 
        return super().download(boundary, **kwargs)
