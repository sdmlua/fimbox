"""
Author: Manjila Singh
Date Created: July 2026

Package entry point so ``python -m fimbox.nextgen`` runs the pipeline CLI.

Kept separate from ``pipeline.py`` (and not imported by ``__init__``) so that
``python -m fimbox.nextgen`` does not trip the runpy "found in sys.modules"
double-import warning.
"""

from __future__ import annotations

from .pipeline import _main

if __name__ == "__main__":
    _main()
