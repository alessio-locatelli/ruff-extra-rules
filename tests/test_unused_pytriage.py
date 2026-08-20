from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

import pytest

from pre_commit_hooks.ast_checks._base import ASTCheck, CheckResult, FixOutcome, FixResult, SuppressionUsage, Violation
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks._orchestrator import CheckOrchestrator, load_checks
from pre_commit_hooks.ast_checks.meaningless_vars import MeaninglessVarsCheck, MeaninglessVarsLevel
from pre_commit_hooks.ast_checks.unused_pytriage import UnusedPytriageCheck
from pre_commit_hooks.ast_checks.validate_function_name import ValidateFunctionNameCheck


def test_unused_pytriage_reports_redundant_known_suppression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("def example():\n    result = 1  # pytriage: TR1\n    return result\n")

    assert (
        main(
            [
                str(filepath),
                "--select",
                "meaningless-vars,unused-pytriage",
            ]
        )
        == 1
    )

    assert "TR8" in capsys.readouterr().err


def test_unused_pytriage_alone_does_not_audit_inactive_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("def example():\n    result = 1  # pytriage: TR1\n    return result\n")

    assert main([str(filepath), "--select", "unused-pytriage"]) == 0
    assert capsys.readouterr().err == ""


def test_unused_pytriage_is_opt_in_by_default() -> None:
    assert "unused-pytriage" not in {check.check_id for check in load_checks()}
    assert "unused-pytriage" in {check.check_id for check in load_checks(select={"unused-pytriage"})}


def test_unused_pytriage_keeps_a_used_suppression_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("def example():\n    result = get_result()  # pytriage: TR1\n    return result\n")

    assert (
        main([str(filepath), "--select", "meaningless-vars,unused-pytriage", "--meaningless-vars-level", "permissive"])
        == 0
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "comment",
    ["# pytriage: TR1,TR1", "# pytriage: TR99", "# pytriage: TR8"],
    ids=["duplicate", "unknown", "self"],
)
def test_unused_pytriage_ignores_non_reportable_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], comment: str
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(f"def example():\n    result = 42  {comment}\n    return result\n")

    assert main([str(filepath), "--select", "meaningless-vars,unused-pytriage"]) == (1 if "TR1" in comment else 0)
    if "TR1" in comment:
        assert "TR1, TR1" in capsys.readouterr().err
    else:
        assert capsys.readouterr().err == ""


def test_unused_pytriage_does_not_audit_format_suppressed_comments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("# fmt: off\ndef example():\n    result = 42  # pytriage: TR1\n    return result\n# fmt: on\n")

    assert main([str(filepath), "--select", "meaningless-vars,unused-pytriage"]) == 0
    assert capsys.readouterr().err == ""


def test_unused_pytriage_reports_at_the_final_location_after_other_fixes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text('"""Doc."""\n\n\n\ndef example():\n    result = 42  # pytriage: TR1\n    return result\n')

    assert (
        main(
            [
                str(filepath),
                "--select",
                "excessive-blank-lines,meaningless-vars,unused-pytriage",
                "--fix",
            ]
        )
        == 1
    )
    assert ":5:18: TR8:" in capsys.readouterr().err


def test_unused_pytriage_uses_cached_suppression_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("def example():\n    result = get_result()  # pytriage: TR1\n    return result\n")
    cache_dir = tmp_path / "cache"
    check_args = {"meaningless-vars": {"level": MeaninglessVarsLevel.PERMISSIVE}}
    checks = load_checks(select={"meaningless-vars", "unused-pytriage"}, check_args=check_args)

    first = CheckOrchestrator(checks=checks, cache_dir=cache_dir)
    assert first.process_files([str(filepath)]) == {}

    def fail_if_called(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("meaningless-vars should be served from its cache")

    monkeypatch.setattr(MeaninglessVarsCheck, "check", fail_if_called)
    second = CheckOrchestrator(
        checks=load_checks(select={"meaningless-vars", "unused-pytriage"}, check_args=check_args), cache_dir=cache_dir
    )
    assert second.process_files([str(filepath)]) == {}


def test_unused_pytriage_supports_checks_with_a_default_tracking_hook(tmp_path: Path) -> None:
    class PlainCheck:
        check_id = "plain-check"
        error_code = "TR99"
        cacheable = False
        tracks_direct_inputs = False

        @staticmethod
        def get_prefilter_pattern() -> list[str]:
            return ["# pytriage:"]

        @staticmethod
        def check(_filepath: Path, _tree: object, _source: str) -> CheckResult:
            return CheckResult([], [SuppressionUsage("plain-check", "TR99", 2)])

        @staticmethod
        def check_with_suppression_tracking(_filepath: Path, _tree: object, _source: str) -> CheckResult:
            return PlainCheck.check(_filepath, _tree, _source)

        @staticmethod
        def fix(_filepath: Path, violations: list[Violation], _source: str, _tree: object) -> FixResult:
            return FixResult.for_violations(violations, FixOutcome.DECLINED)

    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n# pytriage: TR99\n")

    assert (
        CheckOrchestrator(checks=[cast("ASTCheck", PlainCheck()), UnusedPytriageCheck()]).process_files([str(filepath)])
        == {}
    )
    assert PlainCheck.fix(
        filepath, [Violation("plain-check", "TR99", 2, 0, "unused", fixable=False)], "", object()
    ).outcomes == (FixOutcome.DECLINED,)


def test_unused_pytriage_failure_is_recorded(tmp_path: Path) -> None:
    class FailingAudit(UnusedPytriageCheck):
        __slots__ = ()

        def check_with_suppression_usage(
            self, _source: str, _usages: tuple[SuppressionUsage, ...], _active_error_codes: frozenset[str]
        ) -> CheckResult:
            raise RuntimeError("audit failed")

    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1  # pytriage: TR99\n")
    orchestrator = CheckOrchestrator(checks=[FailingAudit()])

    assert orchestrator.process_files([str(filepath)]) == {}
    assert orchestrator.rule_failures == [(str(filepath), "unused-pytriage")]


def test_unused_pytriage_refresh_handles_a_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    orchestrator = CheckOrchestrator(checks=[])
    monkeypatch.setattr(CheckOrchestrator, "_parsed_source", lambda _self, _filepath: None)

    orchestrator._refresh_unused_pytriage(filepath, [], [UnusedPytriageCheck()], CheckResult())


def test_unused_pytriage_refresh_skips_an_unavailable_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    orchestrator = CheckOrchestrator(checks=[])
    orchestrator._unavailable_check_ids.add("unavailable")

    class UnavailableCheck:
        check_id = "unavailable"
        error_code = "TR99"

    monkeypatch.setattr(CheckOrchestrator, "_parsed_source", lambda _self, _filepath: ("x = 1\n", ast.parse("x = 1\n")))
    orchestrator._refresh_unused_pytriage(
        filepath, [cast("ASTCheck", UnavailableCheck())], [UnusedPytriageCheck()], CheckResult()
    )


def test_unused_pytriage_refresh_records_a_check_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    orchestrator = CheckOrchestrator(checks=[])

    class FailingCheck:
        check_id = "failing-check"
        error_code = "TR99"

        @staticmethod
        def check_with_suppression_tracking(_filepath: Path, _tree: object, _source: str) -> CheckResult:
            raise RuntimeError("refresh failed")

    monkeypatch.setattr(CheckOrchestrator, "_parsed_source", lambda _self, _filepath: ("x = 1\n", ast.parse("x = 1\n")))
    orchestrator._refresh_unused_pytriage(
        filepath, [cast("ASTCheck", FailingCheck())], [UnusedPytriageCheck()], CheckResult()
    )

    assert orchestrator.rule_failures == [(str(filepath), "failing-check")]


def test_unused_pytriage_fix_declines() -> None:
    violation = Violation("unused-pytriage", "TR8", 1, 0, "unused", fixable=False)

    fix_result = UnusedPytriageCheck().fix(Path("test.py"), [violation], "x = 1\n", ast.parse("x = 1\n"))

    assert fix_result.outcomes == (FixOutcome.DECLINED,)


def test_validate_function_name_records_a_pytriage_usage() -> None:
    source = "def get_total(numbers):  # pytriage: TR4\n    return sum(numbers)\n"

    check_result = ValidateFunctionNameCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [(usage.check_id, usage.error_code, usage.line) for usage in check_result.suppression_usages] == [
        ("validate-function-name", "TR4", 1)
    ]


def test_validate_function_name_records_each_suppressed_pytriage_usage() -> None:
    source = (
        "def get_total(numbers):  # pytriage: TR4\n"
        "    return sum(numbers)\n"
        "def get_max(numbers):  # pytriage: TR4\n"
        "    return max(numbers)\n"
    )

    check_result = ValidateFunctionNameCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [usage.line for usage in check_result.suppression_usages] == [1, 3]


def test_validate_function_name_ignores_a_format_suppressed_candidate_when_tracking_usage() -> None:
    source = (
        "def get_total(numbers):  # pytriage: TR4\n"
        "    return sum(numbers)\n"
        "# fmt: off\n"
        "def get_max(numbers):\n"
        "    return max(numbers)\n"
        "# fmt: on\n"
    )

    check_result = ValidateFunctionNameCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [usage.line for usage in check_result.suppression_usages] == [1]
