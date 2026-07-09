"""
Method B3 — bathymetry wedge with hydraulic-continuity width.  ** CHOSEN **

Depth δ from the PZF power-law fit; width solved from Manning's equation so
the excavated rectangular channel carries exactly the observed baseflow:

    w0 = Q0 · n_ch / (δ^(5/3) · √S),   n_ch = 0.05,  capped by TopWidth

Both origin observations (δ from the PZF fit, Q0 from the rating curve) are
honored simultaneously, so the SRC starts at the observed baseflow by
construction.  Study-area evaluation (s05e_evaluate_methods.py): lowest bound
saturation (9.4% at N_MAX / 2.6% at N_MIN), median calibrated n = 0.060, 76%
of n within the 0.02–0.20 literature range, origin ratio 0.99.

Run:
    .venv\\Scripts\\python.exe scripts/s05d_calibrate_B3_wedge_continuity.py
"""
from s05_ncalib_core import run_batch

if __name__ == "__main__":
    run_batch("B3")
