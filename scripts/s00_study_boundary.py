from pathlib import Path
import pandas as pd
import geopandas as gpd
from shapely import wkt

# ── EDIT THESE ───────────────────────────────────────────────────
EXCEL_PATH = Path(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\study_area.xlsx")

# "wkt"     — use the wkt_geom column already in the Excel (requires knowing the CRS)
# "service" — fetch boundaries by HUC_CODE from the USGS web service (no CRS needed)
MODE = "service"

# Used only when MODE = "wkt". Confirm the CRS with your data source.
# Coordinate values ~1.5M–1.65M suggest EPSG:5070 (NAD83 Conus Albers).
WKT_CRS = "EPSG:5069"

# Optional: list specific HUC_CODE values to include.
# Leave as [] to use ALL rows in the Excel.
HUC_CODES = []   # e.g. [3020102, 3020103, 3020104]
# ─────────────────────────────────────────────────────────────────

OUT_PATH = Path("docs/study_boundary/study_area.gpkg")

df = pd.read_excel(EXCEL_PATH)
print(f"Loaded {len(df)} rows from Excel")

if HUC_CODES:
    df = df[df["HUC_CODE"].isin(HUC_CODES)]
    print(f"Filtered to {len(df)} rows matching HUC_CODES")

if MODE == "wkt":
    # ── Option A: use WKT geometry already in the Excel ──────────
    df["geometry"] = df["wkt_geom"].apply(wkt.loads)
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=WKT_CRS)

elif MODE == "service":
    # ── Option B: fetch boundaries from USGS web service ─────────
    # HUC_CODE in the Excel may be 7-digit (e.g. 3020102); zero-pad to 8.
    from fimbox import HUC8Finder
    finder = HUC8Finder()
    frames = []
    skipped = []
    for code in df["HUC_CODE"]:
        huc8 = str(int(code)).zfill(8)
        print(f"  Fetching HUC8 {huc8} ...")
        try:
            frames.append(finder.from_huc8(huc8))
        except ValueError:
            print(f"  WARNING: HUC8 {huc8} not found in service — skipping")
            skipped.append(huc8)
    if skipped:
        print(f"\nSkipped {len(skipped)} HUC(s) not found in service: {skipped}")
    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry")

else:
    raise ValueError(f"MODE must be 'wkt' or 'service', got {MODE!r}")

dissolved = gdf.dissolve().reset_index(drop=True)[["geometry"]]
# Simplify to reduce vertex count — the ArcGIS FeatureServer rejects
# overly complex geometries as spatial filters (returns HTTP 400).
dissolved["geometry"] = dissolved["geometry"].simplify(0.01, preserve_topology=True)

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
dissolved.to_file(OUT_PATH, driver="GPKG", index=False)
print(f"Saved merged boundary ({len(df)} HUCs dissolved) --> {OUT_PATH}")
