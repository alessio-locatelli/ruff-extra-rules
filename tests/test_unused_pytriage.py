from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

import pytest

from pre_commit_hooks.ast_checks._base import (
    ASTCheck,
    BaseCheck,
    CheckResult,
    CheckUnavailableError,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
)
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks._orchestrator import CheckOrchestrator, load_checks
from pre_commit_hooks.ast_checks.excessive_blank_lines import ExcessiveBlankLinesCheck
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

    monkeypatch.setattr(MeaninglessVarsCheck, "_check", fail_if_called)
    second = CheckOrchestrator(
        checks=load_checks(select={"meaningless-vars", "unused-pytriage"}, check_args=check_args), cache_dir=cache_dir
    )
    assert second.process_files([str(filepath)]) == {}


def test_unused_pytriage_supports_checks_with_a_default_tracking_hook(tmp_path: Path) -> None:
    class PlainCheck(BaseCheck):
        @property
        def check_id(self) -> str:
            return "plain-check"

        @property
        def error_code(self) -> str:
            return "TR99"

        @property
        def cacheable(self) -> bool:
            return False

        def get_prefilter_pattern(self) -> list[str]:
            return ["# pytriage:"]

        def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> CheckResult:
            return CheckResult([], [SuppressionUsage("plain-check", "TR99", 2)])

    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n# pytriage: TR99\n")

    assert (
        CheckOrchestrator(checks=[cast("ASTCheck", PlainCheck()), UnusedPytriageCheck()]).process_files([str(filepath)])
        == {}
    )


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


def test_unused_pytriage_refresh_records_an_audit_failure_after_fix(tmp_path: Path) -> None:
    class FailingRefreshAudit(UnusedPytriageCheck):
        __slots__ = ("calls",)

        def __init__(self) -> None:
            self.calls = 0

        def check_with_suppression_usage(
            self, source: str, usages: tuple[SuppressionUsage, ...], active_error_codes: frozenset[str]
        ) -> CheckResult:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("refresh audit failed")
            return super().check_with_suppression_usage(source, usages, active_error_codes)

    filepath = tmp_path / "module.py"
    filepath.write_text('"""Doc."""  # pytriage: TR99\n\n\n\ndef example() -> str:\n    return "hi"\n')
    audit = FailingRefreshAudit()
    orchestrator = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck(), audit], fix_mode=True)

    orchestrator.process_files([str(filepath)])

    assert audit.calls == 2
    assert orchestrator.rule_failures == [(str(filepath), "unused-pytriage")]


def test_unused_pytriage_rerun_retains_always_rerun_suppression_context(tmp_path: Path) -> None:
    class CacheableCheck(BaseCheck):
        @property
        def check_id(self) -> str:
            return "cacheable-check"

        @property
        def error_code(self) -> str:
            return "TR97"

        def get_prefilter_pattern(self) -> list[str] | None:
            return None

        def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> CheckResult:
            return CheckResult()

    class AlwaysFixingCheck(BaseCheck):
        __slots__ = ()

        @property
        def check_id(self) -> str:
            return "always-fixing-check"

        @property
        def error_code(self) -> str:
            return "TR98"

        @property
        def cacheable(self) -> bool:
            return False

        def get_prefilter_pattern(self) -> list[str] | None:
            return None

        def check(self, _filepath: Path, _tree: ast.Module, source: str) -> CheckResult:
            if "# fixed\n" in source:
                return CheckResult()
            return CheckResult([Violation(self.check_id, self.error_code, 1, 0, "needs fixing", fixable=True)])

        def check_with_suppression_tracking(self, filepath: Path, tree: ast.Module, source: str) -> CheckResult:
            return CheckResult(
                self.check(filepath, tree, source),
                [SuppressionUsage(self.check_id, self.error_code, 1)],
            )

        def fix(
            self, filepath: Path, violations: list[Violation], source: str, _tree: ast.Module, _encoding: str = "utf-8"
        ) -> FixResult:
            filepath.write_text(source + "# fixed\n")
            return FixResult.for_violations(violations, FixOutcome.APPLIED)

    class RecordingAudit(UnusedPytriageCheck):
        __slots__ = ("calls",)

        def __init__(self) -> None:
            self.calls: list[tuple[tuple[SuppressionUsage, ...], frozenset[str]]] = []

        def check_with_suppression_usage(
            self, _source: str, usages: tuple[SuppressionUsage, ...], active_error_codes: frozenset[str]
        ) -> CheckResult:
            self.calls.append((usages, active_error_codes))
            return CheckResult([Violation("unused-pytriage", "TR8", 1, 0, "stale audit", fixable=False)])

    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1  # pytriage: TR99\n")
    cache_dir = tmp_path / "cache"
    cacheable_check = cast("ASTCheck", CacheableCheck())
    CheckOrchestrator(checks=[cacheable_check], cache_dir=cache_dir).process_files([str(filepath)])
    audit = RecordingAudit()

    violations = CheckOrchestrator(
        checks=[cacheable_check, AlwaysFixingCheck(), audit], cache_dir=cache_dir, fix_mode=True
    ).process_files([str(filepath)])

    assert audit.calls[-1] == ((SuppressionUsage("always-fixing-check", "TR98", 1),), frozenset({"TR97", "TR98"}))
    violations_for_file = violations[str(filepath)]
    assert [violation.check_id for violation in violations_for_file].count("unused-pytriage") == 1
    assert [violation.check_id for violation in violations_for_file].count("always-fixing-check") == 1


def test_unused_pytriage_rerun_discards_stale_cached_usage_after_always_fix(
    tmp_path: Path,
) -> None:
    class CacheableCheck(BaseCheck):
        @property
        def check_id(self) -> str:
            return "cacheable-check"

        @property
        def error_code(self) -> str:
            return "TR97"

        def get_prefilter_pattern(self) -> list[str] | None:
            return None

        def check(self, _filepath: Path, _tree: ast.Module, source: str) -> CheckResult:
            if "# fixed" in source:
                return CheckResult()
            return CheckResult(
                [Violation(self.check_id, self.error_code, 2, 0, "needs fixing", fixable=False)],
                [SuppressionUsage(self.check_id, self.error_code, 1)],
            )

    class AlwaysFixingCheck(BaseCheck):
        __slots__ = ()

        @property
        def check_id(self) -> str:
            return "always-fixing-check"

        @property
        def error_code(self) -> str:
            return "TR98"

        @property
        def cacheable(self) -> bool:
            return False

        def get_prefilter_pattern(self) -> list[str] | None:
            return None

        def check(self, _filepath: Path, _tree: ast.Module, source: str) -> CheckResult:
            if "# fixed" in source:
                return CheckResult()
            return CheckResult([Violation(self.check_id, self.error_code, 2, 0, "needs fixing", fixable=True)])

        def fix(
            self, filepath: Path, violations: list[Violation], source: str, _tree: ast.Module, _encoding: str = "utf-8"
        ) -> FixResult:
            filepath.write_text(source.replace("bad = 1", "good = 1") + "# fixed\n")
            return FixResult.for_violations(violations, FixOutcome.APPLIED)

    class RecordingAudit(UnusedPytriageCheck):
        __slots__ = ("calls",)

        def __init__(self) -> None:
            self.calls: list[tuple[tuple[SuppressionUsage, ...], frozenset[str]]] = []

        def check_with_suppression_usage(
            self, _source: str, usages: tuple[SuppressionUsage, ...], active_error_codes: frozenset[str]
        ) -> CheckResult:
            self.calls.append((usages, active_error_codes))
            return CheckResult(
                [] if usages else [Violation(self.check_id, self.error_code, 1, 0, "unused", fixable=False)]
            )

    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1  # pytriage: TR97\nbad = 1\n")
    cache_dir = tmp_path / "cache"
    cacheable_check = cast("ASTCheck", CacheableCheck())
    CheckOrchestrator(checks=[cacheable_check], cache_dir=cache_dir).process_files([str(filepath)])
    audit = RecordingAudit()

    violations = CheckOrchestrator(
        checks=[cacheable_check, AlwaysFixingCheck(), audit], cache_dir=cache_dir, fix_mode=True
    ).process_files([str(filepath)])

    assert audit.calls[-1] == ((), frozenset({"TR97", "TR98"}))
    assert [violation.check_id for violation in violations[str(filepath)]] == [
        "always-fixing-check",
        "cacheable-check",
        "unused-pytriage",
    ]
    assert "# pytriage: TR97" in filepath.read_text()


def test_unused_pytriage_refresh_handles_a_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    orchestrator = CheckOrchestrator(checks=[])
    monkeypatch.setattr(CheckOrchestrator, "_parsed_source", lambda _self, _filepath: None)
    violations_result = CheckResult([Violation("test", "TR99", 1, 0, "message", fixable=False)])

    orchestrator._refresh_unused_pytriage(filepath, [], [UnusedPytriageCheck()], violations_result)

    assert violations_result == [Violation("test", "TR99", 1, 0, "message", fixable=False)]
    assert orchestrator.rule_failures == []


def test_unused_pytriage_refresh_skips_an_unavailable_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    orchestrator = CheckOrchestrator(checks=[])
    orchestrator._unavailable_check_ids.add("unavailable")

    class UnavailableCheck:
        check_id = "unavailable"
        error_code = "TR99"

    monkeypatch.setattr(CheckOrchestrator, "_parsed_source", lambda _self, _filepath: ("x = 1\n", ast.parse("x = 1\n")))
    violations_result = CheckResult([Violation("test", "TR99", 1, 0, "message", fixable=False)])
    orchestrator._refresh_unused_pytriage(
        filepath, [cast("ASTCheck", UnavailableCheck())], [UnusedPytriageCheck()], violations_result
    )

    assert violations_result == [Violation("test", "TR99", 1, 0, "message", fixable=False)]
    assert orchestrator.rule_failures == []


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


def test_unused_pytriage_refresh_records_an_unavailable_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    orchestrator = CheckOrchestrator(checks=[])

    class UnavailableCheck:
        check_id = "unavailable-check"
        error_code = "TR99"

        @staticmethod
        def check_with_suppression_tracking(_filepath: Path, _tree: object, _source: str) -> CheckResult:
            raise CheckUnavailableError("refresh unavailable")

    monkeypatch.setattr(CheckOrchestrator, "_parsed_source", lambda _self, _filepath: ("x = 1\n", ast.parse("x = 1\n")))
    orchestrator._refresh_unused_pytriage(
        filepath, [cast("ASTCheck", UnavailableCheck())], [UnusedPytriageCheck()], CheckResult()
    )

    assert orchestrator.rule_failures == []
    assert orchestrator.unavailable_checks == [("unavailable-check", "refresh unavailable")]


def test_unused_pytriage_fix_declines() -> None:
    violation = Violation("unused-pytriage", "TR8", 1, 0, "unused", fixable=False)

    fix_result = UnusedPytriageCheck().fix(Path("test.py"), [violation], "x = 1\n", ast.parse("x = 1\n"))

    assert fix_result.outcomes == (FixOutcome.DECLINED,)


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        ("def get_total(numbers):  # pytriage: TR4\n    return sum(numbers)\n", [1]),
        (
            (
                "def get_total(numbers):  # pytriage: TR4\n"
                "    return sum(numbers)\n"
                "def get_max(numbers):  # pytriage: TR4\n"
                "    return max(numbers)\n"
            ),
            [1, 3],
        ),
        (
            (
                "def get_total(numbers):  # pytriage: TR4\n"
                "    return sum(numbers)\n"
                "# fmt: off\n"
                "def get_max(numbers):\n"
                "    return max(numbers)\n"
                "# fmt: on\n"
            ),
            [1],
        ),
    ],
    ids=["single", "multiple", "format-suppressed"],
)
def test_validate_function_name_records_pytriage_usage(source: str, expected_lines: list[int]) -> None:
    check_result = ValidateFunctionNameCheck().check_with_suppression_tracking(
        Path("test.py"), ast.parse(source), source
    )

    assert check_result == []
    assert [(usage.check_id, usage.error_code, usage.line) for usage in check_result.suppression_usages] == [
        ("validate-function-name", "TR4", line) for line in expected_lines
    ]


def test_validate_function_name_tracks_a_suppression_alongside_an_unsuppressed_suggestion() -> None:
    source = (
        "def get_total(numbers):  # pytriage: TR4\n"
        "    return sum(numbers)\n"
        "def get_max(numbers):\n"
        "    return max(numbers)\n"
    )

    check_result = ValidateFunctionNameCheck().check_with_suppression_tracking(
        Path("test.py"), ast.parse(source), source
    )

    assert [(usage.check_id, usage.error_code, usage.line) for usage in check_result.suppression_usages] == [
        ("validate-function-name", "TR4", 1)
    ]
    assert [violation.error_code for violation in check_result] == ["TR4"]
