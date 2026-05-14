# importing the fimbox preprocessing module to test HUCChecker
import logging
import fimbox

log = logging.getLogger(__name__)
checker = fimbox.HUCChecker()


def test_huc_checker():
    # Single HUC Query
    r = checker.check_any("03020202", strict=False)
    log.info(f"total={r.n_total} found={r.n_found} missing={r.n_missing}")
    log.info(f"missing: {r.missing_hucs}")

    # List of HUCs Query
    r = checker.check_any(["01010001", "99999999"], strict=False)
    log.info(f"total={r.n_total} found={r.n_found} missing={r.n_missing}")
    log.info(f"missing: {r.missing_hucs}")
