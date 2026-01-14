### HUC Validation

This module validates Hydrologic Unit Codes (HUCs) against the acceptable HUC lists packaged in:

`fimbox/config/huc_lists/*.lst`

**After installing the FIMbox**,

**Python Usage**
```bash
import fimbox

checker = fimbox.HUCChecker()

# Single HUC
r = checker.check_any("03020201", strict=False)
print(r.n_total, r.n_found, r.n_missing)
print("missing:", sorted(r.missing_hucs))

# List of HUCs
r = checker.check_any(["01010001", "99999999"], strict=False)
print("missing:", sorted(r.missing_hucs))

# File input; HUC8; header allowed)
r = checker.check_any("my_hucs.csv", strict=False)
print("missing:", sorted(r.missing_hucs))

# Strict mode: raises HUCValidationError if any are missing
checker.check_any(["01010001", "99999999"], strict=True)

```

**CLI**

```bash
#FOR missing or not printing 
python -m fimbox.preprocessing.hucs -u 03020202 --print-missing

#On multiple list
python -m fimbox.preprocessing.hucs -u 01010001 99999999 --print-missing

#On List
python -m fimbox.preprocessing.hucs -u my_hucs.txt --print-missing
python -m fimbox.preprocessing.hucs -u my_hucs.csv --print-missing
```
