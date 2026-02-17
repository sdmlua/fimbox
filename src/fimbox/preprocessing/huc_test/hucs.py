"""
Author: Supath Dhital (sdhital@crimson.ua.edu)
Date updated: Jan 2026

Description: Module to validate Hydrologic Unit Codes (HUCs) against an acceptable list,
if user is interested into the HUC level hand processing and FIM generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Set, List, Optional, Union, Sequence
import argparse
import csv
from importlib import resources as importlib_resources


class HUCValidationError(KeyError):
    """Raised when input HUCs are invalid or not in the acceptable list."""


@dataclass(frozen=True)
class HUCCheckResult:
    input_hucs: Set[str]
    accepted_hucs_count: int
    missing_hucs: Set[str]
    found_hucs: Set[str]

    @property
    def n_total(self) -> int:
        return len(self.input_hucs)

    @property
    def n_found(self) -> int:
        return len(self.found_hucs)

    @property
    def n_missing(self) -> int:
        return len(self.missing_hucs)


HUCInput = Union[str, Path, Sequence[str]]


def _packaged_lst_files() -> List[Path]:
    base = importlib_resources.files("fimbox").joinpath("config", "huc_lists")
    if not base.is_dir():
        raise FileNotFoundError("fimbox/config/huc_lists not found inside package.")
    files = [Path(p) for p in base.iterdir() if p.name.lower().endswith(".lst")]
    if not files:
        raise FileNotFoundError(
            "No packaged *.lst files found at fimbox/config/huc_lists/."
        )
    return files


class HUCChecker:
    """
    Validates HUCs against acceptable list(s) packaged in fimbox/config/huc_lists/*.lst.

    Input can be:
    - single HUC string
    - list/tuple of HUC strings
    - file path: .lst/.txt (line-delimited) or .csv (first column; header allowed)
    """

    def __init__(self):
        self._accepted_hucs: Optional[Set[str]] = None

    def load_acceptable_hucs(self) -> Set[str]:
        if self._accepted_hucs is not None:
            return self._accepted_hucs

        accepted: Set[str] = set()
        for fpath in _packaged_lst_files():
            with fpath.open("r", encoding="utf-8") as f:
                for line in f:
                    v = self.clean_huc_value(line)
                    if v:
                        accepted.add(v)

        if not accepted:
            raise ValueError(
                "Packaged HUC list files were found but contained no usable HUCs."
            )

        self._accepted_hucs = accepted
        return accepted

    def check_any(self, hucs: HUCInput, strict: bool = False) -> HUCCheckResult:
        input_hucs = self._coerce_input(hucs)
        if not input_hucs:
            raise ValueError("No HUCs provided.")

        bad = next((h for h in input_hucs if not h.isnumeric()), None)
        if bad:
            msg = (
                f"Huc value of {bad} does not appear to be a number. "
                "It could be an incorrect value but also could be that the huc list "
                "(if you used one) is incorrect or is not unix encoded."
            )
            raise HUCValidationError(msg)

        accepted = self.load_acceptable_hucs()
        missing = input_hucs - accepted
        found = input_hucs - missing

        if strict and missing:
            miss_preview = ", ".join(sorted(missing)[:10])
            raise HUCValidationError(
                f"{len(missing)} HUC(s) not found in the acceptable HUC list. Examples: {miss_preview}"
            )

        return HUCCheckResult(
            input_hucs=input_hucs,
            accepted_hucs_count=len(accepted),
            missing_hucs=missing,
            found_hucs=found,
        )

    def count_any(self, hucs: HUCInput, strict: bool = True) -> int:
        return self.check_any(hucs, strict=strict).n_total

    def _coerce_input(self, hucs: HUCInput) -> Set[str]:
        if isinstance(hucs, (str, Path)):
            v = self.clean_huc_value(str(hucs))
            p = Path(v)
            return self._read_file(p) if p.is_file() else ({v} if v else set())

        out = {self.clean_huc_value(x) for x in hucs}
        out.discard("")
        return out

    def _read_file(self, path: Path) -> Set[str]:
        return (
            self._read_csv_first_col(path)
            if path.suffix.lower() == ".csv"
            else self._read_lines(path)
        )

    def _read_lines(self, path: Path) -> Set[str]:
        with path.open("r", encoding="utf-8") as f:
            out = {self.clean_huc_value(line) for line in f}
        out.discard("")
        return out

    def _read_csv_first_col(self, path: Path) -> Set[str]:
        out: Set[str] = set()
        with path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if not row:
                    continue
                v = self.clean_huc_value(row[0])
                if v and v.isnumeric():  # header like "HUC8" will be skipped
                    out.add(v)
        return out

    @staticmethod
    def clean_huc_value(huc: str) -> str:
        return huc.strip().replace('"', "").replace("'", "")


def hucinfo(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Checks input HUCs for availability within inputs"
    )
    parser.add_argument(
        "-u",
        "--hucs",
        required=True,
        nargs="+",
        help="Single HUC, list of HUCs, or a file path (.lst/.txt/.csv) containing HUCs",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error if any HUCs are missing (default prints summary).",
    )
    parser.add_argument(
        "--print-missing",
        action="store_true",
        help="Print missing HUCs (comma-separated) after summary.",
    )
    args = parser.parse_args(argv)

    checker = HUCChecker()

    inp: Union[str, List[str]]
    if len(args.hucs) == 1:
        inp = checker.clean_huc_value(args.hucs[0])
    else:
        inp = [checker.clean_huc_value(x) for x in args.hucs]

    res = checker.check_any(inp, strict=args.strict)

    print(f"total={res.n_total} found={res.n_found} missing={res.n_missing}")
    if args.print_missing and res.missing_hucs:
        print("missing:", ", ".join(sorted(res.missing_hucs)))

    return 0 if (not args.strict or res.n_missing == 0) else 2


if __name__ == "__main__":
    raise SystemExit(hucinfo())
