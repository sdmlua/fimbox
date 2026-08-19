### Streamflow Retrieval and Analysis
<hr style="border: 1px solid blue;">

**fimbox.streamflow** retrieves the discharge data that drives FIM generation and supports its evaluation: NWM v3.0 retrospective, NWM Analysis and Assimilation (AnA), NWM operational forecasts (short/medium/long range), GEOGLOWS v2 retrospective, and USGS gage observations. Retrieved series are archived under `<aoi>/streamflow/` and exported as FIM-ready CSVs (`feature_id, discharge_cms`) into `<aoi>/discharge-inputs/`. Plotting and statistics helpers compare NWM against USGS observations.

**Workflow**

The module first pulls discharge for the AOI's feature IDs from the chosen source and stores it as a reusable parquet archive. The archive is then sliced into FIM-ready CSVs that drive FIM generation, so repeated map runs never re-download data. USGS observations feed a separate comparison track for plots and skill metrics.

<!-- Diagram source: workflows/streamflow.mmd - edit that file and regenerate with `make workflows` (see workflows/README.md) -->
<div align="center">
  <img src="../../../workflows/svg/streamflow.svg" alt="streamflow workflow" />
</div>

**Module contents**

| File | What it contains |
|---|---|
| `nwm_retrospective.py` | `NWMRetrospective` class and `getNWMretrospective` wrapper: NWM v3.0 hourly retrospective to parquet archive + FIM CSVs. |
| `nwm_analysisassim.py` | `NWMAnalysisAssim` class and `getNWManalysisassim` wrapper: NWM Analysis and Assimilation (AnA) hourly streamflow to parquet archive + FIM CSVs. |
| `nwm_forecast.py` | `NWMForecast` class and `getNWMforecast` wrapper: operational forecast cycles to per-day FIM CSVs. |
| `geoglows.py` | `GeoglowsData`: GEOGLOWS v2 retrospective from S3 zarr, mapped to feature_ids via a hydrotable. |
| `usgs.py` | `USGSData` (fetch/series) and `get_usgs_fid_pairs`: USGS gage observations for comparison. |
| `pipeline.py` | `StreamflowPipeline`: retrospective/forecast/select in one class. |
| `plotting.py` | `plot_nwm`, `plot_usgs`, `plot_comparison`: 500-DPI time series figures saved to `<aoi>/plots/`. |
| `statistics.py` | `calculate_statistics`, `compute_metrics`, `StreamflowMetrics`: KGE/NSE/PBias between NWM and USGS. |
| `_common.py` | AOI layout resolution, feature_id loading, FIM-ready CSV writing. |

### Usage
<hr style="border: 1px solid blue;">

```python
import fimbox

# NWM retrospective (archive + FIM-ready CSVs in <aoi>/discharge-inputs/)
csvs = fimbox.getNWMretrospective(
    "out/my_basin",                        #AOI root directory
    date="2020-05-20 12:00:00",            #single instant (YYYY-MM-DD HH:MM:SS) or day (YYYY-MM-DD)
    # feature_ids: Optional[list] = None,  #explicit feature ID list
    # feature_id_csv: Optional[Path] = None, #feature_id CSV (default <aoi>/feature_id.csv)
    # start: Optional[str] = None,         #range start (YYYY-MM-DD), used with end instead of date
    # end: Optional[str] = None,           #range end (YYYY-MM-DD)
    # sortby: Optional[str] = None,        #"maximum"|"minimum"|"mean" -> one aggregated CSV; None -> per hour
)

# NWM Analysis and Assimilation (AnA) — gauge-assimilated best estimate of past conditions
csvs = fimbox.getNWManalysisassim(
    "out/my_basin",                        #AOI root directory
    date="2020-05-20 12:00:00",            #single instant (YYYY-MM-DD HH:MM:SS) or day (YYYY-MM-DD)
    # feature_ids / feature_id_csv,        #as above
    # start: Optional[str] = None,         #range start (YYYY-MM-DD), used with end instead of date
    # end: Optional[str] = None,           #range end (YYYY-MM-DD)
    # sortby: Optional[str] = None,        #"maximum"|"minimum"|"median"|"mean" -> one aggregated CSV; None -> per hour
)

# NWM operational forecast
csvs = fimbox.getNWMforecast(
    "out/my_basin",                        #AOI root directory
    forecast_range="shortrange",           #"shortrange" | "mediumrange" | "longrange"
    # feature_ids / feature_id_csv,        #as above
    # forecast_date: Optional[str] = None, #cycle date (YYYY-MM-DD); default today
    # hour: Optional[int] = None,          #cycle hour (0-23), snapped to a valid cycle
    # sort_by: str = "maximum",            #daily aggregation "maximum"|"minimum"|"median"
)

# Class-based pipeline (retrieve once, slice the archive many times)
pipe = fimbox.StreamflowPipeline("out/my_basin")
csvs = pipe.retrospective(start="2020-05-19", end="2020-05-22", sortby="maximum")
csvs = pipe.select(date="2020-05-20")      #from existing archive, no re-download
csvs = pipe.analysis_assim(date="2020-05-20 12:00:00")
csvs = pipe.forecast("mediumrange", forecast_date="2024-06-01", hour=12)

# USGS observations
usgs = fimbox.USGSData("out/my_basin")
usgs.fetch(["02465000"], "2020-05-19", "2020-05-22")     #sites, start, end
series = usgs.series("02465000", "2020-05-19", "2020-05-22")
pairs = fimbox.get_usgs_fid_pairs("out/my_basin")        #(location_id, feature_id) pairs

# Plots and statistics (saved to <aoi>/plots/)
fimbox.plot_nwm("out/my_basin", [6729039, 6729043], "2016-10-05", "2016-10-20")
fimbox.plot_usgs("out/my_basin", ["02465000"], "2016-10-05", "2016-10-20")
fimbox.plot_comparison("out/my_basin", 6729039, "02465000", "2016-10-05", "2016-10-20")

metrics = fimbox.calculate_statistics(
    "out/my_basin",                        #AOI root directory
    6729039,                               #NWM feature ID
    "02465000",                            #USGS site ID
    "2016-10-05", "2016-10-20",            #start, end (YYYY-MM-DD)
    # plot: bool = True,                   #also save a metrics bar chart
)
print(metrics.kge, metrics.nse, metrics.pbias_pct)
```

**Key outputs**: parquet archives under `<aoi>/streamflow/`, FIM-ready CSVs under `<aoi>/discharge-inputs/`, figures under `<aoi>/plots/`.

**For more usage notes refer to the [tests](../../../tests/) or [docs](../../../docs/) for the `fimbox` python package.**
