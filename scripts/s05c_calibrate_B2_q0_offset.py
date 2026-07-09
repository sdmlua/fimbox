"""
Method B2 — baseflow offset (no geometry modification).

The observed baseflow at the DEM water-surface elevation (Q0, from the rating
curve) is added to the SRC discharge:  Q(h) = K(h)·√S/n(h) + Q0
(Garousi-Nejad et al. 2019, WRR).  Simple and robust — this is also the
automatic fallback of every other method when the PZF fit fails QC — but it
leaves the submerged-channel conveyance deficit in the geometry, so n absorbs
it (~10% of anchors pin at N_MIN; see s05e_evaluate_methods.py).

Run:
    .venv\\Scripts\\python.exe scripts/s05c_calibrate_B2_q0_offset.py
"""
from s05_ncalib_core import run_batch

if __name__ == "__main__":
    run_batch("B2")
