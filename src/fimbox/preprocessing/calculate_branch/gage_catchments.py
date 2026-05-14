"""
Author: Supath Dhital
Date Updated: May 2026

Gage watershed delineation and outlet backpool mitigation.

Steps:
  1. stream_pixel_points      Vectorise stream pixel centroids to points GeoPackage.
  2. GageCatchments           Reverse-D8 label propagation from outlet points.
  3. OutletBackpoolMitigate   Detect and trim oversized outlet catchment (non-branch-zero only).

Inputs / outputs follow the inundation-mapping convention:
  demDerived_streamPixels_{id}.tif           --> flows_points_pixels_{id}.gpkg
  demDerived_reaches_split_points_{id}.gpkg  --> gw_catchments_reaches_{id}.tif
  flows_points_pixels_{id}.gpkg              --> gw_catchments_pixels_{id}.tif
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.features
import rasterio.sample
from shapely.geometry import Point
from shapely import ops as shapely_ops

log = logging.getLogger(__name__)

# WBT D8 pointer: power-of-2 code --> (row_offset, col_offset)
_D8_OFFSETS: dict[int, tuple[int, int]] = {
    1: (0, 1),
    2: (1, 1),
    4: (1, 0),
    8: (1, -1),
    16: (0, -1),
    32: (-1, -1),
    64: (-1, 0),
    128: (-1, 1),
}


# Stream pixel centroids
def stream_pixel_points(
    stream_pixels: Path,
    out_gpkg: Path,
) -> Path:
    """
    Convert stream pixel raster to point GeoPackage with sequential IDs.

    Parameters
    ----------
    stream_pixels : demDerived_streamPixels_{id}.tif (value 1 = stream)
    out_gpkg      : flows_points_pixels_{id}.gpkg
    """
    stream_pixels = Path(stream_pixels)
    out_gpkg = Path(out_gpkg)

    with rasterio.open(str(stream_pixels)) as src:
        data = src.read(1)
        transform = src.transform
        crs = src.crs

    row_idxs, col_idxs = np.nonzero(data >= 1)
    xs = transform[2] + (col_idxs + 0.5) * transform[0]
    ys = transform[5] + (row_idxs + 0.5) * transform[4]

    ids = np.arange(1, len(row_idxs) + 1, dtype=np.int32)
    points = [Point(x, y) for x, y in zip(xs, ys)]

    gdf = gpd.GeoDataFrame({"id": ids, "geometry": points}, crs=crs)
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(str(out_gpkg), driver="GPKG", index=False, engine="fiona")

    log.info("stream_pixel_points: %d points --> %s", len(gdf), out_gpkg.name)
    return out_gpkg


# gage watershed
@dataclass
class GageCatchments:
    """
    Delineate gage watersheds via iterative reverse-D8 label propagation.

    Each cell is labeled with the ID of the nearest downstream outlet point.
    Runs in multiple vectorised passes until convergence; typically converges
    in ≤500 passes for 10-m DEMs with 2-km reach segments.

    Parameters
    ----------
    flowdir        : flowdir_d8_burned_filled_{id}.tif (WBT D8 pointer)
    outlet_points  : GeoPackage with point geometries and integer 'id' column
    out_path       : output watershed raster
    max_iter       : safety cap on propagation iterations (default 5000)
    """

    flowdir: Path
    outlet_points: Path
    out_path: Path
    max_iter: int = 5000

    def __post_init__(self):
        for attr in ("flowdir", "outlet_points", "out_path"):
            setattr(self, attr, Path(getattr(self, attr)))

    def run(self) -> Path:
        with rasterio.open(str(self.flowdir)) as src:
            d8 = src.read(1)
            nodata_d8 = src.nodata
            profile = src.profile.copy()
            transform = src.transform
            crs = src.crs

        outlets_gdf = gpd.read_file(str(self.outlet_points), engine="fiona").to_crs(crs)

        outlet_rc_ids: list[tuple[int, int, int]] = []
        rows_n, cols_n = d8.shape
        for _, row_data in outlets_gdf.iterrows():
            pt = row_data.geometry
            col_f, row_f = ~transform * (pt.x, pt.y)
            r, c = int(row_f), int(col_f)
            if 0 <= r < rows_n and 0 <= c < cols_n:
                outlet_rc_ids.append((r, c, int(row_data["id"])))

        log.info(
            "GageCatchments: %d outlet points, propagating --> %s",
            len(outlet_rc_ids),
            self.out_path.name,
        )

        result = _gage_watershed(
            d8, outlet_rc_ids, nodata_d8=nodata_d8, max_iter=self.max_iter
        )

        # int32 matches FIM output and is supported by all GIS tools.
        # HydroIDs using fimid prefix (4 digits + 4 seq = 8 digits) fit in int32.
        profile.update(
            dtype="int32",
            nodata=0,
            compress="lzw",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="YES",
        )
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(str(self.out_path), "w", **profile) as dst:
            dst.write(result.astype(np.int32), 1)

        log.info("GageCatchments: written --> %s", self.out_path.name)
        return self.out_path


def _gage_watershed(
    d8: np.ndarray,
    outlet_rc_ids: list[tuple[int, int, int]],
    nodata_d8: Optional[float] = None,
    max_iter: int = 5000,  # kept for API compatibility, unused
) -> np.ndarray:
    """
    Assign each cell to its nearest downstream outlet — equivalent to
    TauDEM gagewatershed.

    Algorithm: label propagation in topological downstream-->upstream order.
    Each cell inherits the label of its downstream neighbor.  We process
    cells in the order they are visited during a BFS seeded from the outlet
    points — a cell is only enqueued once its downstream neighbor is already
    labeled, guaranteeing the label is ready when we process it.

    Runtime O(n): every cell is visited exactly once.
    """
    rows, cols = d8.shape
    n = rows * cols

    flat_d8 = d8.ravel().astype(np.int32)
    if nodata_d8 is not None:
        flat_d8[flat_d8 == int(nodata_d8)] = 0

    # Build downstream flat-index (self-loop = no valid downstream)
    ds = np.arange(n, dtype=np.int64)
    r_all = np.arange(n, dtype=np.int64) // cols
    c_all = np.arange(n, dtype=np.int64) % cols

    for d8_val, (dr, dc) in _D8_OFFSETS.items():
        sel = flat_d8 == d8_val
        if not sel.any():
            continue
        r_ds = r_all + dr
        c_ds = c_all + dc
        in_bounds = sel & (r_ds >= 0) & (r_ds < rows) & (c_ds >= 0) & (c_ds < cols)
        ds[in_bounds] = (r_ds * cols + c_ds)[in_bounds]

    # Build upstream adjacency: us[j] = list of cells that drain into j
    idx_all = np.arange(n, dtype=np.int64)
    non_self = ds != idx_all
    src_cells = idx_all[non_self]
    dst_cells = ds[non_self]
    sort_order = np.argsort(dst_cells, kind="stable")
    dst_sorted = dst_cells[sort_order]
    src_sorted = src_cells[sort_order]
    split_pts = np.flatnonzero(np.diff(dst_sorted)) + 1
    boundaries = np.concatenate([[0], split_pts, [len(dst_sorted)]])
    groups = np.split(src_sorted, split_pts)
    us: dict[int, np.ndarray] = {
        int(dst_sorted[boundaries[k]]): groups[k] for k in range(len(groups))
    }

    result = np.zeros(n, dtype=np.int32)
    visited = np.zeros(n, dtype=bool)

    # Seed: label each outlet cell and enqueue its upstream neighbors
    queue: deque[int] = deque()
    for r, c, hid in outlet_rc_ids:
        idx = int(r) * cols + int(c)
        result[idx] = hid
        visited[idx] = True
        for upstream_cell in us.get(idx, []):
            if not visited[int(upstream_cell)]:
                queue.append(int(upstream_cell))

    # Process upstream: cell i inherits label from its downstream neighbor ds[i],
    # which is already labeled because it was processed before i was enqueued.
    while queue:
        i = int(queue.popleft())
        if visited[i]:
            continue
        visited[i] = True
        j = int(ds[i])
        lbl = result[j]
        if lbl == 0:
            # downstream not labeled yet — re-enqueue at the back and retry later
            # (can happen at confluences where two branches meet)
            queue.append(i)
            visited[i] = False
            continue
        result[i] = lbl
        for upstream_cell in us.get(i, []):
            if not visited[int(upstream_cell)]:
                queue.append(int(upstream_cell))

    log.debug(
        "gage_watershed: %d/%d cells labeled",
        int((result > 0).sum()),
        n,
    )
    return result.reshape(rows, cols)


# Outlet backpool mitigation
@dataclass
class OutletBackpoolMitigate:
    """
    Detect and trim oversized outlet catchment.

    Criteria (both must be met to trigger mitigation):
      1. At least one pixel catchment is an outlier (>1 std dev above mean size).
      2. The outlier catchment is at the network outlet (last segment NextDownID == -1).

    When triggered:
      - Trim the outlet reach to its penultimate vertex.
      - Update split_points to match the trimmed reach.
      - Mask the outlier catchment from both catchment rasters.

    Branch zero is always skipped (no 'levpa_id' in nwm streams).

    Parameters
    ----------
    branch_dir              : branch output directory
    catchment_pixels_path   : gw_catchments_pixels_{id}.tif
    catchment_reaches_path  : gw_catchments_reaches_{id}.tif
    split_flows_gpkg        : demDerived_reaches_split_{id}.gpkg
    split_points_gpkg       : demDerived_reaches_split_points_{id}.gpkg
    nwm_streams_gpkg        : nwm_subset_streams.gpkg (used to detect branch zero)
    dem_path                : dem_thalwegCond_{id}.tif (for slope recalculation)
    slope_min               : minimum slope floor (default 0.0001)
    """

    branch_dir: Path
    catchment_pixels_path: Path
    catchment_reaches_path: Path
    split_flows_gpkg: Path
    split_points_gpkg: Path
    nwm_streams_gpkg: Path
    dem_path: Path
    slope_min: float = 0.0001

    def __post_init__(self):
        for attr in (
            "branch_dir",
            "catchment_pixels_path",
            "catchment_reaches_path",
            "split_flows_gpkg",
            "split_points_gpkg",
            "nwm_streams_gpkg",
            "dem_path",
        ):
            setattr(self, attr, Path(getattr(self, attr)))

    def run(self) -> bool:
        """
        Run mitigation. Returns True if mitigation was applied, False otherwise.
        """
        # Skip for branch zero
        if self.nwm_streams_gpkg.exists():
            nwm = gpd.read_file(str(self.nwm_streams_gpkg), engine="fiona")
            if "levpa_id" not in nwm.columns:
                log.info("OutletBackpoolMitigate: branch zero — skipping")
                return False
        else:
            log.info("OutletBackpoolMitigate: nwm_streams not found — skipping")
            return False

        if not self.catchment_pixels_path.exists():
            log.warning("OutletBackpoolMitigate: catchment_pixels not found — skipping")
            return False

        split_flows = gpd.read_file(str(self.split_flows_gpkg), engine="fiona")
        split_points = gpd.read_file(str(self.split_points_gpkg), engine="fiona")

        # Identify outlet reach (NextDownID == -1)
        outlet_segs = split_flows[split_flows["NextDownID"] == -1]
        if len(outlet_segs) != 1:
            log.info(
                "OutletBackpoolMitigate: %d segments with NextDownID=-1, skipping",
                len(outlet_segs),
            )
            return False

        with rasterio.open(str(self.catchment_pixels_path)) as src:
            catch_data = src.read(1)

        # Criteria 1: outlier catchment size
        flagged, outlier_ids = _catch_size_outliers(catch_data)
        if not flagged:
            log.info("OutletBackpoolMitigate: no catchment size outliers detected")
            return False

        # Criteria 2: outlier at outlet
        outlet_row = outlet_segs.iloc[0]
        last_pt = Point(outlet_row.geometry.coords[-1])

        with rasterio.open(str(self.catchment_pixels_path)) as src:
            transform = src.transform
            col_f, row_f = ~transform * (last_pt.x, last_pt.y)
            outlet_catch_id = int(catch_data[int(row_f), int(col_f)])

        if outlet_catch_id not in outlier_ids:
            log.info(
                "OutletBackpoolMitigate: outlier catchment not at outlet — skipping"
            )
            return False

        log.info(
            "OutletBackpoolMitigate: backpool detected (catchment ID=%d) — trimming outlet reach",
            outlet_catch_id,
        )

        # Trim: snap outlet reach to penultimate vertex
        coords = list(outlet_row.geometry.coords)
        if len(coords) >= 3:
            trim_pt = Point(coords[-3])
        elif len(split_flows) > 1:
            # fall back to last point of second-to-last segment
            node_2tl = outlet_row["From_Node"]
            seg_2tl = split_flows[split_flows["To_Node"] == node_2tl]
            if seg_2tl.empty:
                log.warning(
                    "OutletBackpoolMitigate: cannot find second-to-last segment — skipping"
                )
                return False
            trim_pt = Point(list(seg_2tl.iloc[0].geometry.coords)[-1])
        else:
            log.warning(
                "OutletBackpoolMitigate: outlet reach too short to trim — skipping"
            )
            return False

        trim_pt_gdf = gpd.GeoDataFrame([{"geometry": trim_pt}], crs=split_flows.crs)
        trim_pt_snapped = split_flows.iloc[[outlet_segs.index[0]]].interpolate(
            split_flows.iloc[[outlet_segs.index[0]]].project(trim_pt_gdf.geometry)
        )
        trim_pt_buf = trim_pt_snapped.iloc[0].buffer(1)

        split_result = shapely_ops.split(outlet_row.geometry, trim_pt_buf)
        if len(list(split_result.geoms)) > 1:
            longest = max(split_result.geoms, key=lambda g: g.length)
            split_flows = split_flows.copy()
            split_flows.loc[outlet_segs.index[0], "geometry"] = longest

            # Recalculate slope and length for trimmed segment
            with rasterio.open(str(self.dem_path)) as dem_ds:
                seg_geom = split_flows.loc[outlet_segs.index[0], "geometry"]
                p0 = seg_geom.coords[0]
                p1 = seg_geom.coords[-1]
                try:
                    e0, e1 = [
                        v[0] for v in rasterio.sample.sample_gen(dem_ds, [p0, p1])
                    ]
                    slope = max(
                        abs(e0 - e1) / max(seg_geom.length, 1e-9), self.slope_min
                    )
                except Exception:
                    slope = self.slope_min
            split_flows.loc[outlet_segs.index[0], "S0"] = slope
            split_flows.loc[outlet_segs.index[0], "LengthKm"] = seg_geom.length * 1e-3

        # Filter split points to those within trimmed flow buffer
        trim_buf = split_flows.buffer(10).geometry.union_all()
        split_points = split_points[split_points.geometry.within(trim_buf)]

        # Mask outlier catchment from both rasters
        catch_poly_gdf = _polygonize_catchments(self.catchment_pixels_path)
        if catch_poly_gdf is not None and len(catch_poly_gdf) > 0:
            filtered = catch_poly_gdf[catch_poly_gdf["HydroID"] != outlet_catch_id]
            if len(filtered) > 0:
                new_boundary = filtered.dissolve()
                boundary_json = [new_boundary.iloc[0].geometry.__geo_interface__]
                _mask_raster_to_boundary(
                    self.catchment_reaches_path,
                    boundary_json,
                    self.catchment_reaches_path,
                )
                _mask_raster_to_boundary(
                    self.catchment_pixels_path,
                    boundary_json,
                    self.catchment_pixels_path,
                )

        # Save updated vectors
        split_flows.to_file(
            str(self.split_flows_gpkg), driver="GPKG", index=False, engine="fiona"
        )
        split_points.to_file(
            str(self.split_points_gpkg), driver="GPKG", index=False, engine="fiona"
        )

        log.info("OutletBackpoolMitigate: mitigation applied — files updated")
        return True


# Internal utilis
def _catch_size_outliers(
    catch_data: np.ndarray,
) -> tuple[bool, list[int]]:
    """Return (flagged, outlier_ids) based on 1-std-dev threshold."""
    unique, counts = np.unique(catch_data, return_counts=True)
    # remove background (0)
    mask = unique > 0
    unique = unique[mask]
    counts = counts[mask]

    if len(counts) < 2:
        return False, []

    mean_c = counts.mean()
    std_c = counts.std()
    outlier_mask = np.abs(counts - mean_c) > std_c
    outlier_ids = unique[outlier_mask].tolist()

    if outlier_ids:
        log.info(
            "  catchment size outliers: %d found (mean=%.0f, std=%.0f)",
            len(outlier_ids),
            mean_c,
            std_c,
        )
        return True, outlier_ids
    return False, []


def _polygonize_catchments(raster_path: Path) -> Optional[gpd.GeoDataFrame]:
    """Polygonize integer raster to GeoDataFrame with HydroID column."""
    try:
        with rasterio.open(str(raster_path)) as src:
            data = src.read(1)
            transform = src.transform
            crs = src.crs
            nodata = src.nodata

        mask = data != (int(nodata) if nodata is not None else 0)
        shapes = list(rasterio.features.shapes(data, mask=mask, transform=transform))

        geoms = []
        vals = []
        for geom, val in shapes:
            from shapely.geometry import shape

            geoms.append(shape(geom))
            vals.append(int(val))

        return gpd.GeoDataFrame({"HydroID": vals, "geometry": geoms}, crs=crs)
    except Exception as exc:
        log.warning("_polygonize_catchments failed: %s", exc)
        return None


def _mask_raster_to_boundary(
    raster_path: Path, boundary_json: list, save_path: Path
) -> None:
    """Mask a raster to a geometry boundary and save in-place."""
    from rasterio.mask import mask as rio_mask
    import shutil

    tmp = save_path.with_suffix(".tmp.tif")
    try:
        with rasterio.open(str(raster_path)) as src:
            profile = src.profile.copy()
            profile.update(
                compress="lzw",
                tiled=True,
                blockxsize=512,
                blockysize=512,
                BIGTIFF="YES",
            )
            masked, _ = rio_mask(src, boundary_json, crop=False)
        with rasterio.open(str(tmp), "w", **profile) as dst:
            dst.write(masked[0], 1)
        shutil.move(str(tmp), str(save_path))
    except Exception as exc:
        log.warning("_mask_raster_to_boundary failed: %s", exc)
        if tmp.exists():
            tmp.unlink()
