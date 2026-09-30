from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pre_commit_hooks.ast_checks._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
    find_ignored_lines_and_pytriage_comments,
    ignore_pattern_for,
    record_suppression_usage_if_ignored,
)
from pre_commit_hooks.ast_checks._options import EnumOption, IntOption

from .analysis import find_findings
from .reorder import DefinedFarFromUseLevel

if TYPE_CHECKING:
    import ast
    from pathlib import Path

    from pre_commit_hooks.ast_checks._options import CheckOption

CHECK_ID = "defined-far-from-use"
ERROR_CODE = "TR11"
IGNORE_PATTERN = ignore_pattern_for(ERROR_CODE)
DEFAULT_MAX_DISTANCE = 5


class DefinedFarFromUseCheck(BaseCheck):
    __slots__ = ("_level", "_max_distance")

    OPTIONS: ClassVar[tuple[CheckOption, ...]] = (
        EnumOption(
            name="level",
            values=DefinedFarFromUseLevel,
            default=DefinedFarFromUseLevel.CONSERVATIVE,
            help=(
                "Which assignments defined-far-from-use (TR11) reports. "
                "'conservative' (default) reports only values that are safe to move, such as literals; "
                "'aggressive' also reports calls and other expressions that may have side effects."
            ),
        ),
        IntOption(
            name="max-distance",
            default=DEFAULT_MAX_DISTANCE,
            minimum=1,
            help=(
                "The most statements defined-far-from-use (TR11) allows between an assignment "
                f"and the first use of its variable (default: {DEFAULT_MAX_DISTANCE})."
            ),
        ),
    )

    def __init__(
        self,
        level: DefinedFarFromUseLevel = DefinedFarFromUseLevel.CONSERVATIVE,
        max_distance: int = DEFAULT_MAX_DISTANCE,
    ) -> None:
        self._level = level
        self._max_distance = max_distance

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        return None

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        findings = find_findings(tree, self._level, self._max_distance)
        if not findings:
            return CheckResult()

        ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(source, IGNORE_PATTERN)
        violations: list[Violation] = []
        suppression_usages: list[SuppressionUsage] = []
        for finding in sorted(findings, key=lambda item: (item.target.lineno, item.target.col_offset)):
            line = finding.target.lineno
            if record_suppression_usage_if_ignored(
                suppression_usages,
                comments,
                ignored_lines=ignored_lines,
                format_suppressed=format_suppressed,
                check_id=self.check_id,
                error_code=self.error_code,
                candidate_lines=(line,),
            ):
                continue
            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=line,
                    col=finding.target.col_offset,
                    message=f"{finding.message}. Or add '# pytriage: {ERROR_CODE}' to suppress.",
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
