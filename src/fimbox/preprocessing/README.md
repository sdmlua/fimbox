### Preprocessing
<hr style="border: 1px solid blue;">

**fimbox.preprocessing** turns a boundary polygon (or HUC8 ID) into everything HAND-FIM needs: staged input datasets, hydro-conditioned DEMs, branch-level HAND rasters, synthetic rating curves (SRCs), and calibrated hydroTables. It is the largest module in `fimbox` and is organized as one subpackage per stage.

**Workflow**

The pipeline moves an AOI from a raw boundary to a fully calibrated, FIM-ready dataset in five stages. Each stage writes its outputs into the AOI folder and feeds the next, so the chain can be run end-to-end or stage-by-stage. The entry point is flexible: pass your own boundary polygon **or** just an 8-digit HUC8 ID, and every major dataset can be swapped: 3DEP DEM at any resolution or your own DEM, NWM medium-resolution or NHDPlus High-Resolution hydrography, or your own flowlines/catchments. Bridge DEM processing is optional and only needed when bridge decks should be healed in the HAND raster.

<!-- Diagram source: workflows/preprocessing.mmd - edit that file and regenerate with `make workflows` (see workflows/README.md) -->
<div align="center">
  <img src="../../../workflows/svg/preprocessing.svg" alt="preprocessing workflow" />
</div>

**Module contents**

| Path | What it contains |
|---|---|
| [`download_data/`](download_data/) | Download and standardize all AOI inputs (DEM, NWM/NHDPlus hydrography, FEMA NFHL, NLD levees, OSM roads/bridges, USGS gages). |
| [`huc_test/`](huc_test/) | Validate HUC8 codes against the acceptable HUC lists shipped with the package. |
| [`process_bridgedem/`](process_bridgedem/) | Build per-bridge LiDAR elevation rasters and the bridge/DEM difference raster used to heal bridge decks in HAND. |
| [`calculate_branch/`](calculate_branch/) | Branch derivation and the per-branch HAND workflow: DEM conditioning, flow direction/accumulation, REM/HAND, reach splitting, crosswalk, and SRC/hydroTable generation. |
| [`calibrate_ratingcurve/`](calibrate_ratingcurve/) | SRC calibration: bathymetry, bankfull, channel/overbank subdivision, USGS/spatial/manual calibration, and AOI aggregation. |
| `preprocess_area.py` | `getAllInputData`: the combined download pipeline that stages every AOI input in one call. |
| `source_naming.py` | Filename conventions (`source_name`, `detect_identifier`, `resolve_source`) so alternative hydrography sources can replace NWM transparently. |

**Order of operations**

1. `getAllInputData` stages inputs under `<out_dir>/<aoi_id>/watershed-data/`.
2. (Optional) `generateBridgeRaster` + `BridgeDEMDiff` produce `bridge_elev_diff.tif`.
3. `BranchDerivation` + `calculate_allbranches` produce per-branch HAND rasters, SRCs, and hydroTables.
4. `run_calibration` adjusts and calibrates the SRCs/hydroTables.

### Usage
<hr style="border: 1px solid blue;">

```python
import fimbox

# Stage all AOI inputs from a boundary polygon (or huc8="03020201")
pp = fimbox.getAllInputData(
    boundary="path/to/aoi_boundary.gpkg",   #AOI polygon file (alternative to huc8)
    out_dir="out/my_basin",                 #root output directory (default fimbox_preprocess)
    # huc8: Optional[str] = None,           #8-digit HUC ID instead of a boundary file
    # boundary_layer: Optional[str] = None, #layer name when boundary is a multi-layer gpkg
    # epsg: int = 5070,                     #output CRS (NAD83 CONUS Albers)
    # dem_resolution: int = 10,             #3DEP DEM resolution in meters (1/3/10/30/60)
    # buffer_m: float = 2000.0,             #boundary buffer distance for data downloads
    # headwater_buffer_cells: int = 8,      #DEM cells for the inner headwater clip
    # get_flowlines: bool = True,           #download flowlines (False = bring your own)
    # get_catchments: bool = True,          #download catchments (False = bring your own)
    # resolution: str = "medium",           #"medium" = NWM, "high" = NHDPlus HR
    # flowlines: Optional[Path] = None,     #bring-your-own flowlines file
    # catchments: Optional[Path] = None,    #bring-your-own catchments file
    # stream_fields: Optional[dict] = None, #canonical->user column map for custom streams
    # catchment_fields: Optional[dict] = None, #canonical->user column map for custom catchments
    # identifier: str = "nwm",              #filename prefix for staged hydrography
    # dem: Optional[Path] = None,           #bring-your-own DEM (reprojected/clipped)
)
pp.run()        # full pipeline; or pp.run_dem() / pp.run_nhd() / pp.run_nld() / pp.run_osm() individually
```

Each subpackage README documents its own classes and parameters; the branch and calibration steps are driven through `AOIProcessingConfig`, `calculate_allbranches`, and `run_calibration`.

**For more usage notes refer to the [tests](../../../tests/) or [docs](../../../docs/) for the `fimbox` python package.**
