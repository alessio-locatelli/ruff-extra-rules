from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


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


def _malformed(path: Path, expected: str) -> ValueError:
    return ValueError(f"{path}: malformed benchmark input; {expected}. Regenerate the file and try again.")


def _read_json(path: Path) -> object:
    try:
        source = path.read_text()
    except FileNotFoundError as error:
        message = f"{path}: file is missing; create it or pass the correct path."
        raise ValueError(message) from error
    except PermissionError as error:
        message = f"{path}: permission denied; grant read access and try again."
        raise ValueError(message) from error
    except OSError as error:
        message = f"{path}: could not access the file; check the path and filesystem access."
        raise ValueError(message) from error
    try:
        return json.loads(source)
    except json.JSONDecodeError as error:
        message = f"{path}: invalid JSON; regenerate the benchmark file and try again."
        raise ValueError(message) from error


def _number(value: object, path: Path, expected: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _malformed(path, expected)
    try:
        number = float(value)
    except OverflowError as error:
        raise _malformed(path, expected) from error
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise _malformed(path, expected)
    return number


def _load_report(path: Path) -> BenchmarkReport:
    report = _read_json(path)
    if not isinstance(report, dict) or not isinstance(entries := report.get("benchmarks"), list):
        raise _malformed(path, "expected a pytest-benchmark report with a benchmarks list")
    benchmark_entries: list[BenchmarkReportEntry] = []
    names: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(name := entry.get("name"), str) or not name:
            raise _malformed(path, f"benchmark entry {index} needs a non-empty name")
        if name in names:
            raise _malformed(path, f"benchmark entry {index} duplicates name {name}")
        if not isinstance(stats := entry.get("stats"), dict):
            raise _malformed(path, f"benchmark entry {index} needs a stats object")
        names.add(name)
        benchmark_entries.append(
            {
                "name": name,
                "stats": {
                    "iqr": _number(
                        stats.get("iqr"),
                        path,
                        f"benchmark entry {index} needs numeric stats.iqr",
                        minimum=0,
                    ),
                    "median": _number(
                        stats.get("median"),
                        path,
                        f"benchmark entry {index} needs numeric stats.median",
                        minimum=0,
                    ),
                },
            }
        )
    return {"benchmarks": benchmark_entries}


def _load_baseline(path: Path) -> Baseline:
    baseline = _read_json(path)
    if not isinstance(baseline, dict) or not isinstance(entries := baseline.get("benchmarks"), dict):
        raise _malformed(path, "expected a baseline with a benchmarks object")
    version = baseline.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _malformed(path, "expected an integer version")
    baseline_entries: dict[str, BaselineEntry] = {}
    for name, entry in entries.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            raise _malformed(path, "each benchmark needs a non-empty name and an object value")
        baseline_entries[name] = {
            "median_seconds": _number(
                entry.get("median_seconds"),
                path,
                f"benchmark {name} needs numeric median_seconds",
                minimum=0,
            )
        }
    return {
        "benchmarks": baseline_entries,
        "minimum_absolute_increase_seconds": _number(
            baseline.get("minimum_absolute_increase_seconds"),
            path,
            "expected numeric minimum_absolute_increase_seconds",
            minimum=0,
        ),
        "minimum_regression_ratio": _number(
            baseline.get("minimum_regression_ratio"),
            path,
            "expected numeric minimum_regression_ratio",
            minimum=0,
        ),
        "version": version,
    }


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
    try:
        with Path(summary_path).open("a") as summary_file:
            summary_file.write("\n".join(lines) + "\n")
    except OSError as error:
        message = f"{summary_path}: could not write the benchmark summary; set GITHUB_STEP_SUMMARY to a writable file."
        raise OSError(message) from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("baseline", type=Path)
    arguments = parser.parse_args()
    try:
        report = _load_report(arguments.report)
        baseline = _load_baseline(arguments.baseline)
        regressions = find_regressions(report, baseline)
        _write_summary(regressions)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    for regression in regressions:
        print(
            f"::warning title=Performance regression candidate::{regression.name}: "
            f"{regression.baseline_seconds:.3f}s to {regression.current_seconds:.3f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
