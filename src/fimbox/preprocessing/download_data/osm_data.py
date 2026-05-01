"""
Author: Supath Dhital
Date updated: April 2026

Download major road segments from OpenStreetMap (OSM) within a user-provided boundary.

- Queries Overpass API for highway types: motorway, trunk, primary, secondary, tertiary
- Explicitly EXCLUDES bridges (ways with bridge=*) to avoid unrealistic flood depth calcs
- Boundary input can be: shapefile/gpkg/geojson path, GeoDataFrame/GeoSeries, shapely geometry, or bbox
- Output is saved to GeoPackage in EPSG:5070
- User can pass out_dir, out_name (or ourfile), out_layer (or ourlayer); defaults used otherwise
- Large areas are automatically split into tiles fetched in parallel via ThreadPoolExecutor

AND

Download bridge features from OpenStreetMap (OSM) within a user-provided boundary.
- Uses OSMnx features_from_polygon with {"bridge": True}
- Boundary input can be: shapefile/gpkg/geojson path, GeoDataFrame/GeoSeries, shapely geometry, or bbox
- Converts non-LineString geometries to LineStrings when possible (Polygon -> exterior; Point -> skipped by default)
- Removes abandoned/proposed/demolished bridges based on bridge_type (highway-* / railway-*)
- Dissolves touching bridge segments (buffer + graph connectivity) to form continuous bridge lines
- Output is saved to GeoPackage in EPSG:5070
- User can pass out_dir, out_name (or ourfile), out_layer (or ourlayer); defaults used otherwise
"""

import math
import random
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union, Tuple, Sequence, List, Any

import geopandas as gpd
import osmnx as ox
import pandas as pd
import requests
from networkx import Graph, connected_components
from shapely.geometry import LineString, Polygon, MultiPolygon, box
from shapely.ops import unary_union
from tqdm import tqdm


# shared boundary with IO helpers (used by both roads + bridges)
@dataclass
class _OSMBoundaryIO:
    out_sr: int = 5070

    def _read_boundary_file(self, path: Union[str, Path], layer: Optional[str] = None) -> gpd.GeoDataFrame:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)

        if path.suffix.lower() == ".gpkg":
            if layer is None:
                layers = gpd.list_layers(path)
                if layers is None or len(layers) == 0:
                    raise ValueError(f"No layers found in {path}")
                layer = layers.iloc[0]["name"]
            gdf = gpd.read_file(path, layer=layer)
        else:
            gdf = gpd.read_file(path)

        if gdf.empty:
            raise ValueError(f"Boundary file is empty: {path}")
        if gdf.crs is None:
            raise ValueError(f"Boundary CRS is missing: {path}")
        return gdf

    def _boundary_to_geom4326(
        self,
        boundary: Union[
            gpd.GeoDataFrame,
            gpd.GeoSeries,
            Polygon,
            MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float],
            str,
            Path,
        ],
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
    ) -> Union[Polygon, MultiPolygon]:
        # File path
        if isinstance(boundary, (str, Path)):
            gdf = self._read_boundary_file(boundary, layer=boundary_layer)
            geom = unary_union(gdf.to_crs("EPSG:4326").geometry)
            return self._ensure_poly(geom)

        # GeoPandas
        if isinstance(boundary, (gpd.GeoDataFrame, gpd.GeoSeries)):
            if boundary.crs is None:
                raise ValueError("Boundary GeoDataFrame/GeoSeries must have a CRS.")
            geom = unary_union(boundary.to_crs("EPSG:4326").geometry)
            return self._ensure_poly(geom)

        # bbox
        if isinstance(boundary, (tuple, list)) and len(boundary) == 4 and all(
            isinstance(x, (int, float)) for x in boundary
        ):
            geom = box(*boundary)
            if boundary_crs is not None:
                geom = gpd.GeoSeries([geom], crs=boundary_crs).to_crs("EPSG:4326").iloc[0]
            return self._ensure_poly(geom)

        # shapely geometry
        if isinstance(boundary, (Polygon, MultiPolygon)):
            geom = boundary
            if boundary_crs is not None:
                geom = gpd.GeoSeries([geom], crs=boundary_crs).to_crs("EPSG:4326").iloc[0]
            return self._ensure_poly(geom)

        raise TypeError(f"Unsupported boundary type: {type(boundary)}")

    @staticmethod
    def _ensure_poly(geom) -> Union[Polygon, MultiPolygon]:
        if geom.is_empty:
            raise ValueError("Boundary geometry is empty after dissolve/reproject.")
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            raise TypeError(f"Boundary must dissolve to Polygon/MultiPolygon, got {geom.geom_type}")
        return geom

    def _write_gpkg(
        self,
        gdf: gpd.GeoDataFrame,
        out_dir: Union[str, Path],
        out_name: str,
        out_layer: str,
    ) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / out_name
        gdf.to_file(out_path, layer=out_layer, driver="GPKG")
        return out_path


@dataclass
class DownloadOSMRoads(_OSMBoundaryIO):
    timeout: int = 300       # per-tile Overpass timeout (seconds)
    max_attempts: int = 5    # retries per tile
    sleep_base: float = 3.0  # base backoff (seconds); actual wait = base * attempt + jitter

    # Tile area target in sq-degrees. Tiles are sized adaptively from the actual bbox:
    _TILE_AREA_DEG_SQ: float = 0.25
    _MAX_WORKERS: int = 8   

    _OVERPASS_MIRRORS: List[str] = None 
    def __post_init__(self):
        self._OVERPASS_MIRRORS = [
            "https://overpass-api.de/api/interpreter",
            "https://lz4.overpass-api.de/api/interpreter",
            "https://z.overpass-api.de/api/interpreter",
        ]
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "fimbox/0.1 (+https://github.com/sdmlua/fimbox)"

    # adaptive tiling

    def _make_tiles(
        self, minx: float, miny: float, maxx: float, maxy: float
    ) -> List[Tuple[float, float, float, float]]:
        area = (maxx - minx) * (maxy - miny)
        n = max(1, math.ceil(area / self._TILE_AREA_DEG_SQ))
        # distribute tiles to respect the bbox aspect ratio
        aspect = (maxx - minx) / max(maxy - miny, 1e-9)
        ny = max(1, round(math.sqrt(n / aspect)))
        nx = max(1, math.ceil(n / ny))
        dx = (maxx - minx) / nx
        dy = (maxy - miny) / ny
        return [
            (minx + i * dx, miny + j * dy, minx + (i + 1) * dx, miny + (j + 1) * dy)
            for i in range(nx) for j in range(ny)
        ]

    def _n_workers(self, n_tiles: int) -> int:
        return min(n_tiles, self._MAX_WORKERS)

    # Overpass
    def _overpass_query(self, bbox: Tuple[float, float, float, float]) -> dict:
        # Overpass bbox order: S,W,N,E
        minx, miny, maxx, maxy = bbox
        query = (
            f"[out:json][timeout:{self.timeout}];"
            f"(way[\"highway\"~\"^motorway$|^trunk$|^primary$|^secondary$|^tertiary$\"]"
            f"[!\"bridge\"]({miny},{minx},{maxy},{maxx}););"
            f"out body;>;out skel qt;"
        )
        # Must use GET with params={'data': ...}; POST with raw body returns 406 on overpass-api.de
        last_exc: Exception = RuntimeError("no attempt")
        for attempt in range(1, self.max_attempts + 1):
            mirror = self._OVERPASS_MIRRORS[(attempt - 1) % len(self._OVERPASS_MIRRORS)]
            try:
                r = self._session.get(mirror, params={"data": query}, timeout=self.timeout + 60)
                if r.status_code in (429, 502, 503, 504):
                    raise RuntimeError(f"HTTP {r.status_code} from {mirror}")
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_attempts:
                    time.sleep(self.sleep_base * attempt + random.uniform(0, 2.0))
        raise RuntimeError(f"Overpass query failed after {self.max_attempts} attempts: {last_exc}") from last_exc

    # JSON → GDF
    @staticmethod
    def _json_to_lines_gdf(osm_json: dict) -> gpd.GeoDataFrame:
        elems = osm_json.get("elements", [])
        nodes = {e["id"]: (e["lon"], e["lat"]) for e in elems if e.get("type") == "node"}
        rows = []
        for e in elems:
            if e.get("type") != "way":
                continue
            coords = [nodes[nid] for nid in e.get("nodes", []) if nid in nodes]
            if len(coords) < 2:
                continue
            tags = e.get("tags") or {}
            rows.append({
                "osmid":    str(e["id"]),
                "highway":  tags.get("highway", "unknown"),
                "name":     tags.get("name", ""),
                "ref":      tags.get("ref", ""),
                "surface":  tags.get("surface", ""),
                "lanes":    tags.get("lanes", ""),
                "geometry": LineString(coords),
            })
        if not rows:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return gpd.GeoDataFrame(rows, crs="EPSG:4326")

    # parallel fetch
    def _fetch_tile(self, tile: Tuple[float, float, float, float]) -> gpd.GeoDataFrame:
        try:
            return self._json_to_lines_gdf(self._overpass_query(tile))
        except Exception as exc:
            tqdm.write(f"  [warn] tile {tile} skipped: {exc}")
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    def _fetch_all(self, tiles: List[Tuple[float, float, float, float]]) -> gpd.GeoDataFrame:
        workers = self._n_workers(len(tiles))
        parts: List[gpd.GeoDataFrame] = []

        if workers == 1:
            for tile in tqdm(tiles, desc="OSM roads", unit="tile"):
                r = self._fetch_tile(tile)
                if not r.empty:
                    parts.append(r)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._fetch_tile, t): t for t in tiles}
                with tqdm(total=len(tiles), desc="OSM road tiles", unit="tile") as pbar:
                    for fut in as_completed(futures):
                        r = fut.result()
                        if not r.empty:
                            parts.append(r)
                        pbar.update(1)

        if not parts:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")

    # public API
    def query_to_gdf(
        self,
        boundary: Union[
            gpd.GeoDataFrame, gpd.GeoSeries,
            Polygon, MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float], str, Path,
        ],
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
        clip_to_boundary: bool = True,
    ) -> gpd.GeoDataFrame:
        geom4326 = self._boundary_to_geom4326(boundary, boundary_layer, boundary_crs)
        minx, miny, maxx, maxy = geom4326.bounds
        tiles = self._make_tiles(minx, miny, maxx, maxy)
        print(f"OSM roads: {len(tiles)} tile(s), {self._n_workers(len(tiles))} worker(s).")

        gdf = self._fetch_all(tiles)
        if gdf.empty:
            print("No road features returned.")
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        gdf = gdf.drop_duplicates(subset=["osmid"]).reset_index(drop=True)
        print(f"  {len(gdf)} unique segments after dedup.")

        if clip_to_boundary:
            gdf = gpd.clip(gdf, gpd.GeoDataFrame(geometry=[geom4326], crs="EPSG:4326"), keep_geom_type=True)
            print(f"  {len(gdf)} segments after clipping.")

        return gdf.to_crs(epsg=self.out_sr)

    def download(
        self,
        boundary: Union[
            gpd.GeoDataFrame, gpd.GeoSeries,
            Polygon, MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float], str, Path,
        ],
        out_dir: Union[str, Path],
        out_name: str = "osm_roads.gpkg",
        out_layer: str = "osm_roads",
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
        ourfile: Optional[str] = None,
        ourlayer: Optional[str] = None,
    ) -> gpd.GeoDataFrame:
        if ourfile:
            out_name = ourfile
        if ourlayer:
            out_layer = ourlayer
        gdf = self.query_to_gdf(boundary=boundary, boundary_layer=boundary_layer,
                                 boundary_crs=boundary_crs, clip_to_boundary=True)
        out_path = self._write_gpkg(gdf, out_dir, out_name, out_layer)
        print(f"Saved: {out_path}")
        return gdf


@dataclass
class DownloadOSMBridges(_OSMBoundaryIO):
    requests_timeout: int = 300
    max_attempts: int = 5
    sleep_base: float = 2.0
    dissolve_buffer: float = 0.0001  # dissolve happens in EPSG:4326
    drop_list_columns: bool = True

    @staticmethod
    def _find_touching_groups(gdf: gpd.GeoDataFrame) -> List[set]:
        graph = Graph()
        graph.add_nodes_from(gdf.index)
        spatial_index = gdf.sindex
        for idx, row in gdf.iterrows():
            geom = row.geometry
            cand_idx = list(spatial_index.intersection(geom.bounds))
            cand = gdf.iloc[cand_idx]
            hits = cand[cand.intersects(geom)]
            for midx in hits.index:
                if midx != idx:
                    graph.add_edge(idx, midx)
        return list(connected_components(graph))

    @staticmethod
    def _clean_schema(gdf: gpd.GeoDataFrame, drop_list_columns: bool = True) -> gpd.GeoDataFrame:
        if gdf is None or len(gdf) == 0:
            return gdf

        if drop_list_columns:
            cols_to_drop = []
            for col in gdf.columns:
                try:
                    if any(isinstance(v, list) for v in gdf[col].dropna()):
                        cols_to_drop.append(col)
                except Exception:
                    pass
            if cols_to_drop:
                gdf = gdf.drop(columns=list(set(cols_to_drop)))

        bad_column_names = [
            "id",
            "fid",
            "ID",
            "fixme",
            "FIXME",
            "NYSDOT_ref",
            "REF",
            "fixme:maxspeed",
            "LAYER",
            "unsigned_ref",
            "Fut_Ref",
            "Ref",
            "FIXME:ref",
        ]
        cols_to_drop2 = [c for c in bad_column_names if c in gdf.columns]
        if cols_to_drop2:
            gdf = gdf.drop(columns=cols_to_drop2)

        return gdf

    @staticmethod
    def _make_bridge_type(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if "highway" not in gdf.columns:
            gdf["highway"] = None
        if "railway" not in gdf.columns:
            gdf["railway"] = None

        gdf["bridge_type"] = gdf.apply(
            lambda row: (
                f"highway-{row['highway']}"
                if pd.notna(row["highway"])
                else f"railway-{row['railway']}"
            ),
            axis=1,
        )
        return gdf

    @staticmethod
    def _filter_unwanted_bridge_types(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        unwanted_bridge_types = [
            "highway-razed",
            "highway-proposed",
            "highway-abandoned",
            "highway-destroyed",
            "highway-dismantled",
            "highway-demolished",
            "railway-razed",
            "railway-proposed",
            "railway-abandoned",
            "railway-destroyed",
            "railway-dismantled",
            "railway-demolished",
        ]
        if "bridge_type" in gdf.columns:
            gdf = gdf[~gdf["bridge_type"].isin(unwanted_bridge_types)]
        return gdf

    @staticmethod
    def _force_lines(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if gdf.empty:
            return gdf

        def to_line(geom):
            if geom is None:
                return None
            gt = geom.geom_type
            if gt in ("LineString", "MultiLineString"):
                return geom
            if gt == "Polygon":
                return LineString(geom.exterior.coords)
            if gt == "MultiPolygon":
                polys = list(geom.geoms)
                if not polys:
                    return None
                p = max(polys, key=lambda x: x.area)
                return LineString(p.exterior.coords)
            return None

        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.apply(to_line)
        gdf = gdf[gdf.geometry.notna()].copy()
        return gdf

    def _pull_bridges_osmnx(self, geom4326: Union[Polygon, MultiPolygon]) -> gpd.GeoDataFrame:
        # osmnx reads better in 4326; we output 5070 later
        ox.settings.requests_timeout = self.requests_timeout

        for attempt in range(1, self.max_attempts + 1):
            try:
                gdf = ox.features_from_polygon(geom4326, {"bridge": True})
                if gdf is None or len(gdf) == 0:
                    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

                # OSMnx returns multiindex (element, id). Keep id as osmid and drop element.
                if isinstance(gdf.index, pd.MultiIndex) and "element" in gdf.index.names:
                    gdf = gdf.droplevel("element")

                gdf = gdf.copy()
                gdf["osmid"] = gdf.index.astype(str)

                gdf = gdf.reset_index(drop=True)
                if gdf.crs is None:
                    gdf = gdf.set_crs("EPSG:4326")
                else:
                    gdf = gdf.to_crs("EPSG:4326")

                return gdf

            except Exception as e:
                wait = self.sleep_base * attempt + random.uniform(0, 1.5)
                time.sleep(wait)
                if attempt == self.max_attempts:
                    raise RuntimeError(
                        f"osmnx bridges query failed after {self.max_attempts} attempts: {e}"
                    ) from e

        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    def _dissolve_touching(self, gdf_lines_4326: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if gdf_lines_4326.empty:
            return gdf_lines_4326

        buffered = gdf_lines_4326.copy()
        buffered["geometry"] = buffered.geometry.buffer(self.dissolve_buffer)

        groups = self._find_touching_groups(buffered)

        warnings.filterwarnings("ignore")
        dissolved_groups = []
        for grp in groups:
            gg = buffered.loc[list(grp)]
            if gg.empty:
                continue
            d = gg.dissolve()
            d = d.explode(index_parts=False)
            dissolved_groups.append(d)

        if not dissolved_groups:
            out = buffered.copy()
        else:
            out = gpd.GeoDataFrame(pd.concat(dissolved_groups, ignore_index=True), crs=buffered.crs)

        # buffered polygons -> linestring exteriors
        out["geometry"] = out.geometry.apply(
            lambda geom: LineString(geom.exterior.coords)
            if geom is not None and geom.geom_type == "Polygon"
            else geom
        )
        out = out[out.geometry.notna()].copy()
        return out

    def query_to_gdf(
        self,
        boundary: Union[
            gpd.GeoDataFrame,
            gpd.GeoSeries,
            Polygon,
            MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float],
            str,
            Path,
        ],
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
        clip_to_boundary: bool = True,
    ) -> gpd.GeoDataFrame:
        geom4326 = self._boundary_to_geom4326(boundary, boundary_layer, boundary_crs)

        gdf = self._pull_bridges_osmnx(geom4326)
        if gdf.empty:
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        gdf = self._clean_schema(gdf, drop_list_columns=self.drop_list_columns)
        gdf = self._make_bridge_type(gdf)
        gdf = self._filter_unwanted_bridge_types(gdf)
        gdf = self._force_lines(gdf)

        if gdf.empty:
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        # dissolve touching (in 4326), then clip (in 4326)
        gdf = gdf.to_crs("EPSG:4326")
        gdf = self._dissolve_touching(gdf)

        if clip_to_boundary and not gdf.empty:
            boundary_gdf = gpd.GeoDataFrame(geometry=[geom4326], crs="EPSG:4326")
            gdf = gpd.clip(gdf, boundary_gdf, keep_geom_type=True)

        if gdf.empty:
            return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{self.out_sr}")

        return gdf.to_crs(epsg=self.out_sr)

    def download(
        self,
        boundary: Union[
            gpd.GeoDataFrame,
            gpd.GeoSeries,
            Polygon,
            MultiPolygon,
            Tuple[float, float, float, float],
            Sequence[float],
            str,
            Path,
        ],
        out_dir: Union[str, Path],
        out_name: str = "osm_bridges.gpkg",
        out_layer: str = "osm_bridges",
        boundary_layer: Optional[str] = None,
        boundary_crs: Optional[Union[str, int]] = None,
        ourfile: Optional[str] = None,
        ourlayer: Optional[str] = None,
    ) -> gpd.GeoDataFrame:
        if ourfile:
            out_name = ourfile
        if ourlayer:
            out_layer = ourlayer

        gdf = self.query_to_gdf(
            boundary=boundary,
            boundary_layer=boundary_layer,
            boundary_crs=boundary_crs,
            clip_to_boundary=True,
        )
        self._write_gpkg(gdf, out_dir, out_name, out_layer)
        return gdf


# CLI--> single entry; choose roads vs bridges via --mode
if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Download OSM roads (excluding bridges) OR OSM bridges within a boundary; output EPSG:5070 GPKG."
    )
    p.add_argument(
        "--mode",
        required=True,
        choices=["roads", "bridges"],
        help="Which dataset to download",
    )
    p.add_argument(
        "--boundary",
        required=True,
        help="Boundary file path (shp/gpkg/geojson) OR bbox 'minx,miny,maxx,maxy' (assumed EPSG:4326 unless --boundary_crs given)",
    )
    p.add_argument("--out_dir", required=True, help="Output directory")
    p.add_argument("--out_name", default=None, help="Output GeoPackage name (defaults depend on mode)")
    p.add_argument("--out_layer", default=None, help="Output layer name (defaults depend on mode)")
    p.add_argument("--boundary_layer", default=None, help="Boundary layer name if boundary is a GeoPackage")
    p.add_argument("--boundary_crs", default=None, help="CRS for bbox/shapely boundary, e.g. 4326")
    args = p.parse_args()

    boundary_val: Any = args.boundary
    boundary_crs_val = int(args.boundary_crs) if args.boundary_crs is not None else None

    # bbox convenience: "minx,miny,maxx,maxy"
    if isinstance(boundary_val, str) and "," in boundary_val:
        parts = [s.strip() for s in boundary_val.split(",")]
        if len(parts) == 4:
            try:
                boundary_val = tuple(float(s) for s in parts)
            except ValueError:
                pass

    if args.mode == "roads":
        dl = DownloadOSMRoads(out_sr=5070)
        dl.download(
            boundary=boundary_val,
            out_dir=args.out_dir,
            out_name=args.out_name or "osm_roads.gpkg",
            out_layer=args.out_layer or "osm_roads",
            boundary_layer=args.boundary_layer,
            boundary_crs=boundary_crs_val,
        )
    else:
        dl = DownloadOSMBridges(out_sr=5070)
        dl.download(
            boundary=boundary_val,
            out_dir=args.out_dir,
            out_name=args.out_name or "osm_bridges.gpkg",
            out_layer=args.out_layer or "osm_bridges",
            boundary_layer=args.boundary_layer,
            boundary_crs=boundary_crs_val,
        )
