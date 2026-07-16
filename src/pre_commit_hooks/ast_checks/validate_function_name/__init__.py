"""validate_function_name - Detect get_* functions and suggest better names.

TRI004: Functions with get_ prefix should use more descriptive names based on
their behavior (e.g., load_, fetch_, calculate_, is_, iter_).

This hook detects functions prefixed with `get_` and suggests more specific
names based on behavioral analysis:

- Boolean returns → is_*
- Disk I/O → load_*/save_*
- Network I/O → fetch_*/send_*
- Generators → iter_*
- Aggregation → calculate_*
- Parsing → parse_*
- Searching → find_*
- Validation → validate_*
- Collection → extract_*
- Object creation → create_*
- Mutation → update_*

This check runs as part of the grouped `ast-checks` hook:

    python -m pre_commit_hooks.ast_checks [--fix] <files>

Suppression:
    Add inline comment to suppress: # pytriage: ignore=TRI004

Example:
    def get_users() -> list[User]:  # pytriage: ignore=TRI004
        return User.objects.all()
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from .._base import Violation
from .analysis import process_file
from .autofix import apply_fix, should_autofix

ERROR_CODE = "TRI004"

logger_check = logging.getLogger("validate_function_name_check")


class ValidateFunctionNameCheck:
    """Check for get_* functions and suggest better names."""

    @property
    def check_id(self) -> str:
        return "validate-function-name"

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["def get_"]

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        """Run check and return violations.

        Args:
            filepath: Path to file
            tree: Parsed AST tree
            source: Source code

        Returns:
            List of violations
        """
        # Use existing analysis module
        suggestions = process_file(filepath)

        # Convert Suggestion objects to Violation objects
        violations = []
        for suggestion in suggestions:
            message = (
                f"Function '{suggestion.func_name}' should be renamed to "
                f"'{suggestion.suggested_name}' ({suggestion.reason})"
            )

            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=suggestion.lineno,
                    col=0,
                    message=message,
                    fixable=True,  # May be fixable based on complexity
                    fix_data={
                        "suggestion": suggestion,  # Store original for autofix
                    },
                )
            )

        return violations

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool:
        """Apply fixes for function naming violations.

        Note: apply_fix() re-reads the file itself (and detects its own
        encoding via read_source_with_encoding) rather than using `source`/
        `encoding` here — see analysis.process_file, which check() also
        routes through independently of CheckOrchestrator's own read.

        Args:
            filepath: Path to file
            violations: Violations to fix
            source: Source code
            tree: Parsed AST tree

        Returns:
            True if fixes were applied successfully
        """
        if not violations:
            return False

        applied_any = False

        for violation in violations:
            if not violation.fix_data:
                continue

            suggestion = violation.fix_data.get("suggestion")
            if not suggestion:
                continue

            # Check if safe to autofix
            if should_autofix(filepath, suggestion):
                try:
                    if apply_fix(filepath, suggestion):
                        applied_any = True
                        # Mark as fixed
                        violation.fix_data["fixed"] = True
                except Exception as fix_error:  # noqa: BLE001
                    logger_check.error(
                        "Failed to apply fix for %s in %s: %s",
                        suggestion.func_name,
                        filepath,
                        repr(fix_error),
                    )

        return applied_any
