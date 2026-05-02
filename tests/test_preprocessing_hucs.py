# importing the fimbox preprocessing module to test HUCChecker
import fimbox

checker = fimbox.HUCChecker()


def test_huc_checker():
    # Single HUC Query
    r = checker.check_any("03020202", strict=False)
    print(f"Total: {r.n_total}, Found: {r.n_found}, Missing: {r.n_missing}")
    print("missing:", r.missing_hucs)

    # List of HUCs Query
    r = checker.check_any(["01010001", "99999999"], strict=False)
    print(f"Total: {r.n_total}, Found: {r.n_found}, Missing: {r.n_missing}")
    print("missing:", r.missing_hucs)
