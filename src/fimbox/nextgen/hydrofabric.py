"""
Resolve an area of interest (AOI) to the NextGen v2.2 hydrofabric.

Given an AOI (shapefile / GeoPackage / any vector fiona can read), this module
finds which hydrofabric VPU(s) the AOI falls in, reads the catchment
(``divides``) polygons that intersect the AOI directly from the public
per-VPU GeoPackage on the CIROH community NextGen DataStream bucket, and
crosswalks them to the NextGen network ids used by the ngen/t-route outputs.

Each NextGen divide (``divide_id`` = ``cat-<n>``) drains to exactly one
flowpath (``id`` = ``wb-<n>``); the integer ``<n>`` is the ``feature_id`` used
in the t-route discharge outputs. The ``network`` layer additionally carries
``hf_id`` — the NOAA reference-fabric / NWM COMID — kept as a crosswalk to the
NWM feature ids the rest of fimbox uses.

Outputs (written under ``<AOI>/hydrofabric/`` when ``save=True``):
    aoi_catchments.gpkg   -- intersecting divides (catchment polygons)
    aoi_flowpaths.gpkg    -- the flowpaths draining those catchments
    network_crosswalk.csv -- divide_id, wb_id, feature_id, hf_id (NWM), vpu
    feature_id.csv        -- unique feature_ids (drop-in for the streamflow step)
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from . import _common as C

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

# GDAL/pyogrio env needed to read the public gpkg anonymously over /vsis3.
_VSIS3_ENV = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "AWS_S3_ENDPOINT": "s3.amazonaws.com",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".gpkg",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "VSI_CACHE": "TRUE",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


@contextmanager
def _vsis3_env():
    """Temporarily set the GDAL env vars required for anonymous /vsis3 reads."""
    old = {k: os.environ.get(k) for k in _VSIS3_ENV}
    os.environ.update(_VSIS3_ENV)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@dataclass
class AOIHydrofabric:
    """Result of an AOI -> hydrofabric resolution.

    Attributes
    ----------
    catchments : GeoDataFrame
        Intersecting ``divides`` (catchment polygons), EPSG:5070.
    flowpaths : GeoDataFrame
        The flowpaths (``wb-*``) draining those catchments, EPSG:5070.
    crosswalk : DataFrame
        One row per catchment: divide_id, wb_id, feature_id, hf_id, vpu.
    vpus : list[str]
        VPU(s) the AOI intersects (usually one).
    """

    catchments: gpd.GeoDataFrame
    flowpaths: gpd.GeoDataFrame
    crosswalk: pd.DataFrame
    vpus: list[str] = field(default_factory=list)

    @property
    def feature_ids(self) -> list[int]:
        """Unique t-route ``feature_id``s (integers of the ``wb-*`` ids)."""
        return sorted(
            {int(f) for f in self.crosswalk["feature_id"].dropna().astype("int64")}
        )

    @property
    def catchment_ids(self) -> list[str]:
        """``cat-*`` divide ids."""
        return self.crosswalk["divide_id"].dropna().astype(str).tolist()

    @property
    def network_ids(self) -> list[str]:
        """``wb-*`` NextGen flowpath (network) ids."""
        return self.crosswalk["wb_id"].dropna().astype(str).tolist()

    @property
    def nwm_feature_ids(self) -> list[int]:
        """NWM/NHD reference COMIDs (``hf_id``) crosswalked to the catchments."""
        return sorted(
            {int(f) for f in self.crosswalk["hf_id"].dropna().astype("int64")}
        )


class NextGenHydrofabric:
    """AOI -> NextGen v2.2 catchments + network-id crosswalk.

    Parameters
    ----------
    aoi : str or Path or GeoDataFrame
        Area of interest: a path to a shapefile / GeoPackage / GeoJSON, or an
        already-loaded GeoDataFrame.
    aoi_layer : str, optional
        Layer name when ``aoi`` is a multi-layer GeoPackage.
    predicate : str, optional
        Spatial test used to select catchments. ``"intersects"`` (default)
        keeps every catchment that touches the AOI; ``"within"`` keeps only
        catchments fully inside the AOI.
    """

    def __init__(
        self,
        aoi: Union[PathLike, gpd.GeoDataFrame],
        *,
        aoi_layer: Optional[str] = None,
        predicate: str = "intersects",
        nwm_crosswalk: bool = False,
    ):
        if predicate not in ("intersects", "within"):
            raise ValueError("predicate must be 'intersects' or 'within'")
        self.predicate = predicate
        # opt-in: reading the non-spatial `network` table scans the whole layer
        self.nwm_crosswalk = nwm_crosswalk
        self.aoi = self._load_aoi(aoi, aoi_layer).to_crs(epsg=C.HF_EPSG)
        self._aoi_geom = self.aoi.geometry.union_all()

    @staticmethod
    def _load_aoi(
        aoi: Union[PathLike, gpd.GeoDataFrame], layer: Optional[str]
    ) -> gpd.GeoDataFrame:
        if isinstance(aoi, gpd.GeoDataFrame):
            gdf = aoi.copy()
        else:
            gdf = gpd.read_file(aoi, layer=layer) if layer else gpd.read_file(aoi)
        if gdf.empty:
            raise ValueError("AOI is empty.")
        if gdf.crs is None:
            raise ValueError("AOI has no CRS; set one before resolving.")
        return gdf

    def candidate_vpus(self) -> list[str]:
        """VPU(s) whose bounding box overlaps the AOI (bbox pre-filter)."""
        aoi_box = box(*self.aoi.total_bounds)
        hits = [
            vpu
            for vpu, bbox in C.vpu_bbox_index().items()
            if box(*bbox).intersects(aoi_box)
        ]
        if not hits:
            raise ValueError(
                "AOI does not overlap any CONUS hydrofabric VPU bounding box. "
                "Check the AOI CRS / extent (must be within CONUS)."
            )
        return hits

    def _read_divides(self, vpu: str) -> gpd.GeoDataFrame:
        """Read ``divides`` in the AOI bbox from a VPU gpkg over /vsis3, then
        apply the precise spatial predicate against the AOI geometry."""
        uri = C.vpu_gpkg_uri(vpu)
        bounds = tuple(self.aoi.total_bounds)
        with _vsis3_env():
            divides = gpd.read_file(uri, layer="divides", bbox=bounds)
        if divides.empty:
            return divides
        if divides.crs is None:
            divides.set_crs(epsg=C.HF_EPSG, inplace=True)
        else:
            divides = divides.to_crs(epsg=C.HF_EPSG)
        if self.predicate == "within":
            mask = divides.within(self._aoi_geom)
        else:
            mask = divides.intersects(self._aoi_geom)
        hit = divides[mask].copy()
        hit["vpuid"] = hit.get("vpuid", vpu)
        hit["vpu"] = vpu
        return hit

    def _read_flowpaths(self, vpu: str, wb_ids: list[str]) -> gpd.GeoDataFrame:
        """Read the flowpaths draining the selected catchments from a VPU gpkg.

        Uses the AOI bbox spatial filter (fast, index-backed) then keeps only
        the ``wb`` ids we selected — an attribute ``WHERE id IN (...)`` over
        /vsis3 would scan the whole layer instead."""
        if not wb_ids:
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{C.HF_EPSG}")
        uri = C.vpu_gpkg_uri(vpu)
        bounds = tuple(self.aoi.total_bounds)
        with _vsis3_env():
            fp = gpd.read_file(uri, layer="flowpaths", bbox=bounds)
        if fp.empty:
            return fp
        if fp.crs is not None:
            fp = fp.to_crs(epsg=C.HF_EPSG)
        fp = fp[fp["id"].astype(str).isin(set(wb_ids))].copy()
        return fp

    def _read_network_crosswalk(self, vpu: str, wb_ids: list[str]) -> pd.DataFrame:
        """Read the NWM ``hf_id`` crosswalk for the selected ``wb`` ids from the
        ``network`` layer (non-spatial). One hf_id per flowpath is kept."""
        if not wb_ids:
            return pd.DataFrame(columns=["wb_id", "hf_id"])
        uri = C.vpu_gpkg_uri(vpu)
        where = "id IN ({})".format(",".join(f"'{i}'" for i in wb_ids))
        try:
            import pyogrio

            with _vsis3_env():
                net = pyogrio.read_dataframe(
                    uri,
                    layer="network",
                    where=where,
                    read_geometry=False,
                    columns=["id", "hf_id"],
                )
        except Exception as exc:
            log.warning("network crosswalk read failed for %s: %s", vpu, exc)
            return pd.DataFrame(columns=["wb_id", "hf_id"])
        net = net.rename(columns={"id": "wb_id"})
        net = net.dropna(subset=["wb_id"]).drop_duplicates(subset=["wb_id"])
        return net[["wb_id", "hf_id"]]

    def resolve(self) -> AOIHydrofabric:
        """Run the full AOI -> catchments + crosswalk resolution."""
        cats, fps, xwalks, vpus = [], [], [], []
        for vpu in self.candidate_vpus():
            hit = self._read_divides(vpu)
            if hit.empty:
                log.info("VPU %s: no catchments intersect the AOI", vpu)
                continue
            vpus.append(vpu)
            wb_ids = hit["id"].dropna().astype(str).tolist()
            log.info("VPU %s: %d catchments intersect the AOI", vpu, len(hit))

            xwalk = pd.DataFrame(
                {
                    "divide_id": hit["divide_id"].astype(str).values,
                    "wb_id": hit["id"].astype(str).values,
                    "vpu": vpu,
                }
            )
            xwalk["feature_id"] = xwalk["wb_id"].map(C.feature_id_from_wb)
            if self.nwm_crosswalk:
                nwm = self._read_network_crosswalk(vpu, wb_ids)
                xwalk = xwalk.merge(nwm, on="wb_id", how="left")
            else:
                xwalk["hf_id"] = pd.NA

            cats.append(hit)
            fps.append(self._read_flowpaths(vpu, wb_ids))
            xwalks.append(xwalk)

        if not cats:
            raise ValueError(
                "No hydrofabric catchments intersect the AOI in any candidate VPU."
            )

        catchments = gpd.GeoDataFrame(
            pd.concat(cats, ignore_index=True), crs=f"EPSG:{C.HF_EPSG}"
        )
        flowpaths = (
            gpd.GeoDataFrame(
                pd.concat([f for f in fps if not f.empty], ignore_index=True),
                crs=f"EPSG:{C.HF_EPSG}",
            )
            if any(not f.empty for f in fps)
            else gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{C.HF_EPSG}")
        )
        crosswalk = pd.concat(xwalks, ignore_index=True).drop_duplicates(
            subset=["divide_id"]
        )
        log.info(
            "AOI hydrofabric: %d catchments across VPU(s) %s (%d unique feature_ids)",
            len(catchments),
            ", ".join(vpus),
            crosswalk["feature_id"].nunique(),
        )
        return AOIHydrofabric(catchments, flowpaths, crosswalk, vpus)

    def resolve_and_save(self, aoi_dir: PathLike) -> AOIHydrofabric:
        """Resolve and write the catchment/flowpath/crosswalk outputs under
        ``<AOI>/hydrofabric/`` plus a ``feature_id.csv`` at the AOI root."""
        C.attach_log(aoi_dir)
        result = self.resolve()
        out = C.hydrofabric_dir(aoi_dir)

        cat_path = out / "aoi_catchments.gpkg"
        result.catchments.to_file(cat_path, driver="GPKG")
        log.info("catchments (%d) --> %s", len(result.catchments), cat_path.name)

        if not result.flowpaths.empty:
            fp_path = out / "aoi_flowpaths.gpkg"
            result.flowpaths.to_file(fp_path, driver="GPKG")
            log.info("flowpaths (%d) --> %s", len(result.flowpaths), fp_path.name)

        xwalk_path = out / "network_crosswalk.csv"
        result.crosswalk.to_csv(xwalk_path, index=False)
        log.info("network crosswalk --> %s", xwalk_path.name)

        fid_path = C.resolve_aoi(aoi_dir) / "feature_id.csv"
        pd.DataFrame({"feature_id": result.feature_ids}).to_csv(fid_path, index=False)
        log.info("feature ids (%d) --> %s", len(result.feature_ids), fid_path.name)
        return result


def build_vpu_index(out_path: Optional[PathLike] = None) -> dict[str, list[float]]:
    """Regenerate the cached per-VPU bounding-box index from the public bucket.

    Reads each ``nextgen_VPU_<id>.gpkg`` ``divides`` layer's ``total_bounds``
    (metadata only) over /vsis3 and writes them to ``data/vpu_bbox.json``. Run
    this only if the hydrofabric VPU set or extents change upstream.
    """
    import json

    import pyogrio

    fs = C.s3()
    vpus = sorted(
        p.split("/")[-1]
        for p in fs.ls(C.HF_GEOPACKAGES_PREFIX)
        if "VPU_" in p.split("/")[-1]
    )
    bboxes: dict[str, list[float]] = {}
    with _vsis3_env():
        for vpu in vpus:
            info = pyogrio.read_info(C.vpu_gpkg_uri(vpu), layer="divides")
            bboxes[vpu] = [float(x) for x in info["total_bounds"]]
            log.info("VPU %s bounds %s", vpu, bboxes[vpu])
    payload = {
        "_comment": "EPSG:5070 [minx, miny, maxx, maxy] per VPU; regenerate via "
        "fimbox.nextgen.hydrofabric.build_vpu_index().",
        "crs": "EPSG:5070",
        "hydrofabric_version": C.HF_VERSION,
        "bboxes": bboxes,
    }
    dest = Path(out_path) if out_path else C._VPU_INDEX_PATH
    dest.write_text(json.dumps(payload, indent=2))
    C.vpu_bbox_index.cache_clear()
    return bboxes
