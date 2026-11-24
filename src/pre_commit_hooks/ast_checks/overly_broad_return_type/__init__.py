from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pre_commit_hooks.ast_checks._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
    byte_col_to_char_col,
    find_ignored_lines_and_pytriage_comments,
    ignore_pattern_for,
    record_suppression_usage_if_ignored,
    split_lines_like_ast,
)
from pre_commit_hooks.ast_checks._options import EnumOption

from .analysis import Finding, OverlyBroadReturnTypeLevel, find_findings
from .catalog import PREFILTER_PATTERNS

if TYPE_CHECKING:
    import ast
    from pathlib import Path

    from pre_commit_hooks.ast_checks._options import CheckOption

CHECK_ID = "overly-broad-return-type"
ERROR_CODE = "TR13"
IGNORE_PATTERN = ignore_pattern_for(ERROR_CODE)


def _message(finding: Finding) -> str:
    advice = (
        f"use `{finding.suggestion}`" if finding.suggestion is not None else f"use a `{finding.concrete}` annotation"
    )
    return (
        f"Return type `{finding.annotation}` is broader than `{finding.concrete}`, "
        f"the only type this function returns{' besides `None`' if finding.returns_none else ''}; {advice}. "
        f"Or add '# pytriage: {ERROR_CODE}' to suppress."
    )


class OverlyBroadReturnTypeCheck(BaseCheck):
    __slots__ = ("_level",)

    OPTIONS: ClassVar[tuple[CheckOption, ...]] = (
        EnumOption(
            name="level",
            values=OverlyBroadReturnTypeLevel,
            default=OverlyBroadReturnTypeLevel.CONSERVATIVE,
            help=(
                "Which functions overly-broad-return-type (TR13) checks. "
                "'conservative' (default) checks functions and methods that no subclass can override; "
                "'aggressive' also checks other methods that neither override nor are overridden in the same file."
            ),
        ),
    )

    def __init__(self, level: OverlyBroadReturnTypeLevel = OverlyBroadReturnTypeLevel.CONSERVATIVE) -> None:
        self._level = level

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        return list(PREFILTER_PATTERNS)

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        findings = find_findings(tree, self._level)
        if not findings:
            return CheckResult()
        ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(source, IGNORE_PATTERN)
        lines = split_lines_like_ast(source)
        violations: list[Violation] = []
        suppression_usages: list[SuppressionUsage] = []
        for finding in findings:
            if record_suppression_usage_if_ignored(
                suppression_usages,
                comments,
                ignored_lines=ignored_lines,
                format_suppressed=format_suppressed,
                check_id=CHECK_ID,
                error_code=ERROR_CODE,
                candidate_lines=finding.candidate_lines,
            ):
                continue
            violations.append(
                Violation(
                    check_id=CHECK_ID,
                    error_code=ERROR_CODE,
                    line=finding.node.lineno,
                    col=byte_col_to_char_col(lines[finding.node.lineno - 1], finding.node.col_offset),
                    message=_message(finding),
                    fixable=False,
                )
            )
        return CheckResult(violations, suppression_usages)

    def fix(
        self,
        _filepath: Path,
        violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.DECLINED)
