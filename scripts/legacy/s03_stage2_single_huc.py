"""
Stage 2 — HAND Processing for a SINGLE HUC8.
Use this to test one HUC before running run_stage2_hand.py on all 24.

Prerequisite: Stage 1 must have completed for this HUC.

Set HUC8 below and run:
    .venv\\Scripts\\python.exe scripts/run_single_huc_stage2.py
"""
import logging
import traceback
from pathlib import Path

# ── SET THIS ──────────────────────────────────────────────────────
HUC8 = "03020102"
# ─────────────────────────────────────────────────────────────────

OUT_DIR    = Path("D:/SI/out")
IDENTIFIER = "nwmmr"
CONFIG_DIR = Path("config")
TASK_LOG   = Path("D:/SI/out/stage2_status.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _log_result(huc8: str, status: str, note: str = "") -> None:
    TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TASK_LOG.open("a") as f:
        f.write(f"stage2 {huc8} {status}{(' ' + note) if note else ''}\n")


def main():
    from fimbox import AOIProcessingConfig, BranchDerivation, calculate_allbranches
    from fimbox._dask import _resolve_n_workers

    aoi_dir = OUT_DIR / f"HUC{HUC8}" / "watershed-data"
    if not aoi_dir.exists():
        log.error(f"watershed-data not found — run Stage 1 first: {aoi_dir}")
        return

    def _opt(p: Path):
        return p if p.exists() else None

    log.info(f"Stage 2 — HUC8: {HUC8}")
    try:
        BranchDerivation(
            out_dir=aoi_dir,
            branch_id_attribute="levpa_id",
            reach_id_attribute="ID",
            branch_buffer_distance_meters=7000.0,
        ).run()

        cfg = AOIProcessingConfig(
            aoi_dir=aoi_dir,
            branch_list_path=aoi_dir / "branch_ids.lst",
            dem_path=aoi_dir / "dem.tif",
            streams_gpkg=aoi_dir / f"{IDENTIFIER}_subset_streams.gpkg",
            boundary_gpkg=aoi_dir / "wbd_buffered.gpkg",
            bridge_elev_diff_path=_opt(aoi_dir / "bridge_elev_diff.tif"),
            levee_gpkg_path=_opt(aoi_dir / "3d_nld_subset_levees_burned.gpkg"),
            headwaters_gpkg=_opt(aoi_dir / f"{IDENTIFIER}_headwater_points_subset.gpkg"),
            levelpaths_extended_gpkg=_opt(
                aoi_dir / f"{IDENTIFIER}_subset_streams_levelPaths_extended.gpkg"
            ),
            agree_buffer_m=15.0,
            agree_smooth_drop=10.0,
            agree_sharp_drop=1000.0,
            cost_distance_tolerance=50.0,
            lateral_elevation_threshold=10,
            max_split_distance_m=1500.0,
            slope_min=0.0001,
            lakes_buffer_dist_m=100.0,
            mannings_n=0.06,
            stage_min_m=0.0,
            stage_interval_m=0.3048,
            stage_max_m=25.2984,
            min_catchment_area=0.25,
            min_stream_length=0.5,
            crosswalk_max_distance_m=100.0,
            src_slope_source="iris_sword",
            iris_slope_csv=None,
            hfab_slope_column=None,
            n_workers=_resolve_n_workers(),
            keep_failed_branches=True,
            delete_deny_list=True,
        )

        calculate_allbranches(
            cfg,
            run_branch_zero=True,
            delete_deny_list=True,
            deny_unit_list=CONFIG_DIR / "deny_unit.lst",
            branch_ids_csv=aoi_dir / "branch_ids.csv",
        )
        _log_result(HUC8, "PASS")
        log.info("PASS")
    except Exception:
        err = traceback.format_exc()
        _log_result(HUC8, "FAIL", err.splitlines()[-1])
        log.error(f"FAIL:\n{err}")


if __name__ == "__main__":
    main()
