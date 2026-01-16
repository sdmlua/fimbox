### DEMProcessor
<hr style="border: 1px solid blue;">

**DEMProcessor** is a robust Python module designed to automate the retrieval, pre-processing, and standardization of Digital Elevation Model (DEM) data. It simplifies geospatial workflows by managing coordinate systems, resolutions, and cropping automatically.

It can,

**Automated 3DEP Retrieval:** Fetches authoritative USGS 3DEP elevation data for any defined boundary within the US in different spatial resolution of 10, 20, \& 60m. 

**Local File Support:** Seamlessly processes local DEM raster files for regions outside the US or offline workflows in case of user wants to work on their own dataset.

### Usage of DEMProcessor
<hr style="border: 1px solid blue;">

```bash
import fimbox

# Automates fetching, projecting to UTM, and clipping
fimbox.DEMProcessor(
    boundary=boundary,
    resolution=10,                     #desired DEM resolution in meters (10, 30, 60), for 3DEP fetch and other 
    output_dir="./dem_test"
    # layer: Optional[str] = None,     #if boundary is geopackage with multiple layers
    # dem_file: Optional[str] = None,  #path to local DEM file if available or outside CONUS  
    # epsg: Optional[int] = None       #desired output CRS EPSG code for projection, if None auto-detects UTM zone 
)
```