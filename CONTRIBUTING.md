# Contributing to `fimbox`

Thank you for contributing to `fimbox`. We welcome bug fixes, documentation
improvements, new preprocessing modules, calibration routines, FIM
generation features, and most importantly we welcome new FIM model integration, tests, and usability improvements.

## Before You Start

- Check existing [issues](https://github.com/sdmlua/fimbox/issues) and pull
  requests before starting work.
- For larger changes (new preprocessing step, new calibration routine,
  major refactor), please open an issue first so we can align on scope
  and approach.
- Keep changes focused. Small, well-scoped pull requests are much easier
  to review and merge.

## Development Setup

`fimbox` requires Python 3.10 or newer (the package pins `>=3.10,<3.13`).

```bash
git clone https://github.com/sdmlua/fimbox.git
cd fimbox
pip install uv
uv venv
```

Activate the virtual environment:

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

Then install the package:

```bash
uv pip install -e .
uv pip install -e ".[dev]"
```

If you prefer Conda, create and activate the environment first, then run
the same `uv pip install` commands inside it.

## Project Layout

The main locations in this repository are:

- `src/fimbox/preprocessing/download_data/` — input data downloaders
  (DEM, NHD, NWM, FEMA NFHL, NLD, OSM bridges/roads, USGS gages).
- `src/fimbox/preprocessing/calculate_branch/` — the HAND production
  pipeline (BranchDerivation, BranchZero, CreateHAND, process_branches,
  calculate_allbranches).
- `src/fimbox/preprocessing/calibrate_ratingcurve/` — synthetic rating
  curve calibration (bankfull identification, channel/overbank
  subdivision, nonmonotonic adjustment, manual calibration).
- `src/fimbox/fimgeneration/` — flood inundation map generation
  (Inundator, BranchMosaic, FimGenerator).
- `src/fimbox/_dask.py` — shared Dask LocalCluster sized to the running
  machine.
- `src/fimbox/_skip_if_valid.py` — file-integrity-aware skip helper used
  throughout the preprocessing pipeline.
- `config/` — deny lists used by the outputs-cleanup step.
- `tests/` — test coverage for each subpackage.
- `docs/` — usage notebooks, images, and sample data.

## Making Changes

- Follow the existing code style and naming patterns used in the
  relevant module.
- Add or update tests when changing behaviour. `tests/test_fimgeneration.py`
  is a good template for a real-AOI test: parameters live at the top of
  the file, the test is skipped automatically when the fixture isn't
  present.
- If your change affects output filenames, raster dtypes, CSV columns,
  or directory structure, document it clearly in the pull request.
- Class-based modules use `@dataclass` and a `run()` method (see
  `Inundator`, `BranchMosaic`, `SrcBankfull`). Match that pattern when
  adding new orchestrator classes.
- Comments are plain `#`. Avoid referencing external repositories
  inside docstrings unless the comment documents an algorithmic
  decision worth tracing.

## Tests

Tests pass parameters at the top of each file so a developer can edit
one constant to switch AOIs:

```python
# tests/test_fimgeneration.py
AOI_DIR = Path("/Users/.../out/test_smallB")
N_WORKERS = 4
```

Tests that need real AOI data are gated with
`@pytest.mark.skipif(not (AOI_DIR / "branches").is_dir(), ...)` so they
skip automatically when the fixture isn't present.

Always run pytest via `python -m pytest`, not the bare `pytest` command.
When conda is active alongside the uv venv, the shell resolves `pytest` to
conda's copy, which doesn't have fimbox installed and fails immediately.
`python -m pytest` uses whichever Python is first on `PATH` — the venv's
Python — so it always finds the installed package.

**Mac / Linux**
```bash
black .
python -m pytest tests/
```

**Windows**
```cmd
python -m pytest tests/
```

Narrower test target while developing:

**Mac / Linux**
```bash
python -m pytest tests/test_fimgeneration.py -s
python -m pytest tests/test_branchprocessing.py::test_step_C25_calculate_allbranches_live_run -s
```

**Windows**
```cmd
python -m pytest tests/test_fimgeneration.py -s
python -m pytest tests/test_branchprocessing.py::test_step_C25_calculate_allbranches_live_run -s
```

The branch-processing live-run test (`test_step_C25_*`) is opt-in via
`FIMBOX_RUN_ALLBRANCHES=1` because it can take 30+ minutes on a HUC8.

## Pull Request Guidelines

When your changes are ready:

1. Create a feature branch from the latest main branch.
2. Commit your changes with a clear commit message.
3. Open a pull request against `sdmlua/fimbox`.

Please include the following in the pull request description:

- a short summary of what changed
- why the change is needed
- any testing you performed (`pytest tests/...`)
- any limitations, assumptions, or known follow-up work

If your pull request changes raster outputs, hydroTable columns, FIM
generation behavior, or directory structure, sample output paths or
screenshots are very helpful.

## Reporting Bugs and Requesting Features

For bugs, please open an issue with:

- a short description of the problem
- steps to reproduce
- expected behavior
- relevant error messages or screenshots
- environment details such as OS, Python version, and package versions
  when relevant

For feature requests, please describe:

- the workflow or use case
- why the current behavior is limiting
- your proposed change, if you already have one in mind

## Questions

For questions about contributing or project direction, please open an
issue or contact:

- Sagy Cohen: sagy.cohen@ua.edu
- Supath Dhital: sdhital@ua.edu
