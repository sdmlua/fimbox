"""D8 flow-direction plumbing shared by the raster traversals.

Both the label propagation in :mod:`gage_catchments` and the flow accumulation in
:mod:`flowacc_dem` need the same thing first: for every cell, where does its water
go. One table, one builder, so the two can't drift apart.
"""

from __future__ import annotations

import numpy as np

# WBT D8 pointer encoding: powers-of-2 --> (row_offset, col_offset)
# 64=N  128=NE  1=E  2=SE  4=S  8=SW  16=W  32=NW
D8_OFFSETS: dict[int, tuple[int, int]] = {
    1: (0, 1),
    2: (1, 1),
    4: (1, 0),
    8: (1, -1),
    16: (0, -1),
    32: (-1, -1),
    64: (-1, 0),
    128: (-1, 1),
}


def downstream_index(d8: np.ndarray) -> np.ndarray:
    """Flat index of each cell's downstream neighbour; itself where flow stops.

    Nodata must already be zeroed by the caller. Uses shifted slices rather than
    per-cell row/col arithmetic, so a HUC8 costs a few views instead of several
    whole-grid index copies.
    """
    rows, cols = d8.shape
    codes = d8.astype(np.int32, copy=False)
    flat_base = np.arange(rows * cols, dtype=np.int32).reshape(rows, cols)
    ds = flat_base.copy()

    for code, (dr, dc) in D8_OFFSETS.items():
        sel = codes == code
        if not sel.any():
            continue
        # Only the window whose neighbour lands on the grid can flow; cells
        # pointing off the edge keep their self-loop.
        r0, r1 = max(0, -dr), rows - max(0, dr)
        c0, c1 = max(0, -dc), cols - max(0, dc)
        if r0 >= r1 or c0 >= c1:
            continue
        np.copyto(
            ds[r0:r1, c0:c1],
            flat_base[r0 + dr : r1 + dr, c0 + dc : c1 + dc],
            where=sel[r0:r1, c0:c1],
        )
    return ds.ravel()
