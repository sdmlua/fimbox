from .preprocessing.calculate_branch import (
    BranchDerivation,
    BranchDerivationResult,
    BranchZero,
    discover_area_inputs,
)

__all__ = [
    "BranchDerivation",
    "BranchDerivationResult",
    "BranchZero",
    "discover_area_inputs",
]


try:
    from .preprocessing.preprocess_area import getAllInputData, preprocess_nld_lines

    __all__.extend(["getAllInputData", "preprocess_nld_lines"])
except ModuleNotFoundError:
    pass

try:
    from .preprocessing.huc_test.hucs import (
        HUCChecker,
        HUCValidationError,
        HUCCheckResult,
    )

    __all__.extend(["HUCChecker", "HUCValidationError", "HUCCheckResult"])
except ModuleNotFoundError:
    pass

try:
    from .preprocessing.download_data.utils import HUC8Finder, getHUC8Info
    from .preprocessing.download_data.dem_process import DEMProcessor
    from .preprocessing.download_data.nhdplus import (
        getNHDPlusData,
        NWMFlowlinesDownloader,
        NWMCatchmentsDownloader,
        NWMLakesDownloader,
    )
    from .preprocessing.download_data.area_masks import (
        DownloadDEMDomain,
        DownloadLandSea,
    )
    from .preprocessing.download_data.nfhl_data import DownloadFEMANFHL
    from .preprocessing.download_data.nld_data import DownloadNLD
    from .preprocessing.download_data.osm_data import (
        DownloadOSMRoads,
        DownloadOSMBridges,
    )
    from .preprocessing.process_bridgedem import generateBridgeRaster, BridgeDEMDiff

    __all__.extend(
        [
            "HUC8Finder",
            "getHUC8Info",
            "DEMProcessor",
            "DownloadFEMANFHL",
            "getNHDPlusData",
            "NWMFlowlinesDownloader",
            "NWMCatchmentsDownloader",
            "NWMLakesDownloader",
            "DownloadDEMDomain",
            "DownloadLandSea",
            "DownloadNLD",
            "DownloadOSMRoads",
            "DownloadOSMBridges",
            "generateBridgeRaster",
            "BridgeDEMDiff",
        ]
    )
except ModuleNotFoundError:
    pass
