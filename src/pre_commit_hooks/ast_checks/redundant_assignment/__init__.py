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

from .._base import BaseCheck, Violation, find_ignored_lines, ignore_pattern_for
from .analysis import VariableTracker, detect_redundancy
from .autofix import RedundantAssignmentFixData, apply_fixes
from .semantic import should_autofix, should_report_violation

if TYPE_CHECKING:
    import ast
    from pathlib import Path

# Format: # pytriage: ignore=TRI005
IGNORE_PATTERN = ignore_pattern_for("TRI005")

ERROR_CODE = "TRI005"
CHECK_ID = "redundant-assignment"


def format_message(var_name: str, pattern_type: str) -> str:
    """Format a violation message.

    Args:
        var_name: Variable name
        pattern_type: Pattern type description

    Returns:
        Formatted message
    """
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
        f"Redundant assignment '{var_name}'. "
        f"Or add '# pytriage: ignore={ERROR_CODE}' to suppress.",
    )


class RedundantAssignmentCheck(BaseCheck):
    """Check for redundant variable assignments (TRI005)."""

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        """Return pattern for git grep pre-filtering.

        Returns:
            Pattern to match assignment statements
        """
        return [" = "]

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        """Run redundancy check on a file.

        Args:
            filepath: Path to file being checked
            tree: Parsed AST tree
            source: Original source code

        Returns:
            List of violations found
        """
        # Get ignored lines
        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)

        # Track variables
        tracker = VariableTracker(source)
        tracker.visit(tree)
        lifecycles = tracker.build_lifecycles()

        # Count assignments per (scope, variable) to identify state tracking
        assignment_counts: dict[tuple[int, str], int] = {}
        for lifecycle in lifecycles:
            key = (lifecycle.assignment.scope_id, lifecycle.assignment.var_name)
            assignment_counts[key] = assignment_counts.get(key, 0) + 1

        # Detect redundant assignments
        violations: list[Violation] = []

        for lifecycle in lifecycles:
            # Skip if variable has multiple assignments (state tracking pattern)
            key = (lifecycle.assignment.scope_id, lifecycle.assignment.var_name)
            if assignment_counts[key] > 1:
                continue

            # Detect pattern
            pattern = detect_redundancy(lifecycle)
            if pattern is None:
                continue

            # Check if line is suppressed
            if lifecycle.assignment.line in ignored_lines:
                continue

            # Apply semantic filtering
            if not should_report_violation(lifecycle, pattern, filepath):
                continue

            # Determine if fixable (very conservative - only simplest cases).
            # Pass the real source lines so the line-length check matches the
            # actual usage line, not just a conservative RHS-length estimate
            # (see docs: apply_fixes independently re-checks the real line,
            # and the two must agree or [FIXABLE] can lie about --fix).
            fixable = should_autofix(
                lifecycle, pattern, filepath, source_lines=tracker.source_lines
            )

            # Create violation
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
            }

            violation = Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=lifecycle.assignment.line,
                col=lifecycle.assignment.col,
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
        """Apply fixes for redundant assignment violations.

        Args:
            filepath: Path to file to fix
            violations: List of violations to fix
            source: Original source code
            tree: Parsed AST tree
            encoding: Encoding to write the file back with

        Returns:
            True if fixes were successfully applied
        """
        return apply_fixes(filepath, violations, source, encoding)
