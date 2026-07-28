"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date Created: May 2026

Combined preprocessing pipeline. Given a HUC8 ID or a boundary file, downloads
and preprocesses all datasets needed for FIM, saving everything into an AOI
folder (named after the HUC8 / boundary) with the canonical filenames the
downstream branch and HAND steps expect.

Output layout
------------
    <AOI_name>/                      -- AOI root (HUC<huc8> or boundary stem)
      processing.log                 -- single combined log (preprocess + branches + FIM)
      feature_id.csv                 -- unique NWM feature_ids (written by FIM extract step)
      watershed-data/                -- ALL input data + processing + branches
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
        branches/<B>/ ...                -- per-branch HAND processing (later stages)
      discharge-inputs/              -- discharge forecast CSV(s) (FIM input)
      fim-outputs/                   -- final inundation depth / extent rasters

``watershed-data/`` is the directory downstream stages take as their
``aoi_dir`` (branch derivation, branch processing, calibration, FIM
generation). Use ``self.aoi_dir`` for the AOI root and ``self.watershed_dir``
(== ``self.case_dir``) for the input-data folder.
"""

from pathlib import Path
from typing import Optional, Sequence, Union

import geopandas as gpd
from shapely.geometry import LineString, MultiLineString
from shapely.geometry import box as shapely_box
from shapely.ops import unary_union

from ..logging_utils import (
    WATERSHED_DIR_NAME,
    attach_case_log,
    default_output_dir,
    get_logger,
)
from .aoi_from_ids import (
    group_label,
    huc_label,
    hucs_to_boundary,
    normalize_groups,
    normalize_huc_groups,
    resolve_reach_group,
)
from .download_data.area_masks import DownloadDEMDomain, DownloadLandSea
from .download_data.dem_process import DEMProcessor
from .download_data.nhdplus import (
    NGEN,
    NWM_MEDIUM,
    SOURCE_LABELS,
    SOURCES,
    getNHDPlusData,
    normalize_catchments,
    normalize_flowlines,
    normalize_source,
)
from .download_data.nld_data import DownloadNLD
from .download_data.osm_data import DownloadOSMBridges, DownloadOSMRoads
from .download_data.usgs_gages import DownloadUSGSGages
from .download_data.utils import HUC8Finder, find_headwater_points
from .source_naming import DEFAULT_IDENTIFIER, source_name

log = get_logger(__name__)

# Buffer applied to boundary/huc8 AOIs when the caller doesn't set one.
_DEFAULT_BUFFER_M = 2000.0

# Hard floor on the buffer, enforced even when the caller passes 0.
#
# The DEM extent decides the HAND solution, not just how much data is staged:
# flow accumulation, the derived stream network, the reach catchments and the
# REM datum are all re-derived inside whatever DEM it is handed. An AOI clipped
# tight to its catchments truncates the derived catchments at the edge and
# leaves inundation nowhere to spread, so the synthetic rating curves describe
# a narrower channel than the real one and the same discharge maps to a much
# higher stage over a much smaller extent. inundation-mapping avoids this with
# wbd_buffer=5000 on every HUC8; 1 km is the minimum that keeps a reach-id run
# in the same regime. Raise it for wide floodplains.
_MIN_BUFFER_M = 1000.0

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
    "usgs_gages": "usgs_gages.gpkg",
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
    Preprocess raw NLD levee lines for DEM burning:
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
    nwm_ids : sequence of int or str, optional
        NWM reach ids (matched on the ``ID`` column). The AOI becomes the
        dissolved footprint of their catchments. With ``buffer_m=0`` (the default
        for this mode) those reaches are the whole stream network; with a buffer
        the widened AOI is re-queried so neighbouring reaches inside it come too.
        Use ``getAllInputDataBatch`` to run several groups at once.
    out_dir : str or Path, optional
        Root output directory. Defaults to ``./fimbox_preprocess``. An AOI
        folder (``<out_dir>/<AOI_name>``) is created underneath, and all input
        data + branch processing land in its ``watershed-data/`` subfolder.
    epsg : int, optional
        Output CRS. Defaults to 5070 (CONUS Albers).
    dem_resolution : int, optional
        3DEP DEM resolution in metres. Default 10.
    buffer_m : float, optional
        Buffer distance in metres applied to the boundary before downloading all
        datasets. Defaults to 2000 for ``huc8``/``boundary`` runs and 0 for
        ``nwm_ids`` runs. Pass 0 to disable, or a value to override either.
    headwater_buffer_cells : int, optional
        Number of DEM cells used for the inner clip when deriving headwaters
    get_flowlines : bool, optional
        Download NWM/NHDPlus flowlines (and derive headwater points from them).
        Default True. Set False to skip.
    get_catchments : bool, optional
        Download NWM/NHDPlus catchments. Default True. Set False to skip.
    source : str, optional
        Flowline/catchment source: ``"nwmmedium"`` (default) the NWM ArcGIS
        FeatureServer, ``"nwmhigh"`` NHDPlus High Resolution via ``pynhd``, or
        ``"ngen"`` the NextGen community hydrofabric. Lakes always come from NWM.
        The pre-rename ``"medium"`` / ``"high"`` spellings still resolve.
    cat_ids : sequence of int or str, optional
        ngen catchment ids (``cat-123``, ``wb-123``, or bare ``123``). Implies
        ``source="ngen"``. The AOI becomes the dissolved footprint of the
        upstream catchments, then hydrography is selected over the buffered AOI.
    feature_ids : sequence of int or str, optional
        NWM / NHD feature ids (comids) resolved against the ngen hydrofabric via
        ``network.hf_id``, so an ngen run can start from the same reach ids as an
        NWM one. With ``source="ngen"``, ``nwm_ids`` is treated as this.
    subset_type : str, optional
        How far an id selection reaches, mirroring ngiab's ``--subset_type``.
        ``"nexus"`` (default) takes everything draining into the seed's
        downstream nexus; ``"catchment"`` stops at the seed itself.
    flowlines, catchments : str or Path, optional
        Bring-your-own flowlines / catchments (path to a vector file). When
        given, the file is normalised and saved instead of downloading that
        dataset. Supply ``stream_fields`` / ``catchment_fields`` if its column
        names differ from the canonical schema.
    stream_fields, catchment_fields : dict, optional
        Field maps (canonical -> your column) for the BYO data. Streams need
        ``ID``, ``order_``, ``levpa_id`` (``feature_id`` defaults to ``ID``);
        catchments need ``ID``. Example:
        ``stream_fields={"ID": "nhdplusid", "order_": "streamorde", "levpa_id": "levelpathi"}``.
    identifier : str, optional
        Filename prefix for the source-derived datasets (streams/catchments/
        lakes/headwaters and their level-path derivatives). Default ``"nwm"``,
        which preserves the legacy filenames. Pass a custom value (e.g.
        ``"3dhp"``) when your data is not NWM, so files are saved as
        ``{identifier}_subset_streams.gpkg`` etc. The whole pipeline auto-detects
        and reuses this prefix downstream.
    dem : str or Path, optional
        Bring-your-own DEM. When given it is reprojected, clipped to the buffer,
        and hole-filled (the same conditioning a downloaded 3DEP DEM gets) and
        saved as ``dem.tif`` instead of fetching from 3DEP.

    Either ``huc8`` or ``boundary`` must be provided.
    """

    def __init__(
        self,
        huc8: Optional[str] = None,
        boundary: Optional[Union[str, Path]] = None,
        boundary_layer: Optional[str] = None,
        nwm_ids: Optional[Sequence[Union[int, str]]] = None,
        out_dir: Optional[Union[str, Path]] = None,
        epsg: int = 5070,
        dem_resolution: int = 10,
        buffer_m: Optional[float] = None,
        headwater_buffer_cells: int = 8,
        get_flowlines: bool = True,
        get_catchments: bool = True,
        get_gages: bool = True,
        source: str = NWM_MEDIUM,
        cat_ids: Optional[Sequence[Union[int, str]]] = None,
        feature_ids: Optional[Sequence[Union[int, str]]] = None,
        subset_type: str = "nexus",
        flowlines: Optional[Union[str, Path]] = None,
        catchments: Optional[Union[str, Path]] = None,
        stream_fields: Optional[dict] = None,
        catchment_fields: Optional[dict] = None,
        identifier: str = DEFAULT_IDENTIFIER,
        dem: Optional[Union[str, Path]] = None,
    ):
        if (
            huc8 is None
            and boundary is None
            and not nwm_ids
            and not cat_ids
            and not feature_ids
        ):
            raise ValueError(
                "Provide one of huc8, boundary, nwm_ids, cat_ids, or feature_ids."
            )

        # cat_ids only exist in the ngen hydrofabric, so they select it outright.
        self.source = normalize_source(NGEN if cat_ids else source)
        self.subset_type = subset_type

        self.huc8 = huc8
        self.boundary_path = Path(boundary) if boundary else None
        self.boundary_layer = boundary_layer
        self.cat_ids = [str(i).strip() for i in cat_ids] if cat_ids else None
        self.feature_ids = (
            [str(i).strip() for i in feature_ids] if feature_ids else None
        )
        # Reach ids mean the same thing to both sources, so an ngen run accepts
        # them under either name and resolves them through network.hf_id.
        if self.source == NGEN and nwm_ids and not self.feature_ids:
            self.feature_ids = [str(i).strip() for i in nwm_ids]
            nwm_ids = None
        self.nwm_ids = [str(i).strip() for i in nwm_ids] if nwm_ids else None
        self.ngen_ids = self.cat_ids or self.feature_ids
        self.epsg = epsg
        self.dem_resolution = dem_resolution
        # Reach-id runs target an exact set of catchments, so they ask for no
        # buffer; boundary/huc8 runs keep the historical 2 km margin. Either way
        # _MIN_BUFFER_M is the floor — see its definition for why a zero-buffer
        # DEM silently corrupts the HAND solution.
        requested_buffer = (
            buffer_m
            if buffer_m is not None
            else (0.0 if (self.nwm_ids or self.ngen_ids) else _DEFAULT_BUFFER_M)
        )
        self.buffer_m = max(float(requested_buffer), _MIN_BUFFER_M)
        if self.buffer_m > requested_buffer:
            log.warning(
                "buffer_m=%s raised to the %s m floor — a tighter DEM truncates "
                "the derived catchments and the REM datum, which shrinks the FIM "
                "extent. Pass a larger buffer_m for wide floodplains.",
                requested_buffer,
                _MIN_BUFFER_M,
            )
        # Populated by _load_boundary in reach-id mode; reused instead of a
        # spatial re-download so the AOI holds only the requested reaches.
        self._reach_aoi = None
        self.headwater_buffer_cells = headwater_buffer_cells
        self.get_flowlines = get_flowlines
        self.get_catchments = get_catchments
        self.get_gages = get_gages
        # bring-your-own flowlines/catchments + their field maps
        self.byo_flowlines = Path(flowlines) if flowlines else None
        self.byo_catchments = Path(catchments) if catchments else None
        self.stream_fields = stream_fields
        self.catchment_fields = catchment_fields
        # bring-your-own DEM (reprojected, clipped, and hole-filled like a
        # downloaded one); when None the DEM is fetched from 3DEP.
        self.byo_dem = Path(dem) if dem else None

        # source-derived filenames carry an identifier prefix (default "nwm").
        self.identifier = identifier
        self._filenames = dict(_FILENAMES)
        self._filenames["nwm_streams"] = source_name("streams", identifier)
        self._filenames["nwm_catchments"] = source_name("catchments", identifier)
        self._filenames["nwm_lakes"] = source_name("lakes", identifier)
        self._filenames["nwm_headwaters"] = source_name("headwaters_points", identifier)

        if huc8:
            self.case_name = f"HUC{huc8}"
        elif self.nwm_ids:
            self.case_name = group_label(self.nwm_ids)
        elif self.ngen_ids:
            self.case_name = group_label(self.ngen_ids, prefix=NGEN)
        else:
            self.case_name = self.boundary_path.stem

        # AOI root holds the log, feature_id.csv, discharge-inputs/ and
        # fim-outputs/. All input data + branch processing live one level
        # deeper, in watershed-data/ — which is the directory every downstream
        # stage (branch derivation/processing, calibration, FIM) takes as its
        # aoi_dir. case_dir is kept as an alias for that watershed-data folder
        # so the rest of this class is unchanged.
        root = Path(out_dir) if out_dir else default_output_dir()
        self.aoi_dir = root / self.case_name
        self.watershed_dir = self.aoi_dir / WATERSHED_DIR_NAME
        self.case_dir = self.watershed_dir
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

        self.logger.info(
            f"Case: {self.case_name}  |  AOI: {self.aoi_dir}  |  "
            f"watershed-data: {self.case_dir}"
        )

    # helpers
    def _out(self, key: str) -> Path:
        # self._filenames overrides the source-derived names with the identifier
        # prefix; everything else falls back to the module-level defaults.
        return self.case_dir / self._filenames[key]

    def _setup_logger(self):
        # All fimbox modules log under `fimbox.*` via getLogger(__name__);
        # attaching handlers to the `fimbox` root makes every nested log
        # call land in this AOI's processing.log as well as stdout.
        attach_case_log(self.case_dir)
        self.logger = get_logger(f"fimbox.preprocess.{self.case_name}")

    @staticmethod
    def _drop_fid(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        # pyogrio reserves 'fid' as an internal OGR field — drop it before saving
        reserved = [c for c in gdf.columns if c.lower() == "fid"]
        return gdf.drop(columns=reserved) if reserved else gdf

    def _load_boundary(self) -> gpd.GeoDataFrame:
        if self.ngen_ids:
            return self._drop_fid(self._ngen_boundary())

        if self.nwm_ids:
            self.logger.info(f"Resolving {len(self.nwm_ids)} NWM reach id(s)...")
            self._reach_aoi = resolve_reach_group(
                self.nwm_ids, epsg=self.epsg, strict=False
            )
            if self._reach_aoi.missing:
                self.logger.warning(
                    f"Unresolved reach id(s): {self._reach_aoi.missing}"
                )
            self.logger.info(
                f"AOI from {self._reach_aoi.reach_count} reach(es) / "
                f"{len(self._reach_aoi.catchments)} catchment(s)"
            )
            return self._drop_fid(self._reach_aoi.boundary)

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

    def _ngen_boundary(self) -> gpd.GeoDataFrame:
        """AOI footprint for an ngen id run: the dissolved upstream catchments.

        Only the boundary is taken here. Hydrography is selected later over the
        *buffered* AOI, so the neighbours the buffer reaches come too — the same
        thing a buffered ``nwm_ids`` run does, and what HAND needs to be right.
        """
        from ..ngen import NgenHydrofabric

        kind = "catchment" if self.cat_ids else "feature"
        self.logger.info(f"Resolving {len(self.ngen_ids)} ngen {kind} id(s)...")

        with NgenHydrofabric(epsg=self.epsg) as hf:
            if self.cat_ids:
                selection = hf.select_by_cat_ids(
                    self.cat_ids, include_outlet=self.subset_type == "nexus"
                )
            else:
                selection = hf.select_by_feature_ids(
                    self.feature_ids, include_outlet=self.subset_type == "nexus"
                )
            if selection.is_empty:
                raise ValueError(
                    f"No ngen catchments found for {kind} ids {self.ngen_ids}."
                )
            if selection.missing:
                self.logger.warning(
                    f"Unresolved ngen {kind} id(s): {selection.missing}"
                )

            catchments = hf.fetch_catchments(selection)

        if catchments.empty:
            raise ValueError(
                f"ngen catchments resolved but returned no geometry: {kind} ids."
            )

        self.logger.info(
            f"AOI from {len(selection.wb_ids)} reach(es) / {len(catchments)} catchment(s)"
        )
        return gpd.GeoDataFrame(
            {"catchment_count": [len(catchments)]},
            geometry=[unary_union(catchments.geometry)],
            crs=catchments.crs,
        ).to_crs("EPSG:4326")

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
        self.logger.info("Study boundary (clipped) --> wbd8_clp.gpkg")

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
        src = f"BYO {self.byo_dem.name}" if self.byo_dem else "3DEP"
        self.logger.info(f"--- DEM ({src}) ---")
        try:
            # dem_file set -> reproject/clip/hole-fill the user's DEM; else fetch 3DEP.
            DEMProcessor(
                boundary=self.buffer_gdf,
                output_dir=str(self.case_dir),
                out_name=_FILENAMES["dem"],
                resolution=self.dem_resolution,
                epsg=self.epsg,
                dem_file=str(self.byo_dem) if self.byo_dem else None,
            )
            self.logger.info(f"DEM --> {_FILENAMES['dem']}")
        except Exception as exc:
            self.logger.error(f"DEM failed: {exc}", exc_info=True)

    def run_nhd(self):
        # Lakes always download; flowlines/catchments are user-toggleable and
        # may be supplied directly (BYO) instead of downloaded.
        want_flowlines = self.get_flowlines or self.byo_flowlines is not None
        want_catchments = self.get_catchments or self.byo_catchments is not None

        expected_keys = ["nwm_lakes"]
        if want_flowlines:
            expected_keys.append("nwm_streams")
        if want_catchments:
            expected_keys.append("nwm_catchments")

        if all(self._out(k).exists() for k in expected_keys):
            self.logger.info("SKIP (exists): nwm_subset_streams/catchments/lakes")
            return

        # Reach-id runs come in two flavours. With no buffer the AOI is exactly
        # the reaches asked for, so the layers held from id resolution are written
        # straight out. With a buffer the point is to reach past those catchments,
        # so the normal spatial download runs over the buffered boundary and picks
        # up the neighbours inside it — the upstream area HAND needs to be right.
        reach_exact = self._reach_aoi is not None and self.buffer_m <= 0
        if reach_exact:
            self._ingest_resolved_reaches()
        elif self._reach_aoi is not None:
            self.logger.info(
                f"Reach-id run with buffer_m={self.buffer_m:g} --> downloading "
                "hydrography over the buffered AOI, not just the requested reaches"
            )

        # 1) Bring-your-own flowlines / catchments: normalise + save (no download).
        self._ingest_byo()

        # 2) Download whatever was not supplied directly.
        dl_flowlines = (
            self.get_flowlines and self.byo_flowlines is None and not reach_exact
        )
        dl_catchments = (
            self.get_catchments and self.byo_catchments is None and not reach_exact
        )

        # With reach_exact both layers came from id resolution, so there is
        # nothing for these toggles to skip.
        if not reach_exact:
            if not self.get_flowlines and self.byo_flowlines is None:
                self.logger.info(
                    "get_flowlines=False --> skipping flowlines/headwaters"
                )
            if not self.get_catchments and self.byo_catchments is None:
                self.logger.info("get_catchments=False --> skipping catchments")

        self.logger.info(
            f"--- {SOURCE_LABELS[self.source]} Flowlines / Catchments + NWM Lakes ---"
        )
        try:
            results = getNHDPlusData(
                boundary=self.buffer_gdf,
                out_dir=str(self.case_dir),
                epsg=self.epsg,
                download_flowlines=dl_flowlines,
                download_catchments=dl_catchments,
                download_lakes=True,
                source=self.source,
                identifier=self.identifier,
                ngen_kwargs={"include_outlet": self.subset_type == "nexus"},
            )
            if results.get("flowlines") is not None and not results["flowlines"].empty:
                self.logger.info(f"streams --> {self._filenames['nwm_streams']}")
                self._run_headwaters(results["flowlines"])
            if (
                results.get("catchments") is not None
                and not results["catchments"].empty
            ):
                self.logger.info(f"catchments --> {self._filenames['nwm_catchments']}")
            if results.get("lakes") is not None and not results["lakes"].empty:
                self.logger.info(f"lakes --> {self._filenames['nwm_lakes']}")
        except Exception as exc:
            self.logger.error(f"NHD failed: {exc}", exc_info=True)

    def _ingest_resolved_reaches(self):
        """Save the reach-id mode streams/catchments under the standard names."""
        if self._reach_aoi is None:
            return

        # Written with exactly the schema the spatial download produces, so every
        # later stage sees the same columns whichever way the AOI was defined.
        # levpa_id and feature_id are deliberately not added here — branch
        # derivation synthesises the level paths and the crosswalk assigns
        # feature_id, same as for a boundary or HUC run.
        fl = self._drop_fid(self._reach_aoi.reaches.to_crs(self.epsg))
        fl.to_file(self._out("nwm_streams"), driver="GPKG", index=False)
        self.logger.info(f"streams ({len(fl)}) --> {self._filenames['nwm_streams']}")

        cat = self._drop_fid(self._reach_aoi.catchments.to_crs(self.epsg))
        cat.to_file(self._out("nwm_catchments"), driver="GPKG", index=False)
        self.logger.info(
            f"catchments ({len(cat)}) --> {self._filenames['nwm_catchments']}"
        )

        self._run_headwaters(fl)

    def _ingest_byo(self):
        """Normalise bring-your-own flowlines / catchments to the canonical
        schema and save them under the standard filenames the pipeline reads."""
        if self.byo_flowlines is not None:
            self.logger.info(f"--- BYO flowlines: {self.byo_flowlines.name} ---")
            try:
                fl = normalize_flowlines(
                    self.byo_flowlines, field_map=self.stream_fields, epsg=self.epsg
                )
                fl = self._drop_fid(fl)
                fl.to_file(self._out("nwm_streams"), driver="GPKG", index=False)
                self.logger.info(
                    f"BYO streams ({len(fl)}) --> {self._filenames['nwm_streams']}"
                )
                self._run_headwaters(fl)
            except Exception as exc:
                self.logger.error(f"BYO flowlines failed: {exc}", exc_info=True)

        if self.byo_catchments is not None:
            self.logger.info(f"--- BYO catchments: {self.byo_catchments.name} ---")
            try:
                cat = normalize_catchments(
                    self.byo_catchments, field_map=self.catchment_fields, epsg=self.epsg
                )
                cat = self._drop_fid(cat)
                cat.to_file(self._out("nwm_catchments"), driver="GPKG", index=False)
                self.logger.info(
                    f"BYO catchments ({len(cat)}) --> {self._filenames['nwm_catchments']}"
                )
            except Exception as exc:
                self.logger.error(f"BYO catchments failed: {exc}", exc_info=True)

    def _run_headwaters(self, flowlines_gdf: gpd.GeoDataFrame):
        """Derive headwater points from flowlines, clipped to inner buffer."""
        try:
            # Inset the buffer by headwater_buffer_cells pixels to keep headwaters
            # off the DEM edge, but never cut inside the AOI itself — with no
            # buffer there is no margin to give up, so skip the inset entirely.
            inset = min(
                self.headwater_buffer_cells * self.dem_resolution, self.buffer_m
            )
            projected = self.buffer_gdf.to_crs(epsg=self.epsg)
            inner_gdf = projected.copy()
            if inset > 0:
                inner_gdf["geometry"] = projected.geometry.buffer(-inset)
            inner_gdf = inner_gdf[~inner_gdf.is_empty]

            fl = flowlines_gdf.to_crs(epsg=self.epsg)
            if not inner_gdf.empty and inset > 0:
                fl = gpd.clip(fl, inner_gdf)

            hw = find_headwater_points(fl)
            if not hw.empty:
                hw.to_file(self._out("nwm_headwaters"), driver="GPKG", index=False)
                self.logger.info(
                    f"Headwaters ({len(hw)} points) --> {self._filenames['nwm_headwaters']}"
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
                    self.logger.info(
                        "NLD: no levee lines in this area — skipping levee burn."
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
                self.logger.info("NLD: no levee protected areas in this area.")

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

    def run_gages(self):
        # USGS gages feed the optional NWM-vs-observations evaluation. An empty
        # result (no gages in the AOI) is normal and writes nothing.
        if not self.get_gages or self._skip("usgs_gages"):
            return
        self.logger.info("--- USGS Gages ---")
        try:
            gdf = DownloadUSGSGages().download(
                boundary=self.buffer_gdf,
                aoi_id=self.case_name,
                out_dir=str(self.case_dir),
                out_name=_FILENAMES["usgs_gages"],
            )
            if gdf.empty:
                self.logger.info("USGS gages: none in AOI — nothing staged")
            else:
                self.logger.info(f"USGS gages --> {_FILENAMES['usgs_gages']}")
        except Exception as exc:
            self.logger.error(f"USGS gages failed: {exc}", exc_info=True)

    # full pipeline
    def run(self):
        self.logger.info(f"=== PreprocessAll: {self.case_name} ===")
        self.logger.info(f"Output: {self.case_dir}")
        self.run_dem()
        self.run_nhd()
        self.run_nld()
        self.run_osm()
        self.run_gages()
        self.logger.info("=== ALL STEPS COMPLETE ===")
        self._log_summary()

    def _log_summary(self):
        # Data files live in watershed-data (self.case_dir); the combined log
        # sits at the AOI root, so it is no longer alongside them.
        files = sorted(
            f
            for f in self.case_dir.iterdir()
            if f.suffix in (".gpkg", ".tif") and f.is_file()
        )
        self.logger.info(f"--- Summary: {self.case_name} ({len(files)} files) ---")
        for f in files:
            size_kb = f.stat().st_size // 1024
            self.logger.info(f"  {f.name:<45}  {size_kb:>6} KB")


def getAllInputDataBatch(
    hucs: Optional[Sequence] = None,
    nwm_ids: Optional[Sequence] = None,
    separate: bool = False,
    together: bool = False,
    run: bool = True,
    continue_on_error: bool = True,
    **kwargs,
) -> list[getAllInputData]:
    """Prepare one or many AOIs from HUC ids or NWM reach ids.

    Grouping is explicit — geography is never used to guess::

        nwm_ids=[101, 102]              one AOI holding both
        nwm_ids=[[101], [201]]          one AOI each
        nwm_ids=[101, 102], separate=True   one AOI per reach

        hucs=["0806", "0807"]           one AOI each (FIM convention)
        hucs=[["0806", "0807"]]         one combined AOI
        hucs=[...], together=True       one combined AOI

    Parameters
    ----------
    separate : split every group into single-id AOIs
    together : merge all groups into one AOI
    run : call ``.run()`` on each AOI; False builds them without downloading
    continue_on_error : log and carry on when one AOI fails, instead of raising

    Returns the ``getAllInputData`` instance for each AOI, in group order.
    """
    if not hucs and not nwm_ids:
        raise ValueError("Provide hucs or nwm_ids.")
    if separate and together:
        raise ValueError("separate and together are mutually exclusive.")

    jobs: list[dict] = []
    for grp in normalize_huc_groups(hucs, together=together) if hucs else []:
        if len(grp) > 1:
            # Several HUCs in one AOI: dissolve them into a boundary file first,
            # since getAllInputData's huc8= takes a single id.
            jobs.append({"_hucs": grp})
        else:
            jobs.append({"huc8": grp[0]})

    reach_groups = normalize_groups(nwm_ids, separate=separate) if nwm_ids else []
    if together and len(reach_groups) > 1:
        reach_groups = [[i for grp in reach_groups for i in grp]]
    jobs.extend({"nwm_ids": grp} for grp in reach_groups)

    log.info(f"Batch: {len(jobs)} AOI(s)")
    built: list[getAllInputData] = []
    for i, job in enumerate(jobs, 1):
        label = job.get("huc8") or job.get("nwm_ids") or job.get("_hucs")
        try:
            if "_hucs" in job:
                job = {"boundary": _stage_huc_group(job.pop("_hucs"), kwargs)}
            aoi = getAllInputData(**job, **kwargs)
            log.info(f"[{i}/{len(jobs)}] {aoi.case_name}")
            if run:
                aoi.run()
            built.append(aoi)
        except Exception as exc:
            if not continue_on_error:
                raise
            log.error(f"[{i}/{len(jobs)}] AOI {label} failed: {exc}", exc_info=True)
    return built


def _stage_huc_group(hucs: Sequence[str], kwargs: dict) -> Path:
    """Write a dissolved multi-HUC boundary and return its path."""
    out_root = Path(kwargs.get("out_dir") or default_output_dir())
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / f"{huc_label(hucs)}_boundary.gpkg"
    hucs_to_boundary(hucs).to_file(path, driver="GPKG", index=False)
    log.info(f"Combined {len(hucs)} HUCs --> {path.name}")
    return path


# CLI
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Download and preprocess all FIM input data for a HUC8 or boundary."
    )
    aoi = parser.add_mutually_exclusive_group(required=True)
    aoi.add_argument("--huc8", help="HUC8 ID (e.g. 08060202)")
    aoi.add_argument("--boundary", help="Path to boundary file (gpkg/shp/etc.)")
    aoi.add_argument(
        "--cat-ids", help="comma-separated ngen catchment ids; implies --source ngen"
    )
    aoi.add_argument(
        "--feature-ids", help="comma-separated NWM feature ids (ngen source)"
    )
    parser.add_argument("--boundary-layer", default=None)
    parser.add_argument("--out-dir", default="fimbox_preprocess")
    parser.add_argument("--epsg", type=int, default=5070)
    parser.add_argument("--dem-resolution", type=int, default=10)
    parser.add_argument("--buffer-m", type=float, default=2000.0)
    parser.add_argument("--headwater-buffer-cells", type=int, default=8)
    parser.add_argument(
        "--no-flowlines",
        action="store_true",
        help="Skip NWM flowline + headwater download (bring your own).",
    )
    parser.add_argument(
        "--no-catchments",
        action="store_true",
        help="Skip NWM catchment download (bring your own).",
    )
    parser.add_argument(
        "--source",
        default=NWM_MEDIUM,
        choices=SOURCES,
        help="Flowline/catchment source: 'nwmmedium' (NWM, default), 'nwmhigh' "
        "(NHDPlus HR via pynhd), or 'ngen' (NextGen hydrofabric).",
    )
    parser.add_argument(
        "--subset-type",
        choices=["nexus", "catchment"],
        default="nexus",
        help="How far an ngen id selection reaches: 'nexus' (default) everything "
        "draining into the seed's outlet, or 'catchment' stopping at the seed.",
    )
    parser.add_argument(
        "--identifier",
        default=DEFAULT_IDENTIFIER,
        help="Filename prefix for source-derived data (default 'nwm').",
    )
    parser.add_argument(
        "--dem",
        default=None,
        help="Bring-your-own DEM path (reprojected/clipped/hole-filled); "
        "omit to fetch 3DEP.",
    )
    args = parser.parse_args()

    def _split(value):
        return [v.strip() for v in value.split(",") if v.strip()] if value else None

    pp = getAllInputData(
        huc8=args.huc8,
        boundary=args.boundary,
        boundary_layer=args.boundary_layer,
        cat_ids=_split(args.cat_ids),
        feature_ids=_split(args.feature_ids),
        out_dir=args.out_dir,
        epsg=args.epsg,
        dem_resolution=args.dem_resolution,
        buffer_m=args.buffer_m,
        headwater_buffer_cells=args.headwater_buffer_cells,
        get_flowlines=not args.no_flowlines,
        get_catchments=not args.no_catchments,
        source=args.source,
        subset_type=args.subset_type,
        identifier=args.identifier,
        dem=args.dem,
    )
    pp.run()
