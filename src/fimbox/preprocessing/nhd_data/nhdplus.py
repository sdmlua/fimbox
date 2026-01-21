"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date created: Jan 2026

Description: Main pipeline to download, process, and prepare NHDPlus data. 
Optimized for rocket-speed S3 retrieval and hydrological boundary alignment.
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
from typing import List, Dict, Optional
from urllib.request import Request, urlopen

# Importing Utilities
from .utils import *
from .dem_process import DEMProcessor

# Import NFHL downloader
from .nfhl_data import DownloadFEMANFHL

class getNHDPlusData:
    def __init__(
        self, 
        NHDglobalBoundary: str,     # Contains all NHDPlus VPU/RPU boundaries
        inputs_dir: Optional[str] = None,
        boundary_path: Optional[str] = None, 
        huc8: Optional[str] = None,
        epsg: Optional[int] = None,
        out_dir: Optional[str] = None,
        auto_run: bool = True
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
        self.primary_drainage = self.vpus[0]['DrainageAreaID']
        self.target_epsg = 5070 if self.primary_drainage != "PI" else (epsg or 6637)

        # Load user boundary for initial spatial filtering
        if self.boundary_path:
            self.user_gdf = gpd.read_file(self.boundary_path).to_crs(epsg=self.target_epsg)
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
        file_handler = logging.FileHandler(log_file, mode='w')
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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
            with urlopen(req, timeout=10): return True
        except: return False

    def _get_nhd_url(self, drainage: str, unit_id: str, component: str) -> str:
        """Rocket-optimized URL finder. Stops immediately when a valid link is found."""
        base = "https://dmap-data-commons-ow.s3.amazonaws.com/NHDPlusV21/Data"
        # Most frequent versions found in NHDPlus V21
        priority_versions = ["01", "02", "03", "04", "07", "09"]
        
        # Check primary directory then subfolder directory
        prefixes = [f"{base}/NHDPlus{drainage}", f"{base}/NHDPlus{drainage}/NHDPlus{unit_id}"]
        
        # First check priority versions (Fast Path)
        for vv in priority_versions:
            for pre in prefixes:
                url = f"{pre}/NHDPlusV21_{drainage}_{unit_id}_{component}_{vv}.7z"
                try:
                    with requests.head(url, timeout=5) as r:
                        if r.status_code == 200: return url
                except: continue

        # Fallback: full iteration if priority fails
        for vv in range(30, 0, -1):
            vv_str = f"{vv:02d}"
            if vv_str in priority_versions: continue 
            for pre in prefixes:
                url = f"{pre}/NHDPlusV21_{drainage}_{unit_id}_{component}_{vv_str}.7z"
                try:
                    with requests.head(url, timeout=2) as r:
                        if r.status_code == 200: return url
                except: continue
        return ""

    def download_and_unzip(self, units: List[Dict], components: List[str]):
        """Streams downloads for specific units/components."""
        for vpu in units:
            for comp in components:
                url = self._get_nhd_url(vpu['DrainageAreaID'], vpu['UnitID'], comp)
                if not url: continue
                
                zip_path = self.raw_zip_dir / os.path.basename(url)
                if not zip_path.exists():
                    self.logger.info(f"Downloading {url}...")
                    with requests.get(url, stream=True) as r:
                        r.raise_for_status()
                        with open(zip_path, 'wb') as f:
                            shutil.copyfileobj(r.raw, f) # Fastest streaming method
                
                self.logger.info(f"Extracting {zip_path.name}...")
                with py7zr.SevenZipFile(zip_path, mode='r') as z:
                    z.extractall(path=self.unzipped_dir)

    def _pick_field(self, df, candidates: List[str]) -> str:
        """Case-insensitive column picker."""
        cols = {c.lower(): c for c in df.columns}
        for cand in candidates:
            if cand.lower() in cols: return cols[cand.lower()]
        return ""

    def _join_attributes(self, flowlines: gpd.GeoDataFrame, unzipped_path: Path) -> gpd.GeoDataFrame:
        """Joins DBF attributes to flowlines."""
        vaa_p = list(unzipped_path.rglob("PlusFlowlineVAA.dbf"))
        flow_p = list(unzipped_path.rglob("PlusFlow.dbf"))
        if not vaa_p or not flow_p: return flowlines
        vaa = pd.concat([gpd.read_file(p) for p in vaa_p])
        flow = pd.concat([gpd.read_file(p) for p in flow_p])
        c_f, v_c_f = self._pick_field(flowlines, ["ComID", "COMID"]), self._pick_field(vaa, ["ComID", "COMID"])
        s_f, f_f, t_f = self._pick_field(vaa, ["StreamOrde"]), self._pick_field(flow, ["FROMCOMID"]), self._pick_field(flow, ["TOCOMID"])
        flowlines = flowlines.merge(vaa[[v_c_f, s_f]], left_on=c_f, right_on=v_c_f, how='left')
        flowlines = flowlines.merge(flow[[f_f, t_f]], left_on=c_f, right_on=f_f, how='left')
        return flowlines.rename(columns={c_f: 'ID', t_f: 'to', s_f: 'order_'})

    def process_catchments(self, intersect_only: bool = True):
        """Processes catchments and updates the working boundary to the hydrological footprint."""
        missing = []
        for vpu in self.vpus:
            upath = self.unzipped_dir / f"NHDPlus{vpu['DrainageAreaID']}" / f"NHDPlus{vpu['UnitID']}"
            if not list(upath.rglob("Catchment.shp")): missing.append(vpu)
        if missing: self.download_and_unzip(missing, ["NHDPlusCatchment"])
        
        c_paths = list(self.unzipped_dir.rglob("Catchment.shp"))
        if not c_paths: return
        catchments = pd.concat([gpd.read_file(p) for p in c_paths]).to_crs(epsg=self.target_epsg)
        
        if intersect_only:
            # Get full catchments that touch the user boundary
            self.logger.info("Aligning catchments to Hydrological footprint...")
            catchments = gpd.sjoin(catchments, self.user_gdf, how="inner", predicate="intersects")
            
            # --- FIX: Remove 'fid' and index columns to prevent GPKG save error ---
            cols_to_drop = [c for c in catchments.columns if 'index' in c.lower() or c.lower() == 'fid']
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
            upath = self.unzipped_dir / f"NHDPlus{vpu['DrainageAreaID']}" / f"NHDPlus{vpu['UnitID']}"
            if not list(upath.rglob("NHDFlowline.shp")): missing.append(vpu)
        if missing: self.download_and_unzip(missing, ["NHDPlusAttributes", "NHDSnapshot"])
        
        f_paths = list(self.unzipped_dir.rglob("NHDFlowline.shp"))
        if not f_paths: return
        
        flowlines = pd.concat([gpd.read_file(p) for p in f_paths]).to_crs(epsg=self.target_epsg)
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
        DEMProcessor(self.working_boundary, output_dir=str(self.output_root / "DEM"), epsg=self.target_epsg)

    def run_fema(self):
        """Triggers FEMA NFHL download based on hydrological boundary."""
        self.logger.info("Initiating FEMA NFHL extraction...")
        DownloadFEMANFHL(
            boundary=self.working_boundary, 
            output_path=str(self.output_root / "FEMA" / f"fema_nfhl_{self.folder_name}.gpkg")
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
            print(f"Error occurred. Refer to the log: {self.output_root / f'{self.folder_name}_pipeline.log'}")

# CLI Support
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Acquire and Preprocess NHDPlus Data.")
    parser.add_argument("-g", "--global_boundary", required=True, help="Path to NHDPlus Global BoundaryUnit.shp")
    parser.add_argument("-b", "--boundary", help="Path to user defined boundary file (e.g., .shp, .gpkg)")
    parser.add_argument("-u", "--huc8", help="USGS HUC8 ID (8 digits)")
    parser.add_argument("-i", "--inputs_dir", help="Main directory to store inputs/outputs (optional)")
    parser.add_argument("-o", "--out_dir", help="Direct output path for this specific run (optional)")
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
        out_dir=args.out_dir
    )