from __future__ import annotations

from benchmarks.regression import Baseline, BenchmarkReport, Regression, find_regressions


def test_find_regressions_requires_a_substantial_stable_slowdown() -> None:
    baseline: Baseline = {
        "benchmarks": {"default": {"median_seconds": 0.1}},
        "minimum_absolute_increase_seconds": 0.05,
        "minimum_regression_ratio": 1.5,
        "version": 1,
    }
    report: BenchmarkReport = {"benchmarks": [{"name": "default", "stats": {"iqr": 0.01, "median": 0.2}}]}

    assert find_regressions(report, baseline) == [
        Regression(
            baseline_seconds=0.1,
            current_seconds=0.2,
            name="default",
        )
    ]
