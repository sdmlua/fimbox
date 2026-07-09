# Legacy scripts

These files are retired — kept for history only, not maintained, and not
part of the active pipeline. Each is superseded by a file in `scripts/`
(see `../README.md` for the active set):

| Legacy file | Superseded by |
|---|---|
| `s01_download_dem.py` | `s01_download_dem_fast.py` (reads AOI window from cloud-optimized GeoTIFFs directly — ~8x faster, no national VRT parse) |
| `s02_stage1_single_huc.py` | `s02_stage1_all_hucs.py` |
| `s03_stage2_single_huc.py` | `s03_stage2_all_hucs.py` |
| `s04_usgs_gage_crosswalk.py` | `s04_usgs_gage_crosswalk_all_hucs.py` |
| `s05_calibrate_n_recurrence.py` | `s05_ncalib_core.py` + `s05a`–`s05d` |
| `s05_revert_uncalibrated_srcs.py` | Not needed — fixed a failure mode (trunk-branch averaging propagating onto ungauged reaches) that cannot occur under the new pipeline, which only ever touches gauged HydroIDs. Was hardcoded to one HUC (`03020102`) and the pre-correction `D:/SI/out` drive path. |
| `s06_stage3_single_huc.py` | `s06_stage3_all_hucs.py` |
| `s07_stage4_single_huc.py` | `s07_stage4_all_hucs.py` |
| `s08_stage5_single_huc.py` | `s08_stage5_all_hucs.py` |
| `plot_calibration_slides.py` | The method-decision deck (`docs/method_decision_deck.pptx`) and `plot_v2_diagnostics.py` |
| `plot_src_calibration.py`, `plot_src_calibration_inset.py`, `plot_srcs.py` | `plot_v2_diagnostics.py`, `export_rc_panels_and_videos.py` |
| `plot_workflow_chart.py` | — (superseded by the pipeline order documented in `../README.md`) |
| `open_parquet.py` | Scratch one-liner; use `pandas.read_parquet` directly |

`scripts/s05_calibrate_n_recurrence_all_hucs.py` (the old monolithic v2
calibration script) is not moved here — it has local uncommitted edits that
are intentionally being kept out of git history. It is also fully superseded
by `s05_ncalib_core.py` + `s05a`–`s05d`.
