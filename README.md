<div align="center">
  <img src="docs/images/fimbox.png" alt="fimbox" width="150" />
  <h2>FIMbox- A Testbed for Flood Inundation Mapping Experimentation</h2>
  <p>
    <a href="https://github.com/sdmlua/fimbox/releases"><img src="https://img.shields.io/github/v/release/sdmlua/fimbox?include_prereleases" alt="Release" /></a>
    <a href="https://github.com/sdmlua/fimbox/issues"><img src="https://img.shields.io/github/issues/sdmlua/fimbox" alt="Issues" /></a>
    <a href="https://github.com/sdmlua/fimbox/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="License: GPL v3" /></a><br>
    <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff" /></a>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue" alt="Python versions" /></a><br>
    <a href="https://pypi.org/project/fimbox/"><img src="https://badge.fury.io/py/fimbox.svg?icon=si%3Apython" alt="PyPI version" /></a>
    <a href="https://pepy.tech/projects/fimbox"><img src="https://static.pepy.tech/badge/fimbox" alt="PyPI Downloads" /></a>
  </p>
</div>

| | |
| --- | --- |
| <a href="https://sdml.ua.edu"><img src="docs/images/SDML_logo.png" alt="SDML Logo" width="400"></a> | A modular open source testbed framework to standardize Flood Inundation Mapping (FIM) simulations and evaluation with custom datasets and hydrologic parameters in reproducible workflows. `fimbox` provides a streamlined, user friendly and cloud enabled pipeline to generate operational flood inundation maps using the NOAA Office of Water Prediction (OWP) Height Above Nearest Drainage (HAND) FIM framework, driven by National Water Model retrospective and forecasted streamflow. It is developed under the [Surface Dynamics Modeling Lab (SDML)](https://sdml.ua.edu) at The University of Alabama. |

## High-level workflow
---
`fimbox` implements an extensive Height Above Nearest Drainage (HAND) based flood inundation mapping workflow. It offers great flexibility to change datasets (e.g. resolution and source of river networks, catchments, DEMs) and to investigate different research questions (e.g. changing Manning's n, better representation of the synthetic rating curve, stream network segmentation, slope improvement and many more terrain conditioning options) to improve FIM extents and depths. The HAND preprocessing logic follows the NOAA OWP HAND FIM framework, whose reference implementation lives at https://github.com/NOAA-OWP/inundation-mapping. Ongoing work expands the modeling capability beyond a single model, integrating different models to enable multimodel FIM extents, and more.

<div align="center">
  <img src="docs/images/fimbox-flowchart.png" alt="fimbox-workflow" width="700" />
</div>

Every module shown above ships its own README with a detailed workflow diagram and step by step usage: what the module does, every class and parameter it exposes, and runnable examples (see the [Module documentation](#module-documentation) table below). Worked end to end examples for each stage live in [`tests/`](tests/) and the package documentation in [`docs/`](docs/).

## Install
---

`fimbox` targets Python 3.10–3.12.

```bash
git clone https://github.com/sdmlua/fimbox.git
cd fimbox

# uv-based environment (recommended)
pip install uv
uv venv
uv pip install -e .
```

Activate the virtual environment before running any commands:

**Mac / Linux**
```bash
source .venv/bin/activate
```

**Windows (Command Prompt)**
```cmd
.venv\Scripts\activate.bat
```

**Windows (PowerShell)**
```powershell
.venv\Scripts\Activate.ps1
```

If you prefer Conda, create and activate the environment first, then run
`uv pip install -e .` inside it.


## Quick start: from boundary polygon to flood map
---
### 1. Stage AOI inputs

Download the DEM, NHD/NWM hydrography, FEMA NFHL, NLD levees, OSM
bridges/roads, and USGS gages into an AOI working directory.

```python
from fimbox import getAllInputData

getAllInputData(
    boundary="path/to/aoi_boundary.gpkg",
    aoi_id="my_basin",
    out_dir="out/my_basin",
)
```
See the [`tests/`](tests/) folder for further detailed steps including HAND processing, SRC generation, calibration, and FIM generation. Users can change different parameters based on requirements.

## Module documentation
---
Each module has its own README documenting what it contains, the full parameter surface (including optional parameters), and usage examples:

| Module | Documentation | Purpose |
|---|---|---|
| `fimbox.preprocessing` | [README](src/fimbox/preprocessing/README.md) | Overview of the preprocessing stages and the combined `getAllInputData` pipeline. |
| `fimbox.preprocessing.download_data` | [README](src/fimbox/preprocessing/download_data/README.md) | Download and standardize AOI inputs (DEM, hydrography, NFHL, levees, OSM, gages). |
| `fimbox.preprocessing.huc_test` | [README](src/fimbox/preprocessing/huc_test/README.md) | Validate HUC8 codes against the packaged acceptable lists. |
| `fimbox.preprocessing.process_bridgedem` | [README](src/fimbox/preprocessing/process_bridgedem/README.md) | Per-bridge LiDAR rasters and the bridge/DEM difference raster. |
| `fimbox.preprocessing.calculate_branch` | [README](src/fimbox/preprocessing/calculate_branch/README.md) | Branch derivation, HAND generation, crosswalk, and SRC/hydroTable build. |
| `fimbox.preprocessing.calibrate_ratingcurve` | [README](src/fimbox/preprocessing/calibrate_ratingcurve/README.md) | SRC calibration (bathymetry, bankfull, subdivision, USGS/spatial/manual). |
| `fimbox.streamflow` | [README](src/fimbox/streamflow/README.md) | NWM retrospective/forecast, GEOGLOWS, and USGS retrieval, plots, statistics. |
| `fimbox.fimgeneration` | [README](src/fimbox/fimgeneration/README.md) | Per-branch inundation and AOI mosaicking from discharge CSVs. |
| Tests | [README](tests/README.md) | What each test file demonstrates and how to run the suite. |
| Workflow diagrams | [README](workflows/README.md) | Editable Mermaid sources + generator script for every module workflow SVG (`make workflows`). |

**For more usage notes refer to the [tests](tests/) or [docs](docs/) for the `fimbox` python package.**

## Contribution
---
For contribution guidelines see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Acknowledgements
---
| | |
| --- | --- |
| <a href="https://ciroh.ua.edu"><img src="docs/images/CIROH-logo.jpg" alt="CIROH Logo" width="300"></a> | Funding for this project was provided by the National Oceanic & Atmospheric Administration (NOAA), awarded to the Cooperative Institute for Research to Operations in Hydrology (CIROH) through the NOAA Cooperative Agreement with The University of Alabama (NA22NWS4320003). |


## Contact
---
`fimbox` is developed at the
[Surface Dynamics Modeling Lab (SDML)](https://sdml.ua.edu/) at The
University of Alabama.

Sagy Cohen (sagy.cohen@ua.edu), Supath Dhital (sdhital@ua.edu)

NOTE- This repository is still in active development and might contain bugs. Please let us know or create a pull request if you have better ideas. THANK YOU.