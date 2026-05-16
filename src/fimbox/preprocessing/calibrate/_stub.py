"""
Shared helper for not-yet-ported calibration routines.

Each placeholder module in this subpackage uses ``not_yet_ported`` to mark its
entry point. Calling the function raises ``NotImplementedError`` with a
pointer to the inundation-mapping source so the user knows exactly where to
look. This lets the calibration pipeline reference every step by name now
while keeping the port honest about what is and isn't validated.
"""

from __future__ import annotations


class CalibrationNotImplemented(NotImplementedError):
    """Raised when a calibration step has not been ported yet."""


def not_yet_ported(step_name: str, im_source: str) -> None:
    raise CalibrationNotImplemented(
        f"{step_name} has not been ported to fimbox yet.\n"
        f"Reference implementation: inundation-mapping/src/{im_source}\n"
        f"Open an issue or implement it before enabling this toggle in pipeline.run()."
    )


def resolve_aoi_dir(aoi_dir=None, huc_dir=None):
    """Accept either ``aoi_dir`` or ``huc_dir`` and return whichever was passed.
    Raises ``TypeError`` if neither is given, or if both are given with
    different values."""
    if aoi_dir is not None and huc_dir is not None and aoi_dir != huc_dir:
        raise TypeError(
            f"Pass aoi_dir= or huc_dir=, not both with different values "
            f"({aoi_dir!r} vs {huc_dir!r})."
        )
    chosen = aoi_dir if aoi_dir is not None else huc_dir
    if chosen is None:
        raise TypeError("Either aoi_dir= or huc_dir= must be provided.")
    return chosen
