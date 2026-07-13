### HUC Validation
<hr style="border: 1px solid blue;">

**fimbox.preprocessing.huc_test** validates Hydrologic Unit Codes (HUCs) against the acceptable HUC lists packaged in `fimbox/config/huc_lists/*.lst`, so an AOI can be checked before any data download or processing starts.

**Workflow**

Any HUC input (a single code, a list, or a file) is normalized into a set of HUC8 codes. The set is matched against the acceptable HUC lists shipped with the package, and the result reports which codes were found and which are missing. Strict mode turns missing codes into an error, so invalid AOIs fail fast.

<!-- Diagram source: workflows/huc_test.mmd - edit that file and regenerate with `make workflows` (see workflows/README.md) -->
<div align="center">
  <img src="../../../../workflows/svg/huc_test.svg" alt="huc test workflow" />
</div>

**Module contents**

| File | What it contains |
|---|---|
| `hucs.py` | `HUCChecker` (with `check_any` and `count_any`), `HUCCheckResult`, `HUCValidationError`, plus the CLI entry point. |

### Usage
<hr style="border: 1px solid blue;">

**Python**

```python
import fimbox

checker = fimbox.HUCChecker()

# Single HUC
r = checker.check_any(
    "03020201",          #single HUC string, a list of HUCs, or a .lst/.csv file path
    strict=False,        #False = warn on missing HUCs; True = raise HUCValidationError
)
print(r.n_total, r.n_found, r.n_missing)
print("missing:", sorted(r.missing_hucs))

# List of HUCs
r = checker.check_any(["01010001", "99999999"], strict=False)

# File input (HUC8 per line; header allowed)
r = checker.check_any("my_hucs.csv", strict=False)

# Count HUCs while validating (strict by default)
n = checker.count_any(["01010001"], strict=True)
```

**CLI**

```bash
# Single HUC
python -m fimbox.preprocessing.hucs -u 03020202 --print-missing

# Multiple HUCs
python -m fimbox.preprocessing.hucs -u 01010001 99999999 --print-missing

# From a list file
python -m fimbox.preprocessing.hucs -u my_hucs.txt --print-missing
python -m fimbox.preprocessing.hucs -u my_hucs.csv --print-missing
```

**For more usage notes refer to the [tests](../../../../tests/) or [docs](../../../../docs/) for the `fimbox` python package.**
