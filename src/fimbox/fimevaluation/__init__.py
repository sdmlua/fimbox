"""
Author: Supath Dhital
Date Created: July 2026

FIM evaluation subpackage.

Closes the fimbox loop: after :mod:`fimbox.fimgeneration` produces a flood
map, this subpackage finds the matching ground truth in the FIMbench
benchmark database (https://github.com/sdmlua/fimbench) and evaluates the
map against it with the FIMeval framework
(https://github.com/sdmlua/fimeval) — contingency maps, CSI / POD / FAR /
F1 / accuracy metrics, and optional building-level agreement.

Both ``fimbench`` and ``fimeval`` install with fimbox; they are imported
lazily inside the functions so ``import fimbox`` stays light.

Public surface
--------------
BenchmarkQuery          class: query/download benchmark FIMs from FIMbench
BenchmarkQueryResult    matches, records, and downloaded asset paths
queryBenchmarkFIM       functional wrapper around BenchmarkQuery
FIMEvaluator            class: stage a case + run FIMeval end to end
EvaluationResult        case/output dirs + metrics CSVs as one DataFrame
evaluateFIM             functional wrapper around FIMEvaluator
latest_fim_extent       newest flood-extent raster in <aoi>/fim-outputs/
"""

from __future__ import annotations

from .benchmark_query import (
    BenchmarkQuery,
    BenchmarkQueryResult,
    latest_fim_extent,
    queryBenchmarkFIM,
)
from .evaluate import EvaluationResult, FIMEvaluator, evaluateFIM

__all__ = [
    "BenchmarkQuery",
    "BenchmarkQueryResult",
    "queryBenchmarkFIM",
    "FIMEvaluator",
    "EvaluationResult",
    "evaluateFIM",
    "latest_fim_extent",
]
