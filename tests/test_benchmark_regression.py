from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from benchmarks import regression

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

MalformedNestedCase = tuple[object, object, str]


def _baseline() -> regression.Baseline:
    return {
        "benchmarks": {"default": {"median_seconds": 0.1}},
        "minimum_absolute_increase_seconds": 0.05,
        "minimum_regression_ratio": 1.5,
        "version": 1,
    }


def _report(median: float = 0.1) -> regression.BenchmarkReport:
    return {"benchmarks": [{"name": "default", "stats": {"iqr": 0.01, "median": median}}]}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value))


def _run_main(
    monkeypatch: MonkeyPatch,
    temporary_path: Path,
    report: object,
    baseline: object,
) -> int:
    report_path = temporary_path / "report.json"
    baseline_path = temporary_path / "baseline.json"
    _write_json(report_path, report)
    _write_json(baseline_path, baseline)
    monkeypatch.setattr(sys, "argv", ["regression", report_path.as_posix(), baseline_path.as_posix()])
    return regression.main()


def test_find_regressions_requires_a_substantial_stable_slowdown() -> None:
    assert regression.find_regressions(_report(0.2), _baseline()) == [
        regression.Regression(
            baseline_seconds=0.1,
            current_seconds=0.2,
            name="default",
        )
    ]


def test_find_regressions_rejects_different_benchmark_sets() -> None:
    report: regression.BenchmarkReport = {"benchmarks": [{"name": "other", "stats": {"iqr": 0.01, "median": 0.1}}]}

    with pytest.raises(ValueError, match="benchmark names differ"):
        regression.find_regressions(report, _baseline())


def test_number_reports_integer_outside_float_range() -> None:
    with pytest.raises(ValueError, match="malformed benchmark input"):
        regression._number(10**10000, Path("report.json"), "expected a numeric value")


@pytest.mark.parametrize(
    "case",
    [
        ({"benchmarks": [{"name": "default", "stats": {"iqr": 0.01}}]}, _baseline(), "report.json"),
        (_report(), {"benchmarks": {"default": {}}}, "baseline.json"),
    ],
)
def test_main_reports_malformed_nested_entries(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    case: MalformedNestedCase,
) -> None:
    report, baseline, expected = case
    with pytest.raises(SystemExit, match="2"):
        _run_main(monkeypatch, tmp_path, report, baseline)

    assert expected in capsys.readouterr().err


def test_main_reports_invalid_json(capsys: CaptureFixture[str], monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text("{")
    _write_json(baseline_path, _baseline())
    monkeypatch.setattr(sys, "argv", ["regression", report_path.as_posix(), baseline_path.as_posix()])

    with pytest.raises(SystemExit, match="2"):
        regression.main()

    assert str(report_path) in capsys.readouterr().err


def test_main_reports_missing_report(capsys: CaptureFixture[str], monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    missing_report_path = tmp_path / "missing.json"
    baseline_path = tmp_path / "baseline.json"
    _write_json(baseline_path, _baseline())
    monkeypatch.setattr(sys, "argv", ["regression", missing_report_path.as_posix(), baseline_path.as_posix()])

    with pytest.raises(SystemExit, match="2"):
        regression.main()

    assert str(missing_report_path) in capsys.readouterr().err


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("unavailable")])
def test_main_reports_file_access_errors(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    error: OSError,
) -> None:
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    _write_json(report_path, _report())
    _write_json(baseline_path, _baseline())
    monkeypatch.setattr(Path, "read_text", lambda _: (_ for _ in ()).throw(error))
    monkeypatch.setattr(sys, "argv", ["regression", report_path.as_posix(), baseline_path.as_posix()])

    with pytest.raises(SystemExit, match="2"):
        regression.main()

    assert str(report_path) in capsys.readouterr().err


@pytest.mark.parametrize(
    ("report", "baseline"),
    [
        ([], _baseline()),
        ({"benchmarks": {}}, _baseline()),
        ({"benchmarks": [None]}, _baseline()),
        ({"benchmarks": [{"name": 1, "stats": {"iqr": 0.01, "median": 0.1}}]}, _baseline()),
        ({"benchmarks": [{"name": "", "stats": {"iqr": 0.01, "median": 0.1}}]}, _baseline()),
        ({"benchmarks": [{"name": "default", "stats": None}]}, _baseline()),
        ({"benchmarks": [{"name": "default", "stats": {"iqr": True, "median": 0.1}}]}, _baseline()),
        ({"benchmarks": [{"name": "default", "stats": {"iqr": -0.01, "median": 0.1}}]}, _baseline()),
        ({"benchmarks": [{"name": "default", "stats": {"iqr": 0.01, "median": -0.1}}]}, _baseline()),
        (
            {
                "benchmarks": [
                    {"name": "default", "stats": {"iqr": 0.01, "median": 0.1}},
                    {"name": "default", "stats": {"iqr": 0.01, "median": 0.1}},
                ]
            },
            _baseline(),
        ),
        (_report(), []),
        (_report(), {"benchmarks": {}, "version": True}),
        (_report(), {"benchmarks": {"": {"median_seconds": 0.1}}, "version": 1}),
        (_report(), {"benchmarks": {"default": None}, "version": 1}),
        (_report(), {"benchmarks": {"default": {"median_seconds": True}}, "version": 1}),
        (
            _report(),
            {
                "benchmarks": {"default": {"median_seconds": -0.1}},
                "minimum_absolute_increase_seconds": 0.05,
                "minimum_regression_ratio": 1.5,
                "version": 1,
            },
        ),
        (
            _report(),
            {
                "benchmarks": {"default": {"median_seconds": 0.1}},
                "minimum_absolute_increase_seconds": -0.01,
                "minimum_regression_ratio": 1.5,
                "version": 1,
            },
        ),
        (
            _report(),
            {
                "benchmarks": {"default": {"median_seconds": 0.1}},
                "minimum_absolute_increase_seconds": 0.05,
                "minimum_regression_ratio": -0.1,
                "version": 1,
            },
        ),
    ],
)
def test_main_reports_all_other_malformed_input(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    report: object,
    baseline: object,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        _run_main(monkeypatch, tmp_path, report, baseline)

    assert "malformed benchmark input" in capsys.readouterr().err


@pytest.mark.parametrize("median", [0.1, 0.2])
def test_main_writes_a_summary(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    median: float,
) -> None:
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    assert _run_main(monkeypatch, tmp_path, _report(median), _baseline()) == 0

    summary = summary_path.read_text()
    assert "Performance benchmark review" in summary
    if median == 0.1:
        assert "No regression candidates" in summary
    else:
        assert "default" in summary


def test_main_does_not_write_a_summary_without_github_environment(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert _run_main(monkeypatch, tmp_path, _report(), _baseline()) == 0


def test_main_reports_summary_write_failures(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))

    with pytest.raises(SystemExit, match="2"):
        _run_main(monkeypatch, tmp_path, _report(), _baseline())

    assert str(tmp_path) in capsys.readouterr().err
