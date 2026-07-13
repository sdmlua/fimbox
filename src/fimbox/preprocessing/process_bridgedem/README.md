### Bridge DEM Processing
<hr style="border: 1px solid blue;">

**fimbox.preprocessing.process_bridgedem** builds bridge-deck elevation data from USGS 3DEP LiDAR so bridges can be healed in the HAND raster. It runs in two stages: rasterize LiDAR points per bridge, then difference those rasters against the AOI DEM into a single `bridge_elev_diff.tif` consumed by the branch-processing step (`heal_bridges_osm`).

**Workflow**

For every bridge line, LiDAR points are streamed from 3DEP and rasterized into a small deck-elevation raster. Each deck raster is differenced against the AOI DEM, and all differences are mosaicked into one `bridge_elev_diff.tif`. Branch processing later adds this difference onto the HAND raster to heal bridge decks.

<!-- Diagram source: workflows/process_bridgedem.mmd - edit that file and regenerate with `make workflows` (see workflows/README.md) -->
<div align="center">
  <img src="../../../../workflows/svg/process_bridgedem.svg" alt="process bridgedem workflow" />
</div>

**Module contents**

| File | What it contains |
|---|---|
| `bridge_lidar_raster.py` | `generateBridgeRaster`: streams 3DEP LiDAR (EPT) around each bridge line and IDW-rasterizes it into per-bridge elevation GeoTIFFs. |
| `bridge_dem_diff.py` | `BridgeDEMDiff`: computes per-pixel (lidar elevation - DEM elevation) for every bridge raster and mosaics them into one difference raster. |

### Usage
<hr style="border: 1px solid blue;">

```python
import fimbox

# 1. Download LiDAR and build per-bridge elevation tifs
tif_dir = fimbox.generateBridgeRaster(
    bridge_gpkg="out/my_basin/watershed-data/osm_bridges_subset.gpkg",  #bridge lines (OSM or custom)
    out_dir="out/my_basin/watershed-data",  #root output directory
    # resolution: float = 10.0,             #output raster pixel size in meters
    # buffer_m: float = 10.0,               #half-width buffer around each bridge for the LiDAR query
    # n_workers: int = None,                #parallel bridge workers (default all CPUs)
    # tile_workers: int = 8,                #threads per bridge for EPT tile fetch
    # min_tile_depth: int = 6,              #skip EPT tiles shallower than this depth
    # bridge_cls_threshold: float = 0.05,   #min fraction of points classified as bridge deck
    # skip_existing: bool = True,           #skip bridges whose output tif already exists
    # id_col: Optional[str] = None,         #unique bridge ID column (auto-detects osmid)
    # skip_ids: list = None,                #bridge IDs to skip
).run()

# Check which bridges already have rasters vs still pending (safe to run anytime)
status = fimbox.generateBridgeRaster(bridge_gpkg=..., out_dir=...).status()

# 2. Difference the LiDAR rasters against the DEM and mosaic
diff_tif = fimbox.BridgeDEMDiff(
    dem_path="out/my_basin/watershed-data/dem.tif",  #base DEM
    lidar_tif_dir=tif_dir,                  #directory of per-bridge tifs from step 1
    bridge_gpkg="out/my_basin/watershed-data/osm_bridges_subset.gpkg",  #bridge lines
    out_dir="out/my_basin/watershed-data",  #output directory
    # out_name: str = "bridge_elev_diff.tif", #output filename
    # n_workers: int = None,                #parallel workers (default min(cpu_count, 8))
    # id_col: Optional[str] = "osmid",      #unique bridge ID column
    # cleanup_lidar_tifs: bool = True,      #remove per-bridge tifs after a successful mosaic
).run()
```

**Key outputs** (under `<aoi>/watershed-data/bridge_dem/`): `lidar_osm_rasters/<bridge_id>.tif` per bridge and the mosaic `bridge_elev_diff.tif`, which is passed to branch processing as `bridge_elev_diff_path`.

**For more usage notes refer to the [tests](../../../../tests/) or [docs](../../../../docs/) for the `fimbox` python package.**
