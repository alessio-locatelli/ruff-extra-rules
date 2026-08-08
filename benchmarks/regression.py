from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast


class BenchmarkStats(TypedDict):
    iqr: float
    median: float


class BenchmarkReportEntry(TypedDict):
    name: str
    stats: BenchmarkStats


class BenchmarkReport(TypedDict):
    benchmarks: list[BenchmarkReportEntry]


class BaselineEntry(TypedDict):
    median_seconds: float


class Baseline(TypedDict):
    benchmarks: dict[str, BaselineEntry]
    minimum_absolute_increase_seconds: float
    minimum_regression_ratio: float
    version: int


@dataclass(frozen=True)
class Regression:
    baseline_seconds: float
    current_seconds: float
    name: str


def _load_json(path: Path) -> object:
    return json.loads(path.read_text())


def _load_report(path: Path) -> BenchmarkReport:
    report = _load_json(path)
    if not isinstance(report, dict) or not isinstance(report.get("benchmarks"), list):
        message = f"{path} is not a pytest-benchmark report"
        raise TypeError(message)
    return cast("BenchmarkReport", report)


def _load_baseline(path: Path) -> Baseline:
    baseline = _load_json(path)
    if not isinstance(baseline, dict) or not isinstance(baseline.get("benchmarks"), dict):
        message = f"{path} is not a benchmark baseline"
        raise TypeError(message)
    return cast("Baseline", baseline)


def find_regressions(report: BenchmarkReport, baseline: Baseline) -> list[Regression]:
    report_entries = {entry["name"]: entry["stats"] for entry in report["benchmarks"]}
    baseline_entries = baseline["benchmarks"]
    if report_entries.keys() != baseline_entries.keys():
        missing = sorted(baseline_entries.keys() - report_entries.keys())
        unexpected = sorted(report_entries.keys() - baseline_entries.keys())
        message = f"benchmark names differ; missing={missing}, unexpected={unexpected}"
        raise ValueError(message)

    regressions: list[Regression] = []
    for name, current_stats in report_entries.items():
        baseline_median = baseline_entries[name]["median_seconds"]
        current_median = current_stats["median"]
        increase = current_median - baseline_median
        minimum_increase = max(
            baseline_median * (baseline["minimum_regression_ratio"] - 1),
            baseline["minimum_absolute_increase_seconds"],
            current_stats["iqr"] * 3,
        )
        if increase > minimum_increase:
            regressions.append(
                Regression(
                    baseline_seconds=baseline_median,
                    current_seconds=current_median,
                    name=name,
                )
            )
    return regressions


def _write_summary(regressions: list[Regression]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path is None:
        return
    lines = ["## Performance benchmark review", ""]
    if not regressions:
        lines.append("No regression candidates exceeded the review threshold.")
    else:
        lines.extend(
            [
                "The following results need a local reproduction before release:",
                "",
                "| Benchmark | Baseline median | Current median |",
                "| --- | ---: | ---: |",
            ]
        )
        lines.extend(
            f"| {regression.name} | {regression.baseline_seconds:.3f}s | {regression.current_seconds:.3f}s |"
            for regression in regressions
        )
    with Path(summary_path).open("a") as summary_file:
        summary_file.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("baseline", type=Path)
    arguments = parser.parse_args()
    regressions = find_regressions(_load_report(arguments.report), _load_baseline(arguments.baseline))
    _write_summary(regressions)
    for regression in regressions:
        print(
            f"::warning title=Performance regression candidate::{regression.name}: "
            f"{regression.baseline_seconds:.3f}s to {regression.current_seconds:.3f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
