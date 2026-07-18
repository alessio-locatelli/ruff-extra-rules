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

This check runs as part of the grouped `ruff-extra-rules` hook:

    python -m pre_commit_hooks.ast_checks [--fix] <files>

Suppression:
    Add inline comment to suppress: # pytriage: ignore=TRI004

Example:
    def get_users() -> list[User]:  # pytriage: ignore=TRI004
        return User.objects.all()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypedDict, cast

from pre_commit_hooks.ast_checks._base import BaseCheck, Violation

from .analysis import Suggestion, collect_suggestions
from .autofix import apply_fix, should_autofix

if TYPE_CHECKING:
    import ast
    from pathlib import Path

ERROR_CODE = "TRI004"

logger_check = logging.getLogger("validate_function_name_check")


class ValidateFunctionNameFixData(TypedDict):
    """Constructed by check(), read back by fix() to re-apply the same
    suggestion computed during check() rather than recomputing it.
    """

    suggestion: Suggestion


class ValidateFunctionNameCheck(BaseCheck):
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
        # Reuse the orchestrator's already-parsed tree/source instead of
        # re-reading and re-parsing the file (see analysis.process_file for
        # the standalone equivalent used by tests).
        suggestions = collect_suggestions(filepath, tree, source)

        # Convert Suggestion objects to Violation objects
        violations = []
        for suggestion in suggestions:
            message = (
                f"Function '{suggestion.func_name}' should be renamed to "
                f"'{suggestion.suggested_name}' ({suggestion.reason})"
            )

            fix_data: ValidateFunctionNameFixData = {"suggestion": suggestion}
            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=suggestion.lineno,
                    col=0,
                    message=message,
                    fixable=True,  # May be fixable based on complexity
                    # Violation.fix_data is intentionally untyped (dict[str,
                    # Any]) at this boundary; see ValidateFunctionNameFixData
                    # above for the shape check()/fix() actually agree on.
                    fix_data=cast("dict[str, Any]", fix_data),
                )
            )

        return violations

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> bool:
        """Apply fixes for function naming violations.

        Note: apply_fix() re-reads the file itself (and detects its own
        encoding via read_source_with_encoding) rather than using `source`/
        `encoding` here. Unlike check(), this isn't a pure inefficiency to
        remove: when a file has multiple get_ functions to rename, applying
        one rename can shift the text a later rename's positions were
        computed against, so each apply_fix() call re-reads the
        just-written file to stay correct against the current file state.

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

            fix_data = cast("ValidateFunctionNameFixData", violation.fix_data)
            suggestion = fix_data.get("suggestion")
            if not suggestion:
                continue

            # Check if safe to autofix
            if should_autofix(filepath, suggestion):
                try:
                    if apply_fix(filepath, suggestion):
                        applied_any = True
                except Exception:
                    logger_check.exception("Failed to apply fix for %s in %s", suggestion.func_name, filepath)

        return applied_any
