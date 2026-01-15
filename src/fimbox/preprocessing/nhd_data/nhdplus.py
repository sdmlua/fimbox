import os
import ssl
import geopandas as gpd
from pynhd import WaterData, NHDPlusHR
from shapely.validation import make_valid

# Configuration
OUTPUT_DIR = "/Users/Supath/Downloads/SDML/FIMBOX/Output_Hydro_Data1"
BOUNDARY_PATH = "/Users/Supath/Downloads/SDML/FIMBOX/Sample_Data/SampleBoundary.shp"

# SSL and Path Setup
ssl._create_default_https_context = ssl._create_unverified_context
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

def download_hydro_data():
    # 1. Load Boundary
    gdf = gpd.read_file(BOUNDARY_PATH)
    gdf_4326 = gdf.to_crs("EPSG:4326")
    geom = gdf_4326.geometry.union_all()

    print(f"--- Processing Boundary ---")

    # 2. Permanent Waterbodies (Lakes, Reservoirs)
    try:
        print("Downloading Waterbodies...")
        wb_service = WaterData("nhdwaterbody")  # Query waterbodies layer
        waterbodies = wb_service.bygeom(geom)
        if not waterbodies.empty:
            waterbodies_path = os.path.join(OUTPUT_DIR, "nhd_waterbodies.gpkg")
            waterbodies.to_file(waterbodies_path, driver="GPKG")
            print(f"✅ Saved {len(waterbodies)} Waterbodies to {waterbodies_path}")
        else:
            print("⚠️ No Waterbodies found for this area.")
    except Exception as e:
        print(f"× Waterbody Error: {e}")

    # 3. NHDPlus HR Catchments - Download once
    try:
        print("Downloading NHDPlus HR Catchments...")
        catchments_service = NHDPlusHR("catchment")  # Query high-resolution catchments layer
        catchments = catchments_service.bygeom(geom)

        if not catchments.empty:
            catchments_path = os.path.join(OUTPUT_DIR, "nhdplus_hr_catchments.gpkg")
            catchments.to_file(catchments_path, driver="GPKG")
            print(f"✅ Saved {len(catchments)} NHDPlus HR Catchments to {catchments_path}")

            # Use the entire catchment geometry to query NHDPlus HR flowlines
            print("Validating and fixing geometries...")
            catchments["geometry"] = catchments["geometry"].apply(
                lambda geom: make_valid(geom) if not geom.is_valid else geom
            )
            catchments = catchments[catchments.is_valid]  # Remove invalid geometries

            if len(catchments) == 0:
                print("⚠️ No valid catchments available for flowline query")
                return

            flowline_geom = catchments.geometry.union_all()  # Combine all catchment geometries
        else:
            print("⚠️ No NHDPlus HR Catchments found for this area.")
            return
    except Exception as e:
        print(f"× NHDPlus HR Catchment Error: {e}")
        return

    # 4. NHDPlus HR Flowlines
    try:
        print("Downloading NHDPlus HR Flowlines...")
        nhd_hr_service = NHDPlusHR("flowline")  # Correct high-resolution flowlines layer
        flowlines = nhd_hr_service.bygeom(flowline_geom)  # Query flowlines by the geometry
        if not flowlines.empty:
            flowlines_path = os.path.join(OUTPUT_DIR, "nhd_flowlines_hr.gpkg")
            flowlines.to_file(flowlines_path, driver="GPKG")
            print(f"✅ Saved {len(flowlines)} NHDPlus HR Flowlines to {flowlines_path}")
        else:
            print("⚠️ No Flowlines found for the provided area.")
    except Exception as e:
        print(f"× NHDPlus HR Flowline Error: {e}")

if __name__ == "__main__":
    download_hydro_data()