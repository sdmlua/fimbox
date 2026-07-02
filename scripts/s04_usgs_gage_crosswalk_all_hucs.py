"""
USGS Gage Crosswalk for all HUC8s in the study area.

Steps per HUC:
  C20  Download USGS gauge points within the HUC8 boundary from ArcGIS Online
  C21  Assign each gauge to a NWM level path (branch ID)
  C22  Per-branch: snap gauges to thalweg, sample DEM to get dem_adj_elevation
  CON  Consolidate all per-branch usgs_elev_table.csv into one at AOI root
       (required by s05 n-calibration and Stage 4 UsgsRatingCalibrator)

Prerequisite: s03_stage2_all_hucs.py must have completed for each HUC.

Progress is written to TASK_LOG after each HUC so a re-run skips completed ones.

Run:
    .venv\\Scripts\\python.exe scripts/s04_usgs_gage_crosswalk_all_hucs.py
"""
import logging
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
EXCEL_PATH   = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")
HUC_CODE_COL = "HUC_CODE"
OUT_DIR      = Path("E:/SI/out")
IDENTIFIER   = "nwmmr"
TASK_LOG     = Path("E:/SI/out/crosswalk_status.txt")
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _load_done() -> set[str]:
    if not TASK_LOG.exists():
        return set()
    done = set()
    for line in TASK_LOG.read_text().splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[2] == "PASS":
            done.add(parts[1])
    return done


def _log_result(huc8: str, status: str, note: str = "") -> None:
    TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TASK_LOG.open("a") as f:
        f.write(f"crosswalk {huc8} {status}{(' ' + note) if note else ''}\n")


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def run_huc(huc8: str) -> None:
    from fimbox import DownloadUSGSGages, assign_gages_to_branches, run_branch_crosswalk
    import geopandas as gpd

    aoi_root  = OUT_DIR / f"HUC{huc8}"
    watershed = aoi_root / "watershed-data"
    branches  = watershed / "branches"

    if not watershed.exists():
        raise FileNotFoundError(f"watershed-data not found — run Stage 2 first: {watershed}")

    # ── C20: Download gages ──────────────────────────────────────────
    usgs_gages_gpkg = watershed / "usgs_gages.gpkg"
    if not usgs_gages_gpkg.exists():
        boundary_gpkg = watershed / "wbd.gpkg"
        if not boundary_gpkg.exists():
            raise FileNotFoundError(f"HUC boundary not found: {boundary_gpkg}")
        log.info("  C20: downloading USGS gages for HUC8 %s …", huc8)
        gdf = DownloadUSGSGages(out_sr=5070, n_workers=8).download(
            boundary=str(boundary_gpkg),
            aoi_id=huc8,
            out_dir=watershed,
            out_name="usgs_gages.gpkg",
        )
        if gdf.empty:
            raise RuntimeError(f"No USGS gages found for HUC8 {huc8}")
        log.info("  C20 PASS: %d gages", len(gdf))
    else:
        log.info("  C20 SKIP (exists): usgs_gages.gpkg")

    # ── C21: Assign to branches ──────────────────────────────────────
    out_aoi   = watershed / "usgs_subset_gages.gpkg"
    out_bzero = watershed / "usgs_subset_gages_0.gpkg"
    if not out_aoi.exists() or not out_bzero.exists():
        levelpaths_gpkg = watershed / f"{IDENTIFIER}_subset_streams_levelPaths.gpkg"
        if not levelpaths_gpkg.exists():
            raise FileNotFoundError(f"Level-paths gpkg not found: {levelpaths_gpkg}")
        log.info("  C21: assigning gages to branches …")
        result = assign_gages_to_branches(
            usgs_gages_gpkg=usgs_gages_gpkg,
            nwm_streams_levelpaths_gpkg=levelpaths_gpkg,
            aoi_id=huc8,
            out_dir=watershed,
            aoi_filter_column="aoi_id",
            branch_zero_id="0",
        )
        if result is None:
            raise RuntimeError("No gages could be assigned to branches")
        log.info("  C21 PASS: %d gages assigned", len(result.aoi_gages))
    else:
        log.info("  C21 SKIP (exists): usgs_subset_gages.gpkg")

    # ── C22: Per-branch crosswalk ────────────────────────────────────
    aoi_gages_gdf = gpd.read_file(out_aoi)
    gauged_bids   = set(aoi_gages_gdf["levpa_id"].dropna().astype(str).unique()) | {"0"}
    log.info("  C22: branches with gauges = %s", sorted(gauged_bids))

    shared_dem = watershed / "dem.tif"
    if not shared_dem.exists():
        raise FileNotFoundError(f"Shared DEM not found: {shared_dem}")

    for branch_dir in sorted(d for d in branches.iterdir() if d.is_dir()):
        bid = branch_dir.name
        if bid not in gauged_bids:
            continue
        out_table = branch_dir / "usgs_elev_table.csv"
        if out_table.exists():
            log.info("  C22 SKIP branch %s (exists)", bid)
            continue
        gages_gpkg      = out_bzero if bid == "0" else out_aoi
        catchments_gpkg = branch_dir / (
            f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{bid}.gpkg"
        )
        flows_gpkg = branch_dir / (
            f"demDerived_reaches_split_filtered_addedAttributes_crosswalked_{bid}.gpkg"
        )
        missing = [p for p in (catchments_gpkg, flows_gpkg) if not p.exists()]
        if missing:
            log.warning("  C22 SKIP branch %s — missing: %s", bid,
                        ", ".join(p.name for p in missing))
            continue
        branch_dem = branch_dir / f"dem_{bid}.tif"
        dem_path   = branch_dem if branch_dem.exists() else shared_dem
        log.info("  C22: crosswalk branch %s (dem=%s) …", bid, dem_path.name)
        try:
            out = run_branch_crosswalk(
                aoi_gages_gpkg=gages_gpkg,
                branch_catchments_gpkg=catchments_gpkg,
                branch_flows_gpkg=flows_gpkg,
                dem_path=dem_path,
                dem_thalweg_path=dem_path,
                branch_id=bid,
                out_dir=branch_dir,
            )
            written = [p for p in out.values() if p is not None]
            log.info("  branch %s OK: %s", bid,
                     ", ".join(p.name for p in written) if written
                     else "no gages intersected catchments")
        except Exception:
            log.error("  branch %s FAIL:\n%s", bid, traceback.format_exc())

    # ── CON: Consolidate ─────────────────────────────────────────────
    frames = []
    for branch_dir in sorted(d for d in branches.iterdir() if d.is_dir()):
        t = branch_dir / "usgs_elev_table.csv"
        if t.exists():
            df = pd.read_csv(t, dtype={"location_id": str})
            df["levpa_id"] = branch_dir.name
            frames.append(df)
    if frames:
        out_path     = aoi_root / "usgs_elev_table.csv"
        consolidated = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["location_id", "HydroID"]
        )
        consolidated.to_csv(out_path, index=False)
        log.info("  CON PASS: %d gage rows → usgs_elev_table.csv", len(consolidated))
    else:
        log.warning("  CON: no per-branch usgs_elev_table.csv found — no gauges in HUC?")


def main():
    df = pd.read_excel(EXCEL_PATH)
    hucs = [str(int(c)).zfill(8) for c in df[HUC_CODE_COL]]
    done = _load_done()
    remaining = [h for h in hucs if h not in done]

    log.info(f"Crosswalk: {len(hucs)} total | {len(done)} already done | {len(remaining)} to run")
    log.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    batch_start = time.time()
    huc_times: list[tuple[str, float, str]] = []
    passed, failed = list(done), []

    for i, huc8 in enumerate(remaining, 1):
        huc_start = time.time()
        log.info(f"  [{i}/{len(remaining)}] HUC8 = {huc8}  |  "
                 f"batch elapsed: {_fmt(huc_start - batch_start)}")
        try:
            run_huc(huc8)
            elapsed = time.time() - huc_start
            _log_result(huc8, "PASS")
            passed.append(huc8)
            huc_times.append((huc8, elapsed, "PASS"))
            completed   = [t for _, t, s in huc_times if s == "PASS"]
            avg_s       = sum(completed) / len(completed)
            remaining_n = len(remaining) - i
            eta_s       = avg_s * remaining_n
            log.info(f"  [{huc8}] PASS  |  this HUC: {_fmt(elapsed)}  |  "
                     f"avg: {_fmt(avg_s)}  |  remaining: {remaining_n}  |  "
                     f"ETA: {_fmt(eta_s)}"
                     + (f"  (~{datetime.fromtimestamp(time.time() + eta_s).strftime('%H:%M')})"
                        if remaining_n > 0 else "  (last HUC)"))
        except Exception:
            elapsed = time.time() - huc_start
            err = traceback.format_exc().splitlines()[-1]
            _log_result(huc8, "FAIL", err)
            failed.append(huc8)
            huc_times.append((huc8, elapsed, "FAIL"))
            log.error(f"  [{huc8}] FAIL after {_fmt(elapsed)}  |  {err}")

    total = time.time() - batch_start
    log.info(f"\n{'─'*60}")
    log.info(f"Crosswalk complete  |  total time: {_fmt(total)}")
    log.info(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Passed: {len(passed)}  |  Failed: {len(failed)}")
    if huc_times:
        log.info(f"\nPer-HUC timing summary:")
        for huc8, elapsed, status in huc_times:
            log.info(f"  {huc8}  {status:4s}  {_fmt(elapsed)}")
    if failed:
        log.warning(f"\nFailed HUCs: {failed}")
        log.warning("Re-run this script to retry (FAIL lines are automatically retried).")


if __name__ == "__main__":
    main()
