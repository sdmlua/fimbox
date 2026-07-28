### Rating Curve Calibration
<hr style="border: 1px solid blue;">

**fimbox.preprocessing.calibrate_ratingcurve** adjusts and calibrates the synthetic rating curves (SRCs) and hydroTables produced by branch processing. The pipeline order is: reset (rerun only), pre-aggregation, thalweg notches, longitudinal filter, bathymetry, bankfull, channel/overbank subdivision, nonmonotonic fix, USGS rating-curve calibration, spatial-observation calibration, manual calibration, post-aggregation, and log scan. Calibrated discharge is written back per branch as `discharge_cms = precalb_discharge_cms / calb_coef_final`.

**Workflow**

Calibration refines the raw synthetic rating curves in three passes: fix the channel geometry (thalweg notches, longitudinal smoothing, bathymetry), refine the hydraulics (bankfull identification and channel/overbank subdivision), and correct discharge against observations (USGS, spatial, manual). The resulting coefficients are applied per feature ID, and the branch tables are rolled up into AOI-level outputs.

**When subdivision applies.** Subdivision needs both `src_subdiv_toggle` and `src_bankfull_toggle`, since it splits geometry at the `Stage_bankfull` column bankfull identification adds. It is then decided per reach: a reach is subdivided only where a usable bankfull flow exists, meaning its `feature_id` is present in `bankfull_flows_file` with a flow above zero (NWM lake and coastal reaches carry zero). Reaches without one keep `subdiv_applied=False` and their original single-`ManningN` discharge — the roughness table is irrelevant to them. Where subdivision does apply, per-`feature_id` values from `vmann_input_file` are used, falling back to `default_channel_n` (0.06) and `default_overbank_n` (0.12) for feature IDs the table does not cover.

The three observation-driven calibrators (`src_adjust_usgs`, `src_adjust_ras2fim`, `src_adjust_spatial`) read the per-stage `channel_n` / `overbank_n` that only subdivision writes, so they run only when subdivision actually ran — toggling `src_subdiv_toggle` without `src_bankfull_toggle` skips them and logs a warning. Note that `channel_n` / `overbank_n` are recorded for every reach, including non-subdivided ones, so a non-null value in those columns does not by itself mean subdivision was applied to that reach; `subdiv_applied` is the flag to read.

**Rerunning on an already-calibrated AOI is safe**: set `calibration_rerun=True` and `HydroTableReset` restores the uncalibrated baseline before anything else runs, so adjustments never stack on top of a previous calibration. The reset does not need backup files: it recomputes `Discharge (m3s-1)` from the raw per-branch geometry in `src_base_<id>.csv` using Manning's equation with the original `default_SLOPE` and `default_ManningN` stamped during branch processing, pushes that discharge back into both `src_full_crosswalked_<id>.csv` and `hydroTable_<id>.csv`, drops every calibration artefact column (bankfull, subdivision, coefficients), and clears `Bathymetry_source`, leaving the tables exactly as branch processing produced them.

<!-- Diagram source: workflows/calibrate_ratingcurve.mmd - edit that file and regenerate with `make workflows` (see workflows/README.md) -->
<div align="center">
  <img src="../../../../workflows/svg/calibrate_ratingcurve.svg" alt="calibrate ratingcurve workflow" />
</div>

**Module contents**

| File | What it contains |
|---|---|
| `pipeline.py` | `CalibrationConfig` (every knob in one dataclass), `Calibrator`, and the `run_calibration()` entry point. |
| `dem_adjust.py` | `ThalwegNotchesAdjustment`, `LongitudinalFlowFilter`, `BathymetricAdjustment` (eHydro surveys or AI-based bathymetry). |
| `src_adjust.py` | `SrcBankfull` (bankfull stage from NWM flows), `SrcSubdiv` (channel/overbank Manning subdivision), `SrcNonmonotonic`. |
| `src_calibrate.py` | `UsgsRatingCalibrator`, `SpatialObsCalibrator`, `ManualCalibrator`, `Ras2fimCalibrator` (not yet ported). |
| `src_optimization.py` | Shared engine: `update_rating_curve()`, network tracing, and downstream coefficient propagation (8 km, roughness window 0.001 to 0.8). |
| `aggregate.py` | `BranchAggregator`: branch to AOI rollup of elev tables, hydroTables, and SRC files. |
| `reset.py` | `HydroTableReset`: rebuild the uncalibrated baseline for reruns: Manning's equation over `src_base` geometry with the default SLOPE/ManningN, calibration columns dropped. |
| `logscan.py` | `LogScanner`: collect error/warning lines from logs into per-AOI summaries. |
| `_common.py` | Shared helpers, dtype maps, and constants. |

**Input datasets** (bundled in the repo `data/` folder): `nwm3_high_water_threshold_cms.parquet` (bankfull flows), `mannings_global_optz.parquet` (variable channel/overbank n), `usgs_rating_curves.parquet` + `acceptable_sites_for_rating_curves.parquet` + `nwm3_17C_recurrence_flows_cfs.parquet` (USGS calibration), `final_bathymetry_ehydro_ohrfc.gpkg` (bathymetry).

### Usage
<hr style="border: 1px solid blue;">

```python
from pathlib import Path
from fimbox import CalibrationConfig, run_calibration

DATA = Path("data")

cfg = CalibrationConfig(
    # - mode -
    calibration_rerun=True,                     #reset hydroTables to baseline before re-applying
    # - step toggles (all default False; each needs its input file) -
    thalweg_notches_adjustment=True,            #remove thalweg-notch artifact rows, refill stage ladder
    longitudinal_filter=True,                   #smooth hydraulic geometry along reach chains
    bathymetry_adjust=True,                     #add missing in-channel area below the DEM
    src_bankfull_toggle=True,                   #identify bankfull stage in every branch SRC
    src_subdiv_toggle=True,                     #channel/overbank subdivision (requires bankfull on)
    nonmonotonic_src_adjustment=True,           #force in-channel discharge monotonic
    src_adjust_usgs=True,                       #calibrate against USGS rating curves (requires subdiv to have run)
    # src_adjust_ras2fim: bool = False,         #ras2fim rating calibration (not yet ported)
    # src_adjust_spatial: bool = False,         #calibrate against benchmark FIM points (requires subdiv to have run)
    # manual_calb_toggle: bool = False,         #apply per-feature_id manual coefficients
    # - input files for the toggled-on routines -
    bathy_file_ehydro=DATA / "final_bathymetry_ehydro_ohrfc.gpkg",  #eHydro surveyed channels
    # bathy_file_aibased: Optional[Path] = None, #AI-based bathymetry table
    # ai_toggle: int = 0,                       #1 = also use AI-based bathymetry
    # ai_strm_order: int = 4,                   #min stream order for AI-based bathymetry
    bankfull_flows_file=DATA / "nwm3_high_water_threshold_cms.parquet",  #NWM bankfull flows per feature_id
    vmann_input_file=DATA / "mannings_global_optz.parquet",  #variable channel_n/overbank_n per feature_id
    usgs_rating_curve_csv=DATA / "usgs_rating_curves.parquet",  #USGS rating curves (location_id, flow, stage, elevation)
    usgs_acceptable_gages=DATA / "acceptable_sites_for_rating_curves.parquet",  #gage quality filter
    nwm_recur_file=DATA / "nwm3_17C_recurrence_flows_cfs.parquet",  #NWM 2/5/10/25/50-yr recurrence flows
    # ras_rating_curve_csv: Optional[Path] = None, #ras2fim rating curves (when ported)
    # calib_points_file: Optional[Path] = None, #benchmark obs points (.parquet) for spatial calibration
    # man_calb_file: Optional[Path] = None,     #manual coefficients (aoi_id, feature_id, calb_coef_manual)
    # - tunables -
    # default_channel_n: float = 0.06,          #channel n when feature_id missing from vmann table
    # default_overbank_n: float = 0.12,         #overbank n when feature_id missing from vmann table
    # nonmonotonic_stream_order_min: int = 4,   #min stream order for the nonmonotonic fix
    # include_branch_zero: bool = True,         #also process branch zero
    # - aggregation / logging / execution -
    # aggregate_pre: bool = True,               #assemble usgs/ras elev tables before adjustments
    # aggregate_post: bool = True,              #publish final AOI hydroTable + layers after calibration
    # scan_logs: bool = False,                  #collect error/warning lines into per-AOI summaries
    job_branch_limit=4,                         #worker count for branch-parallel routines
    # skip_unimplemented: bool = False,         #warn-and-skip unported routines instead of raising
)

run_calibration("out/my_basin", cfg)            #AOI directory + config; cfg omitted = default pipeline
```

**Key outputs**: per-branch `hydroTable_<id>.csv` and `src_full_crosswalked_<id>.csv` updated in place with `subdiv_applied`, `channel_n`, `overbank_n`, `precalb_discharge_cms`, `calb_coef_usgs`, `calb_coef_ras2fim`, `calb_coef_spatial`, `calb_coef_final`, `calb_applied`, and recalibrated `discharge_cms`; AOI-level `hydroTable.csv` and `src_full_crosswalked.csv` after post-aggregation.

The hydroTable carries all of these columns from the moment branch processing creates it, seeded to a "not populated yet" value (`False` for the flags, null elsewhere). Calibration fills them in, so an all-null column means the step that owns it never ran — which is how the calibrators distinguish "subdivision did not run" from "column absent".

**For more usage notes refer to the [tests](../../../../tests/) or [docs](../../../../docs/) for the `fimbox` python package.**
