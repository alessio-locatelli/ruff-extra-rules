"""Check for redundant variable assignments (TRI005).

TRI005: Detects redundant variable assignments where the variable doesn't add
clarity, transformation, or simplification to the code.

Patterns detected:
1. Assignment + immediate single use: x = "foo"; func(x=x)
2. Single-use variables: x = calc(); return x
3. Literal identity: foo = "foo"

Inline ignore: # pytriage: ignore=TRI005

Examples:
    # ❌ Redundant
    x = "foo"
    func(x=x)

    # ❌ Redundant
    result = get_value()
    return result

    # ✅ Adds clarity (transformative verb)
    formatted_timestamp = format_iso8601(raw_ts)
    return formatted_timestamp

    # ✅ Adds clarity (complex expression)
    user_full_name = f"{user.first_name} {user.last_name}"
    send_email(recipient=user_full_name)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pre_commit_hooks.ast_checks._base import (
    BaseCheck,
    Violation,
    byte_col_to_char_col,
    find_ignored_lines,
    ignore_pattern_for,
)

from .analysis import VariableTracker, detect_redundancy
from .autofix import RedundantAssignmentFixData, apply_fixes
from .semantic import AggressivenessLevel, should_autofix, should_report_violation

if TYPE_CHECKING:
    import argparse
    import ast
    from pathlib import Path

# Format: # pytriage: ignore=TRI005
IGNORE_PATTERN = ignore_pattern_for("TRI005")

ERROR_CODE = "TRI005"
CHECK_ID = "redundant-assignment"


def format_message(var_name: str, pattern_type: str) -> str:
    messages = {
        "IMMEDIATE_SINGLE_USE": (
            f"Redundant assignment '{var_name}' used only once immediately "
            f"after. Consider inlining the value. Or add "
            f"'# pytriage: ignore={ERROR_CODE}' to suppress."
        ),
        "SINGLE_USE": (
            f"Variable '{var_name}' assigned and used only once. "
            f"Consider inlining the expression. Or add "
            f"'# pytriage: ignore={ERROR_CODE}' to suppress."
        ),
        "LITERAL_IDENTITY": (
            f"Identity assignment '{var_name}' is redundant. "
            f"Consider using literal directly. Or add "
            f"'# pytriage: ignore={ERROR_CODE}' to suppress."
        ),
    }
    return messages.get(
        pattern_type,
        f"Redundant assignment '{var_name}'. Or add '# pytriage: ignore={ERROR_CODE}' to suppress.",
    )


class RedundantAssignmentCheck(BaseCheck):
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

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--redundant-assignment-level",
            choices=["conservative", "permissive"],
            default="conservative",
            help=(
                "How eagerly redundant-assignment (TRI005) reports a "
                "violation. 'conservative' (default) flags only the "
                "clearest, safest-to-inline cases; 'permissive' flags a "
                "broader range. Either way, --fix applies to whatever is "
                "reported and mechanically safe to inline — the level "
                "doesn't narrow autofix separately."
            ),
        )

    @classmethod
    def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]:
        return {"level": AggressivenessLevel[args.redundant_assignment_level.upper()]}

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)

        tracker = VariableTracker(source)
        tracker.visit(tree)
        lifecycles = tracker.build_lifecycles()

        # Count assignments per (scope, variable) to identify state tracking
        assignment_counts: dict[tuple[int, str], int] = {}
        for lifecycle in lifecycles:
            key = (lifecycle.assignment.scope_id, lifecycle.assignment.var_name)
            assignment_counts[key] = assignment_counts.get(key, 0) + 1

        violations: list[Violation] = []

        for lifecycle in lifecycles:
            # Skip if variable has multiple assignments (state tracking pattern)
            key = (lifecycle.assignment.scope_id, lifecycle.assignment.var_name)
            if assignment_counts[key] > 1:
                continue

            pattern = detect_redundancy(lifecycle)
            if pattern is None:
                continue

            if lifecycle.assignment.line in ignored_lines:
                continue

            if not should_report_violation(lifecycle, pattern, filepath, level=self._level):
                continue

            # Pass the real source lines so the line-length check matches
            # the actual usage line, not just a conservative RHS-length
            # estimate (see docs: apply_fixes independently re-checks the
            # real line, and the two must agree or [FIXABLE] can lie about
            # --fix).
            fixable = should_autofix(lifecycle, source_lines=tracker.source_lines)

            message = format_message(lifecycle.assignment.var_name, pattern.name)

            # fix_data must stay serializable (no AST nodes/lifecycle objects):
            # apply_fixes() only ever needs these primitives. detect_redundancy()
            # only returns a pattern for single-use lifecycles (see its
            # `is_single_use` precondition), so this always holds here.
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

            # lifecycle.assignment.col is a UTF-8 byte offset (from
            # ast.col_offset); the reported diagnostic column is a
            # character offset (matching misplaced-comment's own
            # tokenize-derived column), so convert before storing it on the
            # Violation. fix_data's own "use_col" above is intentionally
            # left as a raw byte offset: autofix.py re-reads and converts
            # it itself, against whatever line the fix actually targets.
            assign_line_text = tracker.source_lines[lifecycle.assignment.line - 1]
            violation = Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=lifecycle.assignment.line,
                col=byte_col_to_char_col(assign_line_text, lifecycle.assignment.col),
                message=message,
                fixable=fixable,
                # Violation.fix_data is intentionally untyped (dict[str,
                # Any]) at this boundary; see RedundantAssignmentFixData in
                # autofix.py for the shape check()/apply_fixes() agree on.
                fix_data=cast("dict[str, Any]", fix_data),
            )
            violations.append(violation)

        return violations

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        _tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool:
        return apply_fixes(filepath, violations, source, encoding)
