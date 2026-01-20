"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date: Jan 2026

Description: Main pipeline to download, process, and prepare NHDPlus data- Flowlines, Catchments, DEM, Waterbodies
"""

import os
import sys
import py7zr
import shutil
import geopandas as gpd
import pandas as pd
from pathlib import Path
from typing import List, Dict, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

#Importing Utilities
from .utils import *

# Assuming these are available in your environment/project structure
# from data.nfhl.download_fema_nfhl import download_nfhl_wrapper

class getNHDPlusData:
    def __init__(
        self, 
        boundary_path: str, 
        NHDglobalBoundary: str,     #Contains all NHDPlus VPU/RPU boundaries
        inputs_dir: str,
        huc8: Optional[str] = None,
        epsg: Optional[int] = None
    ):
        self.boundary_path = Path(boundary_path)
        self.NHDglobalBoundary = Path(NHDglobalBoundary)
        self.inputs_dir = Path(inputs_dir)
        self.huc8 = huc8
        
        # Create output folder based on boundary name
        self.folder_name = self.boundary_path.stem
        self.output_root = self.inputs_dir / self.folder_name
        self.raw_zip_dir = self.output_root / "raw_zips"
        
        for d in [self.output_root, self.raw_zip_dir]:
            d.mkdir(parents=True, exist_ok=True)

        #Identify VPUs and RPUs using your Finder
        finder = NHDBoundaryFinder(str(self.boundary_path), str(self.NHDglobalBoundary))
        self.vpus = finder.vpus
        self.rpus = finder.rpus
        
        # Determine target CRS
        self.drainage_id = self.vpus[0]['DrainageAreaID'] if self.vpus else "MS"
        self.target_epsg = 5070 if self.drainage_id != "PI" else (epsg or 6637)

        #Load user boundary for clipping
        self.user_gdf = gpd.read_file(self.boundary_path).to_crs(epsg=self.target_epsg)

    def _url_exists(self, url: str) -> bool:
        try:
            req = Request(url, method="HEAD")
            with urlopen(req, timeout=10): return True
        except: return False

    def _get_nhd_url(self, drainage: str, unit_id: str, component: str) -> str:
        base = "https://dmap-data-commons-ow.s3.amazonaws.com/NHDPlusV21/Data"

        # Components: NHDPlusAttributes, NHDPlusCatchment, NHDSnapshot, WBDSnapshot
        """
        This will be directly retrieved from the NHDPlusV21 S3 storage: https://dmap-data-commons-ow.s3.amazonaws.com/NHDPlusV21/Documentation/Metadata/NHDPlusV2_metadata.htm
        For More information: https://www.epa.gov/waterdata/get-nhdplus-national-hydrography-dataset-plus-data
        """
        for vv in range(30, 0, -1):
            url = f"{base}/NHDPlus{drainage}/NHDPlusV21_{drainage}_{unit_id}_{component}_{vv:02d}.7z"
            if self._url_exists(url): return url
        return ""

    def download_and_unzip(self):
        """Downloads all intersecting VPU/RPU components and unzips them."""

        # Note: RPUs are used for Raster/Hydrodem, but Attributes/Flowlines are by VPU
        targets = []
        for vpu in self.vpus:
            for comp in ["NHDPlusAttributes", "NHDSnapshot", "WBDSnapshot", "NHDPlusCatchment"]:
                targets.append((vpu['UnitID'], comp))
        
        for unit_id, comp in targets:
            url = self._get_nhd_url(self.drainage_id, unit_id, comp)
            if not url: continue
            
            zip_path = self.raw_zip_dir / os.path.basename(url)
            if not zip_path.exists():
                print(f"Downloading {url}...")
                os.system(f'wget -q -O "{zip_path}" "{url}"')
            
            with py7zr.SevenZipFile(zip_path, mode='r') as z:
                z.extractall(path=self.output_root / "unzipped")

    def process_vector_data(self):
        """Merges, clips, and processes Flowlines and Catchments."""
        unzipped_path = self.output_root / "unzipped"
        
        # Process Flowlines
        flowline_paths = list(unzipped_path.rglob("NHDFlowline.shp"))
        flowlines = pd.concat([gpd.read_file(p) for p in flowline_paths]).to_crs(epsg=self.target_epsg)
        flowlines = gpd.clip(flowlines, self.user_gdf)
        
        # Join Attributes
        flowlines = self._join_attributes(flowlines, unzipped_path)

        # Process Catchments
        catchment_paths = list(unzipped_path.rglob("Catchment.shp"))
        catchments = pd.concat([gpd.read_file(p) for p in catchment_paths]).to_crs(epsg=self.target_epsg)
        catchments = gpd.clip(catchments, self.user_gdf)
        
        # 4. Optional HUC8 Filter
        if self.huc8:
            # Filter if a HUC8 is provided
            pass 

        # Save Outcomes
        flowlines.to_file(self.output_root / "Flowlines.gpkg", driver="GPKG")
        catchments.to_file(self.output_root / "Catchments.gpkg", driver="GPKG")
        
        # Trigger Headwaters (Assuming findHeadWaterPoints is imported)
        # hw = findHeadWaterPoints(flowlines)
        # hw.to_file(self.output_root / "Headwaters.gpkg", driver="GPKG")

    def run_fema(self):
        """Triggers FEMA NFHL download if HUC8 is available or from bounds."""
        huc_list = [self.huc8] if self.huc8 else [] 
        # download_nfhl_wrapper(huc_list=huc_list, output_folder=self.output_root / "FEMA")

    def run_full_pipeline(self):
        """Main orchestrator."""
        print(f"Starting pipeline for {self.folder_name}...")
        
        # DEM handled by your external module
        print("Processing DEM via DEMProcessor...")
        DEMProcessor(str(self.boundary_path), output_dir=str(self.output_root / "DEM"), epsg=self.target_epsg)
        
        self.download_and_unzip()
        self.process_vector_data()
        self.run_fema()
        print(f"Done. Outputs in {self.output_root}")

# ---------------------------------------------------------
# Support Class (Already provided by you, integrated here)
# ---------------------------------------------------------
class NHDBoundaryFinder:
    def __init__(self, user_boundary_path, global_boundary_path):
        nhd_gdf = gpd.read_file(global_boundary_path)
        user_gdf = gpd.read_file(user_boundary_path)
        if user_gdf.crs != nhd_gdf.crs:
            user_gdf = user_gdf.to_crs(nhd_gdf.crs)
        intersected = gpd.sjoin(nhd_gdf, user_gdf, how="inner", predicate="intersects")
        unique_units = intersected.drop_duplicates(subset=['UnitID'])
        self.vpus, self.rpus = [], []
        for _, row in unique_units.iterrows():
            if row['UnitType'] == 'VPU':
                self.vpus.append({
                    "DrainageAreaID": row['DrainageID'], "UnitID": row['UnitID'],
                    "UnitName": str(row['UnitName']).replace(" ", "").replace("-", "")
                })
            elif row['UnitType'] == 'RPU':
                self.rpus.append({"DrainageAreaID": row['DrainageID'], "UnitID": row['UnitID']})