"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date Created: May 2026

Combined preprocessing pipeline. Given a HUC8 ID or a boundary file, downloads
and preprocesses all datasets needed for FIM, saving everything flat into one
case folder with the same filenames used by inundation-mapping's wbd pre-clip.

Output layout
------------
    wbd.gpkg                         -- exact study boundary (HUC8 polygon or user file)
    wbd_buffered.gpkg               -- buffered boundary used for all data downloads
    DEM_Domain.gpkg                 -- DEM coverage intersecting the buffered boundary
    LandSea_subset.gpkg             -- land/sea/Great Lakes mask intersecting the buffered boundary
    dem.tif                          -- 3DEP DEM (10 m default), clipped to inner buffer
    nwm_subset_streams.gpkg          -- NWM flowlines
    nwm_catchments_proj_subset.gpkg  -- NWM catchments
    nwm_lakes_proj_subset.gpkg       -- NWM lakes
    nwm_headwater_points_subset.gpkg -- headwater points derived from flowlines
    nld_subset_levees.gpkg           -- raw NLD levee lines (for levee-path association)
    3d_nld_subset_levees_burned.gpkg -- elevation-filtered levee lines for DEM burning
    LeveeProtectedAreas_subset.gpkg  -- NLD leveed / protected-area polygons
    osm_roads_subset.gpkg            -- OSM roads (bridges excluded)
    osm_bridges_subset.gpkg          -- OSM bridges
    fema_nfhl_subset.gpkg            -- FEMA NFHL flood zones
    preprocess.log                   -- single combined log
"""

import logging
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
from shapely.geometry import LineString, MultiLineString

from shapely.geometry import box as shapely_box

from ..logging_utils import attach_case_log, get_logger
from .download_data.dem_process import DEMProcessor
from .download_data.area_masks import DownloadDEMDomain, DownloadLandSea
from .download_data.nfhl_data import DownloadFEMANFHL
from .download_data.nhdplus import getNHDPlusData
from .download_data.nld_data import DownloadNLD
from .download_data.osm_data import DownloadOSMBridges, DownloadOSMRoads
from .download_data.utils import HUC8Finder, find_headwater_points

_FILENAMES = {
    "wbd": "wbd.gpkg",
    "wbd_buffer": "wbd_buffered.gpkg",
    "dem_domain": "DEM_Domain.gpkg",
    "landsea": "LandSea_subset.gpkg",
    "dem": "dem.tif",
    "nfhl": "fema_nfhl_subset.gpkg",
    "nwm_streams": "nwm_subset_streams.gpkg",
    "nwm_catchments": "nwm_catchments_proj_subset.gpkg",
    "nwm_lakes": "nwm_lakes_proj_subset.gpkg",
    "nwm_headwaters": "nwm_headwater_points_subset.gpkg",
    "levee_lines": "nld_subset_levees.gpkg",
    "levee_lines_burned": "3d_nld_subset_levees_burned.gpkg",
    "levee_protected_areas": "LeveeProtectedAreas_subset.gpkg",
    "osm_roads": "osm_roads_subset.gpkg",
    "osm_bridges": "osm_bridges_subset.gpkg",
}


# HUC2 bounding boxes for NLD min-Z threshold selection.
_HUC2_BBOX = {
    "01": (
        -73.737982,
        40.939103,
        -66.018747,
        48.099706,
    ),  # New England     — coastal, min_z=0.01
    "02": (
        -80.540991,
        36.669417,
        -71.789711,
        44.153046,
    ),  # Mid Atlantic     — coastal, min_z=0.01
    "03": (
        -90.623497,
        24.395330,
        -75.398098,
        37.521035,
    ),  # South Atl-Gulf   — coastal, min_z=0.01
    "08": (
        -94.338914,
        28.854302,
        -88.289407,
        37.861335,
    ),  # Lower Mississippi — below-sea, min_z=-10
    "12": (
        -103.870535,
        25.854437,
        -93.145981,
        34.688972,
    ),  # Texas-Gulf       — coastal, min_z=0.01
}


# NLD levee-line preprocessing
def _derive_huc2_from_boundary(boundary_gdf: gpd.GeoDataFrame) -> str:
    """
    Determine HUC2 by checking which HUC2 bbox the boundary centroid falls in.
    Uses the largest-overlap bbox when the centroid is ambiguous.
    Returns "00" (default 1-ft threshold) when no special region matches.
    """
    bounds = boundary_gdf.to_crs("EPSG:4326").total_bounds  # xmin, ymin, xmax, ymax
    aoi = shapely_box(*bounds)
    best_huc2, best_area = "00", 0.0
    for huc2, bbox in _HUC2_BBOX.items():
        region = shapely_box(*bbox)
        inter = aoi.intersection(region).area
        if inter > best_area:
            best_area = inter
            best_huc2 = huc2
    return best_huc2


def _remove_null_z_vertices(geom: LineString, huc2: str):
    """
    Strip vertices below the HUC2-specific minimum Z threshold, convert
    surviving Z values from feet to metres, and return a (Multi)LineString.

    Up to 5 consecutive bad vertices are bridged without splitting so that
    short road crossings with missing elevation don't fragment the levee.
    Returns None when no valid segment remains.
    """
    if huc2 in ("01", "02", "03", "12"):  # coastal — near-zero elevations valid
        min_z = 0.01
    elif huc2 == "08":  # Louisiana — below-sea-level levees
        min_z = -10.0
    else:  # default including "00" (no special region)
        min_z = 1.0

    out_segments, current_part = [], []
    skipped, max_skip = 0, 5

    for coord in geom.coords:
        if len(coord) < 3:
            if skipped < max_skip:
                skipped += 1
            else:
                if len(current_part) > 1:
                    out_segments.append(LineString(current_part))
                current_part, skipped = [], 0
            continue

        z = coord[2]
        if z > min_z:
            current_part.append((coord[0], coord[1], z * 0.3048))  # ft --> m
            skipped = 0
        elif skipped < max_skip:
            skipped += 1
        else:
            if len(current_part) > 1:
                out_segments.append(LineString(current_part))
            current_part, skipped = [], 0

    if len(current_part) > 1:
        out_segments.append(LineString(current_part))

    if not out_segments:
        return None
    return MultiLineString(out_segments)


def preprocess_nld_lines(
    levee_gdf: gpd.GeoDataFrame,
    out_path: Path,
    huc2: Optional[str] = None,
    boundary_gdf: Optional[gpd.GeoDataFrame] = None,
) -> gpd.GeoDataFrame:
    """
    Apply inundation-mapping NLD preprocessing to raw levee lines:
      1. Derive HUC2 from the boundary if not supplied.
      2. Remove / trim vertices with no usable Z elevation.
      3. Convert surviving Z values from feet to metres.
      4. Drop rows whose geometry is empty after filtering.

    Parameters
    ----------
    levee_gdf : GeoDataFrame
        Raw NLD line features.
    out_path : Path
        Where to write the preprocessed GeoPackage.

    Returns
    -------
    GeoDataFrame (may be empty if no features survive)
    """
    gdf = levee_gdf.copy()

    if huc2 is None:
        if boundary_gdf is not None:
            huc2 = _derive_huc2_from_boundary(boundary_gdf)
        else:
            # fall back: try existing HUC columns, else query-derived default
            for col in ("HUC8", "huc8", "HUC_8", "HUC2", "huc2"):
                if col in gdf.columns:
                    huc2 = str(gdf[col].iloc[0])[:2]
                    break
            else:
                huc2 = "08"

    # Explode any MultiLineStrings so _remove_null_z_vertices always gets a single LineString
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)

    log = get_logger(__name__)
    total = len(gdf)
    log.info(f"Filtering levee vertices (huc2={huc2}): {total} features")
    results = [_remove_null_z_vertices(row.geometry, huc2) for row in gdf.itertuples()]
    gdf["geometry"] = results
    gdf = gdf[gdf["geometry"].notna() & ~gdf.is_empty].copy()

    if not gdf.empty:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(out_path, driver="GPKG", index=False)
    return gdf


# Main pipeline
class getAllInputData:
    """
    Combined preprocessing pipeline for a FIM study area.

    Parameters
    ----------
    huc8 : str, optional
        8-digit HUC ID — boundary fetched from the hosted HUC8 service.
    boundary : str or Path, optional
        Path to a boundary shapefile / GeoPackage / GeoJSON.
    boundary_layer : str, optional
        Layer name when boundary is a GeoPackage with multiple layers.
    out_dir : str or Path, optional
        Root output directory. Defaults to ``./fimbox_preprocess``.
    epsg : int, optional
        Output CRS. Defaults to 5070 (CONUS Albers).
    dem_resolution : int, optional
        3DEP DEM resolution in metres. Default 10.
    buffer_m : float, optional
        Buffer distance in metres applied to the boundary before downloading
        all datasets. Default 2000.
    headwater_buffer_cells : int, optional
        Number of DEM cells used for the inner clip when deriving headwaters

    Either ``huc8`` or ``boundary`` must be provided.
    """

    def __init__(
        self,
        huc8: Optional[str] = None,
        boundary: Optional[Union[str, Path]] = None,
        boundary_layer: Optional[str] = None,
        out_dir: Optional[Union[str, Path]] = None,
        epsg: int = 5070,
        dem_resolution: int = 10,
        buffer_m: float = 2000.0,
        headwater_buffer_cells: int = 8,
    ):
        if huc8 is None and boundary is None:
            raise ValueError("Provide either huc8 or boundary.")

        self.huc8 = huc8
        self.boundary_path = Path(boundary) if boundary else None
        self.boundary_layer = boundary_layer
        self.epsg = epsg
        self.dem_resolution = dem_resolution
        self.buffer_m = buffer_m
        self.headwater_buffer_cells = headwater_buffer_cells

        self.case_name = f"HUC{huc8}" if huc8 else self.boundary_path.stem

        root = Path(out_dir) if out_dir else Path("fimbox_preprocess")
        self.case_dir = root / self.case_name
        self.case_dir.mkdir(parents=True, exist_ok=True)

        self._setup_logger()

        # Load exact boundary, create buffer, apply DEM-domain and land/sea masks,
        # then save the cleaned boundaries used by all later downloads.
        self.boundary_gdf: gpd.GeoDataFrame = self._load_boundary()
        self.buffer_gdf: gpd.GeoDataFrame = self._make_buffer()

        # Derive HUC2 once from the exact boundary for NLD preprocessing
        self.huc2 = _derive_huc2_from_boundary(self.boundary_gdf)
        self.logger.info(f"Derived HUC2: {self.huc2}")

        self._apply_dem_domain_and_landsea()
        self._save_boundaries()

        self.logger.info(f"Case: {self.case_name}  |  Output: {self.case_dir}")

    # helpers
    def _out(self, key: str) -> Path:
        return self.case_dir / _FILENAMES[key]

    def _setup_logger(self):
        # All fimbox modules log under `fimbox.*` via getLogger(__name__);
        # attaching handlers to the `fimbox` root makes every nested log
        # call land in this case's preprocess.log as well as stdout.
        attach_case_log(self.case_dir)
        self.logger = get_logger(f"fimbox.preprocess.{self.case_name}")

    @staticmethod
    def _drop_fid(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        # pyogrio reserves 'fid' as an internal OGR field — drop it before saving
        reserved = [c for c in gdf.columns if c.lower() == "fid"]
        return gdf.drop(columns=reserved) if reserved else gdf

    def _load_boundary(self) -> gpd.GeoDataFrame:
        if self.huc8:
            self.logger.info(
                f"Fetching HUC8 boundary for {self.huc8} from hosted service..."
            )
            gdf = HUC8Finder().from_huc8(self.huc8)
            if gdf.empty:
                raise ValueError(f"HUC8 {self.huc8!r} not found.")
            return self._drop_fid(gdf.to_crs("EPSG:4326"))

        gdf = (
            gpd.read_file(self.boundary_path, layer=self.boundary_layer)
            if self.boundary_layer
            else gpd.read_file(self.boundary_path)
        )
        if gdf.crs is None:
            raise ValueError("Boundary file has no CRS.")
        return self._drop_fid(gdf.to_crs("EPSG:4326"))

    def _make_buffer(self) -> gpd.GeoDataFrame:
        projected = self.boundary_gdf.to_crs(epsg=self.epsg)
        buffered = projected.copy()
        buffered["geometry"] = projected.geometry.buffer(self.buffer_m)
        return buffered.to_crs("EPSG:4326")

    def _apply_dem_domain_and_landsea(self):
        self.logger.info("--- DEM domain / landsea masks ---")
        boundary = self.boundary_gdf.to_crs(epsg=self.epsg)
        buffered = self.buffer_gdf.to_crs(epsg=self.epsg)

        try:
            dem_domain = DownloadDEMDomain(out_sr=self.epsg).download(
                boundary=self.buffer_gdf
            )
            if dem_domain is not None and not dem_domain.empty:
                dem_domain = gpd.clip(dem_domain.to_crs(epsg=self.epsg), buffered)
                if not dem_domain.empty:
                    dem_domain.to_file(
                        self._out("dem_domain"), driver="GPKG", index=False
                    )
                    boundary = gpd.clip(boundary, dem_domain)
                    buffered = gpd.clip(buffered, dem_domain)
                    self.logger.info(
                        f"DEM domain applied --> {_FILENAMES['dem_domain']}"
                    )
                else:
                    self.logger.warning(
                        "DEM domain service returned no overlap after clipping."
                    )
            else:
                self.logger.warning(
                    "DEM domain service returned no intersecting features."
                )
        except Exception as exc:
            self.logger.error(
                f"DEM domain mask failed; continuing with original boundary: {exc}",
                exc_info=True,
            )

        try:
            landsea = DownloadLandSea(out_sr=self.epsg).download(
                boundary=buffered.to_crs("EPSG:4326")
            )
            if landsea is not None and not landsea.empty:
                landsea = gpd.clip(landsea.to_crs(epsg=self.epsg), buffered)
                if not landsea.empty:
                    landsea.to_file(self._out("landsea"), driver="GPKG", index=False)
                    boundary = boundary.overlay(landsea[["geometry"]], how="difference")
                    buffered = buffered.overlay(landsea[["geometry"]], how="difference")
                    self.logger.info(
                        f"Land/sea mask applied --> {_FILENAMES['landsea']}"
                    )
                else:
                    self.logger.info(
                        "Land/sea service returned no overlap after clipping."
                    )
            else:
                self.logger.info("Land/sea service returned no intersecting features.")
        except Exception as exc:
            self.logger.error(
                f"Land/sea mask failed; continuing without land/sea subtraction: {exc}",
                exc_info=True,
            )

        self.boundary_gdf = self._drop_fid(
            boundary[~boundary.is_empty].to_crs("EPSG:4326")
        )
        self.buffer_gdf = self._drop_fid(
            buffered[~buffered.is_empty].to_crs("EPSG:4326")
        )

    def _save_boundaries(self):
        self.boundary_gdf.to_file(self._out("wbd"), driver="GPKG", index=False)
        self.logger.info(f"Study boundary --> {_FILENAMES['wbd']}")

        # wbd8_clp.gpkg is the canonical name expected by split_reaches and filter_catchments
        wbd8_clp_path = self.case_dir / "wbd8_clp.gpkg"
        self.boundary_gdf.to_file(str(wbd8_clp_path), driver="GPKG", index=False)
        self.logger.info(f"Study boundary (clipped) --> wbd8_clp.gpkg")

        self.buffer_gdf.to_file(self._out("wbd_buffer"), driver="GPKG", index=False)
        self.logger.info(
            f"Buffered boundary ({self.buffer_m} m) --> {_FILENAMES['wbd_buffer']}"
        )

    def _skip(self, key: str) -> bool:
        p = self._out(key)
        if p.exists():
            self.logger.info(f"SKIP (exists): {p.name}")
            return True
        return False

    # individual steps — all use buffer_gdf for downloads
    def run_dem(self):
        if self._skip("dem"):
            return
        self.logger.info("--- DEM ---")
        try:
            DEMProcessor(
                boundary=self.buffer_gdf,
                output_dir=str(self.case_dir),
                out_name=_FILENAMES["dem"],
                resolution=self.dem_resolution,
                epsg=self.epsg,
            )
            self.logger.info(f"DEM --> {_FILENAMES['dem']}")
        except Exception as exc:
            self.logger.error(f"DEM failed: {exc}", exc_info=True)

    def run_nhd(self):
        all_exist = all(
            self._out(k).exists()
            for k in ("nwm_streams", "nwm_catchments", "nwm_lakes")
        )
        if all_exist:
            self.logger.info(f"SKIP (exists): nwm_subset_streams/catchments/lakes")
            return
        self.logger.info("--- NWM Flowlines / Catchments / Lakes ---")
        try:
            results = getNHDPlusData(
                boundary=self.buffer_gdf,
                out_dir=str(self.case_dir),
                epsg=self.epsg,
                download_flowlines=True,
                download_catchments=True,
                download_lakes=True,
            )
            if results.get("flowlines") is not None and not results["flowlines"].empty:
                self.logger.info(f"NWM streams --> {_FILENAMES['nwm_streams']}")
                self._run_headwaters(results["flowlines"])
            if (
                results.get("catchments") is not None
                and not results["catchments"].empty
            ):
                self.logger.info(f"NWM catchments --> {_FILENAMES['nwm_catchments']}")
            if results.get("lakes") is not None and not results["lakes"].empty:
                self.logger.info(f"NWM lakes --> {_FILENAMES['nwm_lakes']}")
        except Exception as exc:
            self.logger.error(f"NHD failed: {exc}", exc_info=True)

    def _run_headwaters(self, flowlines_gdf: gpd.GeoDataFrame):
        """Derive headwater points from flowlines, clipped to inner buffer."""
        try:
            # clip flowlines to buffer_gdf shrunk by headwater_buffer_cells pixels
            shrink = -(self.headwater_buffer_cells * self.dem_resolution)
            projected = self.buffer_gdf.to_crs(epsg=self.epsg)
            inner_gdf = projected.copy()
            inner_gdf["geometry"] = projected.geometry.buffer(shrink)
            inner_gdf = inner_gdf[~inner_gdf.is_empty]

            fl = flowlines_gdf.to_crs(epsg=self.epsg)
            if not inner_gdf.empty:
                fl = gpd.clip(fl, inner_gdf)

            hw = find_headwater_points(fl)
            if not hw.empty:
                hw.to_file(self._out("nwm_headwaters"), driver="GPKG", index=False)
                self.logger.info(
                    f"Headwaters ({len(hw)} points) --> {_FILENAMES['nwm_headwaters']}"
                )
            else:
                self.logger.warning("Headwaters: no points found.")
        except Exception as exc:
            self.logger.error(f"Headwater derivation failed: {exc}", exc_info=True)

    @staticmethod
    def _levee_lines_have_z(path: Path) -> bool:
        try:
            gdf = gpd.read_file(path)
            return bool(gdf.has_z.any())
        except Exception:
            return False

    def run_nld(self):
        lines_path = self._out("levee_lines")
        burned_exist = self._out("levee_lines_burned").exists()
        polys_exist = self._out("levee_protected_areas").exists()

        # treat a Z-less lines file as missing — it came from an old download
        lines_exist = lines_path.exists() and self._levee_lines_have_z(lines_path)
        if not lines_exist and lines_path.exists():
            self.logger.warning("Existing levee lines file has no Z — re-downloading.")
            lines_path.unlink()

        if lines_exist and burned_exist and polys_exist:
            self.logger.info("SKIP (exists): all NLD files present")
            return

        self.logger.info("--- NLD ---")
        if not lines_exist:
            try:
                DownloadNLD(
                    boundary=self.buffer_gdf,
                    out_dir=str(self.case_dir),
                    epsg=self.epsg,
                    lines_name=_FILENAMES["levee_lines"],
                    polys_name=_FILENAMES["levee_protected_areas"],
                )
            except Exception as exc:
                self.logger.error(f"NLD download failed: {exc}", exc_info=True)
                return

        if not burned_exist:
            try:
                lines_path = self._out("levee_lines")
                if lines_path.exists():
                    self.logger.info(f"NLD raw lines --> {_FILENAMES['levee_lines']}")
                    raw_gdf = gpd.read_file(lines_path)
                    burned = preprocess_nld_lines(
                        raw_gdf,
                        self._out("levee_lines_burned"),
                        huc2=self.huc2,
                    )
                    if burned.empty:
                        self.logger.warning(
                            "Levee burn lines: no features survived null-Z filter."
                        )
                    else:
                        self.logger.info(
                            f"Levee burn lines ({len(burned)} features) --> "
                            f"{_FILENAMES['levee_lines_burned']}"
                        )
                else:
                    self.logger.warning(
                        "NLD: no levee lines found within this boundary."
                    )
            except Exception as exc:
                self.logger.error(
                    f"NLD line preprocessing failed: {exc}", exc_info=True
                )

        if not polys_exist:
            polys_path = self._out("levee_protected_areas")
            if polys_path.exists():
                self.logger.info(
                    f"Levee protected areas --> {_FILENAMES['levee_protected_areas']}"
                )
            else:
                self.logger.warning(
                    "NLD: no levee protected-area polygons found within this boundary."
                )

    def run_osm(self):
        if not self._skip("osm_roads"):
            self.logger.info("--- OSM Roads ---")
            try:
                DownloadOSMRoads().download(
                    boundary=self.buffer_gdf,
                    out_dir=str(self.case_dir),
                    out_name=_FILENAMES["osm_roads"],
                    out_layer="osm_roads",
                )
                self.logger.info(f"OSM roads --> {_FILENAMES['osm_roads']}")
            except Exception as exc:
                self.logger.error(f"OSM roads failed: {exc}", exc_info=True)

        if not self._skip("osm_bridges"):
            self.logger.info("--- OSM Bridges ---")
            try:
                DownloadOSMBridges().download(
                    boundary=self.buffer_gdf,
                    out_dir=str(self.case_dir),
                    out_name=_FILENAMES["osm_bridges"],
                    out_layer="osm_bridges",
                )
                self.logger.info(f"OSM bridges --> {_FILENAMES['osm_bridges']}")
            except Exception as exc:
                self.logger.error(f"OSM bridges failed: {exc}", exc_info=True)

    # full pipeline
    def run(self):
        self.logger.info(f"=== PreprocessAll: {self.case_name} ===")
        self.logger.info(f"Output: {self.case_dir}")
        self.run_dem()
        self.run_nhd()
        self.run_nld()
        self.run_osm()
        self.logger.info("=== ALL STEPS COMPLETE ===")
        self._log_summary()

    def _log_summary(self):
        files = sorted(
            f
            for f in self.case_dir.iterdir()
            if f.suffix in (".gpkg", ".tif", ".log") and f.is_file()
        )
        self.logger.info(f"--- Summary: {self.case_name} ({len(files)} files) ---")
        for f in files:
            size_kb = f.stat().st_size // 1024
            self.logger.info(f"  {f.name:<45}  {size_kb:>6} KB")


# CLI
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Download and preprocess all FIM input data for a HUC8 or boundary."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--huc8", help="HUC8 ID (e.g. 08060202)")
    src.add_argument("--boundary", help="Path to boundary file (gpkg/shp/etc.)")
    parser.add_argument("--boundary-layer", default=None)
    parser.add_argument("--out-dir", default="fimbox_preprocess")
    parser.add_argument("--epsg", type=int, default=5070)
    parser.add_argument("--dem-resolution", type=int, default=10)
    parser.add_argument("--buffer-m", type=float, default=2000.0)
    parser.add_argument("--headwater-buffer-cells", type=int, default=8)
    args = parser.parse_args()

    pp = getAllInputData(
        huc8=args.huc8,
        boundary=args.boundary,
        boundary_layer=args.boundary_layer,
        out_dir=args.out_dir,
        epsg=args.epsg,
        dem_resolution=args.dem_resolution,
        buffer_m=args.buffer_m,
        headwater_buffer_cells=args.headwater_buffer_cells,
    )
    pp.run()
