from __future__ import annotations

from typing import TYPE_CHECKING

from ._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
    find_ignored_lines_and_pytriage_comments,
)

if TYPE_CHECKING:
    import ast
    from pathlib import Path

CHECK_ID = "unused-pytriage"
ERROR_CODE = "TR8"


class UnusedPytriageCheck(BaseCheck):
    __slots__ = ()

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    @property
    def cacheable(self) -> bool:
        return False

    @property
    def default_enabled(self) -> bool:
        return False

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["# pytriage:"]

    def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> CheckResult:
        return CheckResult()

    def check_with_suppression_usage(
        self,
        source: str,
        usages: tuple[SuppressionUsage, ...],
        active_error_codes: frozenset[str],
    ) -> CheckResult:
        _ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(source)
        used = {(usage.error_code, usage.line) for usage in usages if usage.error_code in active_error_codes}
        violations: list[Violation] = []

        for comment in comments:
            if comment.line in format_suppressed:
                continue

            remaining_usage = {(code, comment.line) for code in active_error_codes if (code, comment.line) in used}
            unused_codes: list[str] = []
            for code in comment.codes:
                if code not in active_error_codes or code == ERROR_CODE:
                    continue
                key = (code, comment.line)
                if key in remaining_usage:
                    remaining_usage.remove(key)
                else:
                    unused_codes.append(code)

            if unused_codes:
                listed_codes = ", ".join(unused_codes)
                violations.append(
                    Violation(
                        check_id=CHECK_ID,
                        error_code=ERROR_CODE,
                        line=comment.line,
                        col=comment.col,
                        message=f"Unused '# pytriage' suppression code(s): {listed_codes}.",
                        fixable=False,
                    )
                )

        return CheckResult(violations)

    def fix(
        self,
        _filepath: Path,
        violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.DECLINED)
