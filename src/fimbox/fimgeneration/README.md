### FIM Generation
<hr style="border: 1px solid blue;">

**fimbox.fimgeneration** produces flood inundation maps from the preprocessed AOI: for each branch it interpolates a stage from the hydroTable at the forecast discharge, thresholds the HAND raster into extent/depth, and mosaics all branches into AOI-level rasters. Discharge inputs are FIM-ready CSVs (`feature_id, discharge_cms`) written by the streamflow module or supplied by the user.

**Workflow**

FIM generation is a stage-lookup exercise: each branch converts the forecast discharge into a water stage through its hydroTable, then floods every HAND cell that sits below that stage. Per-branch results are mosaicked over the branch-zero base into single AOI-level rasters. One forecast CSV in, one depth and one extent GeoTIFF out.

<!-- Diagram source: workflows/fimgeneration.mmd - edit that file and regenerate with `make workflows` (see workflows/README.md) -->
<div align="center">
  <img src="../../../workflows/svg/fimgeneration.svg" alt="fimgeneration workflow" />
</div>

**Module contents**

| File | What it contains |
|---|---|
| `inundator.py` | `Inundator` / `InundationResult`: per-branch extent and depth raster generation from HAND + hydroTable + forecast. |
| `mosaic.py` | `BranchMosaic` / `MosaicResult`: combine per-branch rasters into AOI depth/extent outputs over the branch-zero base. |
| `pipeline.py` | `FimGenerator` / `FimGenerationResult` (per-branch + mosaic orchestrator), `generateFIM` (streamflow-to-FIM convenience pipeline), and `extract_feature_ids`. |

### Usage
<hr style="border: 1px solid blue;">

```python
import fimbox

# Collect the AOI's NWM feature_ids once (input to the streamflow module)
feature_csv = fimbox.extract_feature_ids(
    "out/my_basin",                        #AOI root directory
    # out_csv: Optional[Path] = None,      #output CSV (default <aoi>/feature_id.csv)
)

# One forecast CSV -> AOI depth/extent rasters
result = fimbox.FimGenerator(
    aoi_dir="out/my_basin",                #AOI root or watershed-data folder
    forecast="discharge.csv",              #forecast CSV path or DataFrame (feature_id, discharge_cms)
    # branch_ids: Optional[Sequence[str]] = None, #limit to these branches; None = all
    # mosaic: bool = True,                 #mosaic per-branch rasters after generation
    # n_workers: int = 1,                  #parallel branch workers (ignored when use_dask)
    # use_dask: Optional[bool] = None,     #use the shared Dask cluster; None = auto
    # min_depth_m: float = 0.03,           #minimum depth threshold (m)
    # drop_lakes: bool = True,             #skip lake reaches (LakeID != -999)
    # int16_mode: bool = True,             #write depth as Int16 millimeters
    # depth_out / extent_out: Optional[Path] = None, #explicit output paths
    # intermediate_dir: Optional[Path] = None, #per-branch staging directory
    # cleanup_intermediates: bool = True,  #delete staging directory after mosaic
).run()
print(result.depth_path, result.extent_path)

# End-to-end: streamflow -> discharge CSVs -> FIM rasters
fim = fimbox.generateFIM(
    aoi_dir="out/my_basin",                #AOI root directory
    # feature_id_csv: Optional[Path] = None, #feature_id CSV; auto-resolved when None
    # n_workers: int = 4,                  #per-branch workers
    # int16_mode: bool = True,             #depth as Int16 millimeters
    # depth: bool = False,                 #also write depth rasters
)
results = fim.from_retrospective(date="2020-05-20 12:00:00")   #fetch NWM retrospective, then FIM
results = fim.from_archive(start="2020-05-19", end="2020-05-22", sortby="maximum")  #reuse downloaded archive
results = fim.from_csv("my_discharge.csv")                     #single user-supplied CSV
results = fim.from_discharge_inputs(date="2020-05-20")         #filter <aoi>/discharge-inputs/ CSVs
```

**Key outputs** (under `<aoi>/fim-outputs/`): AOI extent and depth GeoTIFFs per discharge input, named after the forecast timestamp.

**For more usage notes refer to the [tests](../../../tests/) or [docs](../../../docs/) for the `fimbox` python package.**
