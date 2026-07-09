"""
Method B1 — datum shift (no geometry modification).

The SRC is re-referenced to the rating curve's origin: anchors and the applied
discharge are evaluated at HAND stage h + δ, where δ is the PZF datum gap.
Zero geometry parameters, but the SRC borrows floodplain-level widths for the
submerged channel, which over-adds conveyance even more than method A
(n pins at N_MAX at ~49% of anchors — see s05e_evaluate_methods.py).

Run:
    .venv\\Scripts\\python.exe scripts/s05b_calibrate_B1_datum_shift.py
"""
from s05_ncalib_core import run_batch

if __name__ == "__main__":
    run_batch("B1")
