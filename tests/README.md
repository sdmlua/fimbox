### Tests
<hr style="border: 1px solid blue;">

The test suite doubles as the usage reference for `fimbox`: every stage of the workflow has a test file whose top-level constants (AOI paths, input files, worker counts) are meant to be edited to point at your own data. Tests skip cleanly when the referenced AOI or input file is absent. Most files also keep commented-out variants showing alternative call patterns and optional parameters.

**Folder contents**

| File | What it covers |
|---|---|
| `conftest.py` | Suite-wide logging setup (`configure_cli_logging`). |
| `test_preprocessing_hucs.py` | HUC validation with `HUCChecker` (single HUC, lists, .lst/.csv files, strict mode). |
| `test_downloaddata.py` | Individual dataset downloaders: DEM, NHDPlus/NWM hydrography, FEMA NFHL, NLD levees, OSM roads/bridges, USGS gages. |
| `test_getallinputdata.py` | The combined `getAllInputData` pipeline from a boundary shapefile, including bring-your-own flowlines/catchments/DEM. |
| `test_preprocessDEM.py` | `DEMProcessor` fetching and conditioning (resolutions, local DEM, CRS handling). |
| `test_generate_dem_diff.py` | Bridge LiDAR rasters (`generateBridgeRaster`, with `status()` check) and `BridgeDEMDiff` mosaicking. |
| `test_branchprocessing.py` | `BranchDerivation`, `AOIProcessingConfig`, `calculate_allbranches`, plus step-level tests for the BranchZero and CreateHAND substeps. |
| `test_calibrate_pipeline.py` | The full SRC calibration pipeline via one `run_calibration()` call with every `CalibrationConfig` parameter spelled out, plus one test per calibration stage. |
| `test_nwmstreamflow.py` | Streamflow retrieval (`getNWMretrospective`, `getNWMforecast`, `USGSData`), plotting, and KGE/NSE/PBias statistics. |
| `test_fimgeneration.py` | FIM generation from `discharge-inputs/` CSVs with date/range selection and depth output options. |

### Running
<hr style="border: 1px solid blue;">

```bash
# from the repo root, with the environment activated
pytest tests/ -v                          # whole suite
pytest tests/test_calibrate_pipeline.py -v          # one stage
pytest tests/test_branchprocessing.py -v -k hand    # one test by keyword
```

Before running, edit the constants at the top of each test file (for example `AOI_DIR`, `BANKFULL_FLOWS_FILE`, `N_WORKERS`) to match your machine. The calibration lookup tables referenced by the tests ship in the repo [`data/`](../data/) folder.

The expected order when building an AOI from scratch mirrors the workflow: `test_getallinputdata` (stage inputs), `test_generate_dem_diff` (optional bridge healing), `test_branchprocessing` (HAND + SRC), `test_calibrate_pipeline` (calibration), `test_nwmstreamflow` (discharge), `test_fimgeneration` (flood maps).

**For more usage notes refer to the [docs](../docs/) for the `fimbox` python package.**
