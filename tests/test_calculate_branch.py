import shutil
from pathlib import Path

import fimbox
import geopandas as gpd

out_dir = "/Users/supath/Downloads/MSResearch/FIMBOX/out/HUC03020202"

"""Creating a branches using flowlines, catchments, and lakes from the staged hydro data. 
This test checks that the branch derivation process runs successfully and produces the expected
output files. The test verifies that the levelpaths, branch polygons, and branch list files are 
created in the specified output directory.
"""
# def test_branch_derivation():
#     result = fimbox.BranchDerivation(
#         out_dir=out_dir,
#     ).run()
#     assert result.levelpaths.exists()
#     assert result.branch_polygons.exists()
#     assert result.branch_list.exists()
#     assert result.output_dir == Path(out_dir).resolve()

# """
# This is for using custom paths; using different flowlines and catchments
# """
# def test_branch_derivation_custom(out_dir=out_dir):
#     result = fimbox.BranchDerivation(
#         out_dir=out_dir,
#         stream_network="path/to/stream_network.gpkg",
#         catchments="path/to/catchments.gpkg",
#         boundary="path/to/boundary.gpkg",
#         buffered_boundary="path/to/buffered_boundary.gpkg",
#         lakes="path/to/lakes.gpkg",
#         headwaters="path/to/headwaters.gpkg",
#         reach_id_attribute="flow_id",
#         catchment_reach_id_attribute="flow_ref",
#         stream_order_attribute="stream_order",
#         min_stream_order=3,
#     ).run()

#     assert result.levelpaths.exists()
#     assert result.catchments_levelpaths.exists()
#     assert result.branch_list.exists()


"""
Branch Zero preprocessing.
Runs on HUC 03020202 staged data.
"""
out_dir = "/Users/supath/Downloads/MSResearch/FIMBOX/out/HUC03020202"
dem_path = f"{out_dir}/dem.tif"
streams = f"{out_dir}/nwm_subset_streams.gpkg"
boundary = f"{out_dir}/wbd_buffered.gpkg"
bridge_diff = f"{out_dir}/bridge_elev_diff.tif"


def test_branch_zero():
    outputs = fimbox.BranchZero(
        dem_path=dem_path,
        streams_gpkg=streams,
        boundary_gpkg=boundary,
        out_dir=out_dir,
        bridge_elev_diff_path=bridge_diff,
        agree_buffer_m=15.0,
        agree_smooth_drop=10.0,
        agree_sharp_drop=1000.0,
    ).run()

    print("Outputs:")
    for key, path in outputs.items():
        print(f"  {key}: {path}")

    assert outputs["dem_meters"].exists()
    assert outputs["flows_grid_boolean"].exists()
    assert outputs["dem_burned"].exists()
    assert outputs["dem_burned_filled"].exists()
    assert outputs["flowdir_d8"].exists()
