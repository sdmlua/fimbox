from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class FlowdirDEM:
    """
    D8 flow direction from a pit-filled DEM.

    Parameters
    ----------
    dem      : pit-filled DEM input (dem_burned_filled_{id}.tif)
    out_path : D8 flow pointer output (flowdir_d8_burned_filled_{id}.tif)
    wbt_path : WhiteboxTools executable directory; falls back to WBT_PATH env var
    """

    dem: Path
    out_path: Path
    wbt_path: Optional[str] = None

    def __post_init__(self):
        self.dem = Path(self.dem)
        self.out_path = Path(self.out_path)

    def run(self) -> Path:
        log.info("D8 flow direction start: %s", self.dem.name)
        try:
            import whitebox

            wbt = whitebox.WhiteboxTools()
            wbt.verbose = False
            wbt_dir = self.wbt_path or os.environ.get("WBT_PATH")
            if wbt_dir:
                wbt.set_whitebox_dir(wbt_dir)
            wbt.d8_pointer(str(self.dem), str(self.out_path))
            log.info("D8 flow direction written → %s", self.out_path.name)
            return self.out_path
        except Exception:
            log.exception("D8 flow direction FAILED: dem=%s", self.dem)
            raise
