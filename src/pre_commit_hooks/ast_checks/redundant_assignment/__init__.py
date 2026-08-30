from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pre_commit_hooks.ast_checks._base import (
    BaseCheck,
    CheckResult,
    FixResult,
    SuppressionUsage,
    Violation,
    byte_col_to_char_col,
    find_ignored_lines_and_classify_comments_and_pytriage,
    find_suppression_usage,
    ignore_pattern_for,
    record_suppression_usage_if_ignored,
)
from pre_commit_hooks.ast_checks._options import EnumOption

from .analysis import VariableTracker, detect_redundancy
from .autofix import RedundantAssignmentFixData, apply_fixes
from .semantic import AggressivenessLevel, should_autofix, should_report_violation

if TYPE_CHECKING:
    from pathlib import Path

    from pre_commit_hooks.ast_checks._options import CheckOption

IGNORE_PATTERN = ignore_pattern_for("TR5")

ERROR_CODE = "TR5"
CHECK_ID = "redundant-assignment"


def format_message(var_name: str, pattern_type: str) -> str:
    messages = {
        "IMMEDIATE_SINGLE_USE": (
            f"Redundant assignment '{var_name}' used only once immediately "
            f"after. Consider inlining the value. Or add "
            f"'# pytriage: {ERROR_CODE}' to suppress."
        ),
        "SINGLE_USE": (
            f"Variable '{var_name}' assigned and used only once. "
            f"Consider inlining the expression. Or add "
            f"'# pytriage: {ERROR_CODE}' to suppress."
        ),
        "LITERAL_IDENTITY": (
            f"Identity assignment '{var_name}' is redundant. "
            f"Consider using literal directly. Or add "
            f"'# pytriage: {ERROR_CODE}' to suppress."
        ),
    }
    return messages.get(
        pattern_type,
        f"Redundant assignment '{var_name}'. Or add '# pytriage: {ERROR_CODE}' to suppress.",
    )


class RedundantAssignmentCheck(BaseCheck):
    __slots__ = ("_level",)

    OPTIONS: ClassVar[tuple[CheckOption, ...]] = (
        EnumOption(
            name="level",
            values=AggressivenessLevel,
            default=AggressivenessLevel.CONSERVATIVE,
            help=(
                "How eagerly redundant-assignment (TR5) reports a "
                "violation. 'conservative' (default) flags only the "
                "clearest, safest-to-inline cases; 'permissive' flags a "
                "broader range. Either way, --fix applies to whatever is "
                "reported and mechanically safe to inline — the level "
                "doesn't narrow autofix separately."
            ),
        ),
    )

    def __init__(self, level: AggressivenessLevel = AggressivenessLevel.CONSERVATIVE) -> None:
        self._level = level

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        return [" = "]

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        (
            ignored_lines,
            comment_only_lines,
            trailing_comment_lines,
            format_suppressed,
            comments,
        ) = find_ignored_lines_and_classify_comments_and_pytriage(source, IGNORE_PATTERN)

        tracker = VariableTracker(source, comment_only_lines, trailing_comment_lines, tree)
        tracker.visit(tree)
        lifecycles = tracker.build_lifecycles()

        assignment_counts: dict[tuple[int, str], int] = {}
        for lifecycle in lifecycles:
            key = (lifecycle.assignment.scope_id, lifecycle.assignment.var_name)
            assignment_counts[key] = assignment_counts.get(key, 0) + 1

        rhs_assignment_names: dict[tuple[int, str], set[str]] = {}
        for lifecycle in lifecycles:
            assignment = lifecycle.assignment
            rhs_key = (assignment.scope_id, ast.dump(assignment.rhs_node, include_attributes=False))
            rhs_assignment_names.setdefault(rhs_key, set()).add(assignment.var_name)

        repeated_rhs_names = {
            (scope_id, var_name)
            for (scope_id, _), names in rhs_assignment_names.items()
            for var_name in names
            if len(names) > 1
        }

        violations: list[Violation] = []
        suppression_usages: list[SuppressionUsage] = []

        for lifecycle in lifecycles:
            key = (lifecycle.assignment.scope_id, lifecycle.assignment.var_name)
            if assignment_counts[key] > 1:
                continue

            if key in repeated_rhs_names:
                continue

            if lifecycle.assignment.is_rebinding_marker:
                continue

            pattern = detect_redundancy(lifecycle)
            if pattern is None:
                continue

            candidate_lines = (lifecycle.assignment.line, *(use.line for use in lifecycle.uses))
            assignment_suppression = find_suppression_usage(
                comments,
                format_suppressed,
                self.check_id,
                self.error_code,
                (lifecycle.assignment.line,),
            )
            if not should_report_violation(
                lifecycle,
                pattern,
                level=self._level,
                allow_inline_suppression=assignment_suppression is not None,
            ):
                continue

            if record_suppression_usage_if_ignored(
                suppression_usages,
                comments,
                ignored_lines=ignored_lines,
                format_suppressed=format_suppressed,
                check_id=self.check_id,
                error_code=self.error_code,
                candidate_lines=candidate_lines,
            ):
                continue

            fixable = should_autofix(lifecycle, source_lines=tracker.source_lines)

            message = format_message(lifecycle.assignment.var_name, pattern.name)

            assert len(lifecycle.uses) == 1
            single_use = lifecycle.uses[0]

            fix_data: RedundantAssignmentFixData = {
                "pattern": pattern.name,
                "assign_line": lifecycle.assignment.line,
                "var_name": lifecycle.assignment.var_name,
                "rhs_source": lifecycle.assignment.rhs_source,
                "use_line": single_use.line,
                "use_col": single_use.col,
                "fstring_field_start_col": single_use.fstring_field_span[0] if single_use.fstring_field_span else None,
                "fstring_field_end_col": single_use.fstring_field_span[1] if single_use.fstring_field_span else None,
            }

            assign_line_text = tracker.source_lines[lifecycle.assignment.line - 1]
            violation = Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=lifecycle.assignment.line,
                col=byte_col_to_char_col(assign_line_text, lifecycle.assignment.col),
                message=message,
                fixable=fixable,
                fix_data=cast("dict[str, Any]", fix_data),
            )
            violations.append(violation)

        return CheckResult(violations, suppression_usages)

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        _tree: ast.Module,
        encoding: str = "utf-8",
    ) -> FixResult:
        return apply_fixes(filepath, violations, source, encoding)
