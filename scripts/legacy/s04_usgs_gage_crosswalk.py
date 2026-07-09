"""
USGS Gage Crosswalk for a single HUC8.
Implements steps C20, C21, C22 from the repo (currently commented-out in tests).

Steps:
  C20  Download USGS gauge points within the HUC8 boundary from ArcGIS Online
  C21  Assign each gauge to a NWM level path (branch ID)
  C22  Per-branch: snap gauges to thalweg, sample DEM to get dem_adj_elevation
  CON  Consolidate all per-branch usgs_elev_table.csv into one at AOI root
       (required by Stage 4 UsgsRatingCalibrator and the n-calibration script)

Prerequisite: Stage 2 must have completed (all branch directories must exist).

Run:
    .venv\\Scripts\\python.exe scripts/run_usgs_gage_crosswalk.py
"""
import logging
import traceback
from pathlib import Path

import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────
HUC8    = "03020102"
OUT_DIR = Path("D:/SI/out")
# ─────────────────────────────────────────────────────────────────

IDENTIFIER    = "nwmmr"
AOI_ROOT      = OUT_DIR / f"HUC{HUC8}"
WATERSHED     = AOI_ROOT / "watershed-data"
BRANCHES      = WATERSHED / "branches"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def step_c20_download_gages() -> Path:
    """Download USGS gauge points within the HUC8 boundary."""
    from fimbox import DownloadUSGSGages

    out_path = WATERSHED / "usgs_gages.gpkg"
    if out_path.exists():
        log.info("C20 SKIP (exists): %s", out_path.name)
        return out_path

    boundary_gpkg = WATERSHED / "wbd.gpkg"
    if not boundary_gpkg.exists():
        raise FileNotFoundError(f"HUC boundary not found: {boundary_gpkg}")

    log.info("C20: Downloading USGS gages for HUC8 %s …", HUC8)
    gdf = DownloadUSGSGages(out_sr=5070, n_workers=8).download(
        boundary=str(boundary_gpkg),
        aoi_id=HUC8,
        out_dir=WATERSHED,
        out_name="usgs_gages.gpkg",
    )
    if gdf.empty:
        raise RuntimeError(f"No USGS gages found for HUC8 {HUC8}")

    log.info("C20 PASS: %d gages --> %s", len(gdf), out_path.name)
    return out_path


def step_c21_assign_to_branches(usgs_gages_gpkg: Path) -> tuple[Path, Path]:
    """Tag every gauge with a feature_id + levpa_id (branch ID)."""
    from fimbox import assign_gages_to_branches

    out_aoi   = WATERSHED / "usgs_subset_gages.gpkg"
    out_bzero = WATERSHED / f"usgs_subset_gages_0.gpkg"

    if out_aoi.exists() and out_bzero.exists():
        log.info("C21 SKIP (exists): %s + %s", out_aoi.name, out_bzero.name)
        return out_aoi, out_bzero

    levelpaths_gpkg = WATERSHED / f"{IDENTIFIER}_subset_streams_levelPaths.gpkg"
    if not levelpaths_gpkg.exists():
        raise FileNotFoundError(f"Level-paths gpkg not found: {levelpaths_gpkg}")

    log.info("C21: Assigning gages to branches …")
    result = assign_gages_to_branches(
        usgs_gages_gpkg=usgs_gages_gpkg,
        nwm_streams_levelpaths_gpkg=levelpaths_gpkg,
        aoi_id=HUC8,
        out_dir=WATERSHED,
        aoi_filter_column="aoi_id",
        branch_zero_id="0",
    )
    if result is None:
        raise RuntimeError("No gages could be assigned to branches")

    log.info("C21 PASS: %d gages assigned", len(result.aoi_gages))
    return result.aoi_gages_path, result.branch_zero_gages_path


def step_c22_per_branch_crosswalk(aoi_gages_gpkg: Path, bzero_gages_gpkg: Path) -> None:
    """
    Snap gauges to the DEM thalweg in every branch; writes usgs_elev_table.csv per branch.

    Stage 2 (HAND) cleans up per-branch DEMs after use.  The shared
    ``watershed-data/dem.tif`` covers the full AOI and is used as both the
    raw-elevation and thalweg-elevation raster.  The small bias this introduces
    (thalweg-conditioned DEM is slightly lower at channels) is acceptable when
    the goal is to correct only gauged SRCs.
    """
    from fimbox import run_branch_crosswalk

    # Shared DEM — covers entire watershed, used for all branches.
    shared_dem = WATERSHED / "dem.tif"
    if not shared_dem.exists():
        raise FileNotFoundError(f"Shared DEM not found: {shared_dem}")

    # Identify which branches actually contain gauges to avoid unnecessary work.
    import geopandas as gpd
    aoi_gages = gpd.read_file(aoi_gages_gpkg)
    gauged_branch_ids = set(aoi_gages["levpa_id"].dropna().astype(str).unique())
    # Branch zero always gets a pass.
    gauged_branch_ids.add("0")
    log.info("C22: branches with gauges = %s", sorted(gauged_branch_ids))

    branch_dirs = sorted(d for d in BRANCHES.iterdir() if d.is_dir())
    if not branch_dirs:
        raise FileNotFoundError(f"No branch directories under {BRANCHES}")

    for branch_dir in branch_dirs:
        bid = branch_dir.name
        if bid not in gauged_branch_ids:
            log.debug("C22 SKIP branch %s (no gauges)", bid)
            continue

        out_table = branch_dir / "usgs_elev_table.csv"
        if out_table.exists():
            log.info("C22 SKIP branch %s (exists)", bid)
            continue

        # Branch zero uses the dedicated branch-zero gage set (levpa_id="0")
        gages_gpkg = bzero_gages_gpkg if bid == "0" else aoi_gages_gpkg

        catchments_gpkg = branch_dir / (
            f"gw_catchments_reaches_filtered_addedAttributes_crosswalked_{bid}.gpkg"
        )
        flows_gpkg = branch_dir / (
            f"demDerived_reaches_split_filtered_addedAttributes_crosswalked_{bid}.gpkg"
        )

        missing = [p for p in (catchments_gpkg, flows_gpkg) if not p.exists()]
        if missing:
            log.warning("C22 SKIP branch %s — missing: %s", bid,
                        ", ".join(p.name for p in missing))
            continue

        # Use per-branch dem if it survived cleanup; otherwise fall back to the shared DEM.
        branch_dem = branch_dir / f"dem_{bid}.tif"
        dem_path = branch_dem if branch_dem.exists() else shared_dem

        log.info("C22: crosswalk branch %s (dem=%s) …", bid, dem_path.name)
        try:
            out = run_branch_crosswalk(
                aoi_gages_gpkg=gages_gpkg,
                branch_catchments_gpkg=catchments_gpkg,
                branch_flows_gpkg=flows_gpkg,
                dem_path=dem_path,
                dem_thalweg_path=dem_path,   # shared DEM doubles as thalweg DEM
                branch_id=bid,
                out_dir=branch_dir,
            )
            written = [p for p in out.values() if p is not None]
            if written:
                log.info("  branch %s OK: %s", bid, ", ".join(p.name for p in written))
            else:
                log.info("  branch %s: no gages intersected catchments", bid)
        except Exception:
            log.error("  branch %s FAIL:\n%s", bid, traceback.format_exc())


def step_con_consolidate() -> Path:
    """
    Merge all per-branch usgs_elev_table.csv files into one at the AOI root.
    Stage 4 UsgsRatingCalibrator and the n-calibration script both read from
    aoi_root/usgs_elev_table.csv.
    """
    out_path = AOI_ROOT / "usgs_elev_table.csv"

    frames = []
    for branch_dir in sorted(d for d in BRANCHES.iterdir() if d.is_dir()):
        t = branch_dir / "usgs_elev_table.csv"
        if t.exists():
            df = pd.read_csv(t, dtype={"location_id": str})
            df["levpa_id"] = branch_dir.name
            frames.append(df)

    if not frames:
        log.warning("CON: no per-branch usgs_elev_table.csv found — no gauges in HUC?")
        return out_path

    consolidated = pd.concat(frames, ignore_index=True)
    consolidated = consolidated.drop_duplicates(subset=["location_id", "HydroID"])
    consolidated.to_csv(out_path, index=False)
    log.info("CON PASS: %d gage rows --> %s", len(consolidated), out_path)
    return out_path


def main():
    log.info("=== USGS Gage Crosswalk — HUC8 %s ===", HUC8)

    usgs_gages_gpkg = step_c20_download_gages()
    aoi_gages_gpkg, bzero_gages_gpkg = step_c21_assign_to_branches(usgs_gages_gpkg)
    step_c22_per_branch_crosswalk(aoi_gages_gpkg, bzero_gages_gpkg)
    elev_table = step_con_consolidate()

    log.info("=== Done. usgs_elev_table.csv --> %s ===", elev_table)


if __name__ == "__main__":
    main()
