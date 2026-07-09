"""
Method A — bathymetry wedge with TopWidth-based width.

Depth δ from the PZF power-law fit; width w0 = median hydroTable TopWidth at
the lowest wet stages.  Known weakness: in flat coastal terrain the lowest
HAND increment spans floodplain, so w0 is often far wider than the channel and
the wedge over-adds conveyance (n pins at N_MAX at ~35% of anchors — see
s05e_evaluate_methods.py for the study-area comparison).

Run:
    .venv\\Scripts\\python.exe scripts/s05a_calibrate_A_wedge_topwidth.py
"""
from s05_ncalib_core import run_batch

if __name__ == "__main__":
    run_batch("A")
