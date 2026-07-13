### FIM Evaluation
<hr style="border: 1px solid blue;">

**fimbox.fimevaluation** closes the fimbox loop: after `fimbox.fimgeneration` produces a flood map, this module finds the matching ground truth in the [**FIMbench**](https://github.com/sdmlua/fimbench) benchmark database and evaluates the map against it with the [**FIMeval**](https://github.com/sdmlua/fimeval) framework — contingency maps, agreement metrics (CSI, POD, FAR, F1, accuracy, ...), and optional building-level analysis. Benchmarks download into `<aoi>/benchmark-data/`, evaluation cases are staged under `<aoi>/fim-evaluation/<case>/`, and results land in `<aoi>/fim-evaluation/outputs/`.

**Workflow**

The module first queries the FIMbench catalog (public, anonymous S3 — no credentials) using the AOI's newest flood extent as the spatial footprint, optionally narrowed by HUC8, tier, or event date, and downloads the matched benchmark GeoTIFF + AOI GeoPackage. Candidates and benchmark are then staged into a FIMeval-shaped case directory (FIMeval discovers the benchmark by the word `benchmark` in its filename — staging enforces that) and one `EvaluateFIM` run produces the evaluation boundary, masked rasters, contingency maps, and metrics CSVs, which are collected into a single tidy DataFrame.

<!-- Diagram source: workflows/fimevaluation.mmd - edit that file and regenerate with `make workflows` (see workflows/README.md) -->
<div align="center">
  <img src="../../../workflows/svg/fimevaluation.svg" alt="fimevaluation workflow" />
</div>

**Module contents**

| File | What it contains |
|---|---|
| `benchmark_query.py` | `BenchmarkQuery` class and `queryBenchmarkFIM` wrapper: FIMbench catalog search + asset download into `<aoi>/benchmark-data/`; `latest_fim_extent` helper. |
| `evaluate.py` | `FIMEvaluator` class and `evaluateFIM` wrapper: case staging, `EvaluateFIM` / `PrintContingencyMap` / `PlotEvaluationMetrics` / building-footprint runs, metrics collection into `EvaluationResult`. |

Both `fimbench` and `fimeval` **install together with fimbox** — no extra install step. They are imported lazily inside the functions, so `import fimbox` stays light.

### Usage
<hr style="border: 1px solid blue;">

```python
import fimbox

# 1. Query the FIMbench database — which benchmarks cover my flood map?
result = fimbox.queryBenchmarkFIM(
    "out/my_basin",                      #AOI root; footprint = newest extent in <aoi>/fim-outputs/
    # raster_path="my_fim.tif",          #explicit candidate raster instead
    # boundary_path="my_aoi.gpkg",       #or an AOI boundary vector
    # huc8="03020201",                   #narrow by basin
    # tier="tier1",                      #narrow by benchmark tier ("HWM", "tier_1", ...)
    # event_date="2017-08-30",           #match an exact event date
    # start_date="2016-04-01",           #or a date range
    # end_date="2026-01-01",
    # file_name="HWM_..._BM.tif",        #direct lookup by catalog filename
    area=True,                           #add overlap % and km² per match
    download=True,                       #fetch GeoTIFF + GeoPackage locally
    # out_dir="downloads/",              #default <aoi>/benchmark-data/
)
print(result)                            #pretty match summary
print(result.records)                    #catalog record per match (source, tier, dates)
print(result.benchmark_rasters)          #downloaded .tif paths

# 2. Evaluate the candidate FIM(s) against the benchmark.
evaluation = fimbox.evaluateFIM(
    "out/my_basin",                      #AOI root
    # candidate="my_fim.tif",            #default: every extent .tif in <aoi>/fim-outputs/
    # benchmark="benchmark.tif",         #default: newest .tif in <aoi>/benchmark-data/
    # case_name="hurricane_matthew",     #default: first candidate's stem
    method_name="smallest_extent",       #"smallest_extent" | "convex_hull" | "AOI"
    # aoi_boundary="my_aoi.gpkg",        #required when method_name="AOI"
    # pwb_dir="my_pwb.gpkg",             #own permanent-water-bodies vector (default: CONUS)
    # target_crs="EPSG:32633",           #outside CONUS (default EPSG:5070)
    # target_resolution=10,              #m, when candidate/benchmark resolutions differ
    contingency_map=True,                #save contingency map figures
    plot_metrics=True,                   #save metric bar charts
    # building_footprint=True,           #building-level agreement (Microsoft footprints/GEE)
    # building_footprint_file="bf.gpkg", #or your own footprint vector
)
print(evaluation.metrics)                #CSI / POD / FAR / F1 / accuracy ... as one DataFrame
print(evaluation.output_dir)             #contingency maps, plots, CSVs

# Class-based, when you want to hold on to the configuration:
query = fimbox.BenchmarkQuery(aoi_dir="out/my_basin", tier="HWM", download=True)
evaluator = fimbox.FIMEvaluator(aoi_dir="out/my_basin", method_name="convex_hull")
result = query.run(); evaluation = evaluator.run()
```

**Key outputs**: benchmark assets under `<aoi>/benchmark-data/`, staged cases under `<aoi>/fim-evaluation/<case>/`, and FIMeval results (contingency maps, metric plots, `EvaluationMetrics/*.csv`) under `<aoi>/fim-evaluation/outputs/<case>/`.

**For more usage notes refer to the [tests](../../../tests/) or [docs](../../../docs/) for the `fimbox` python package.**
