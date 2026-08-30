from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypedDict, cast

from pre_commit_hooks.ast_checks._base import (
    BaseCheck,
    CheckResult,
    ConcurrentModificationError,
    FixOutcome,
    FixResult,
    FixValidationError,
    SuppressionUsage,
    Violation,
    find_ignored_lines,
    find_ignored_lines_and_pytriage_comments,
    record_suppression_usage_if_ignored,
)

from .analysis import IGNORE_PATTERN, Suggestion, collect_suggestions
from .autofix import (
    _repository_reference_status,
    apply_fix,
    index_function_nodes,
    is_autofix_safe,
    should_autofix,
)

if TYPE_CHECKING:
    import ast
    from pathlib import Path

ERROR_CODE = "TR4"

logger_check = logging.getLogger("validate_function_name_check")


class ValidateFunctionNameFixData(TypedDict):
    suggestion: Suggestion


class ValidateFunctionNameCheck(BaseCheck):
    __slots__ = ()

    @property
    def check_id(self) -> str:
        return "validate-function-name"

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["def get_"]

    def check(self, filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        return self._check(filepath, tree, source, collect_suppression_usage=False)

    def check_with_suppression_tracking(self, filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        return self._check(filepath, tree, source, collect_suppression_usage=True)

    def _check(self, filepath: Path, tree: ast.Module, source: str, *, collect_suppression_usage: bool) -> CheckResult:
        if collect_suppression_usage:
            ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(
                source, IGNORE_PATTERN
            )
        else:
            ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
            format_suppressed = set()
            comments = ()
        suggestions = collect_suggestions(filepath, tree, source, ignored_lines)
        all_suggestions = (
            collect_suggestions(filepath, tree, source, set())
            if collect_suppression_usage and ignored_lines
            else suggestions
        )
        function_index = index_function_nodes(tree)

        violations = []
        suppression_usages: list[SuppressionUsage] = []
        if collect_suppression_usage:
            for suggestion in all_suggestions:
                record_suppression_usage_if_ignored(
                    suppression_usages,
                    comments,
                    ignored_lines=ignored_lines,
                    format_suppressed=format_suppressed,
                    check_id=self.check_id,
                    error_code=self.error_code,
                    candidate_lines=(suggestion.lineno,),
                )
        for suggestion in suggestions:
            auto_fixable = not suggestion.requires_property and is_autofix_safe(function_index, suggestion)
            reference_status = _repository_reference_status(filepath, suggestion.func_name) if auto_fixable else "safe"
            if suggestion.requires_property:
                message = (
                    f"Function '{suggestion.func_name}' should use @property "
                    f"'{suggestion.suggested_name}' ({suggestion.reason})"
                )
            else:
                message = (
                    f"Function '{suggestion.func_name}' should be renamed to "
                    f"'{suggestion.suggested_name}' ({suggestion.reason})"
                )
            if reference_status == "external":
                message += "; manual rename required because the existing name occurs elsewhere in the repository"
            elif reference_status == "unavailable":
                message += "; manual rename required because repository references could not be checked"

            fix_data: ValidateFunctionNameFixData = {"suggestion": suggestion}
            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=suggestion.lineno,
                    col=0,
                    message=message,
                    fixable=auto_fixable and reference_status == "safe",
                    # Violation.fix_data is intentionally untyped (dict[str,
                    # Any]) at this boundary; see ValidateFunctionNameFixData
                    # above for the shape check()/fix() actually agree on.
                    fix_data=cast("dict[str, Any]", fix_data),
                )
            )

        return CheckResult(violations, suppression_usages)

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> FixResult:
        """Apply fixes for function naming violations.

        Note: apply_fix() re-reads the file itself (and detects its own
        encoding via read_source_with_encoding) rather than using `source`/
        `encoding` here. Unlike check(), this isn't a pure inefficiency to
        remove: when a file has multiple get_ functions to rename, applying
        one rename can shift the text a later rename's positions were
        computed against, so each apply_fix() call re-reads the
        just-written file to stay correct against the current file state.
        """
        if not violations:
            return FixResult.for_violations(violations, FixOutcome.DECLINED)

        outcomes = [FixOutcome.DECLINED] * len(violations)

        for index, violation in enumerate(violations):
            if not violation.fix_data:
                continue

            fix_data = cast("ValidateFunctionNameFixData", violation.fix_data)
            suggestion = fix_data.get("suggestion")
            if not suggestion or suggestion.requires_property:
                continue

            outcome = should_autofix(filepath, suggestion)
            if outcome is not FixOutcome.APPLIED:
                outcomes[index] = outcome
                continue
            if _repository_reference_status(filepath, suggestion.func_name) != "safe":
                continue
            try:
                outcomes[index] = apply_fix(filepath, suggestion)
            except FixValidationError:
                outcomes[index] = FixOutcome.REJECTED
            except ConcurrentModificationError:
                outcomes[index] = FixOutcome.ABORTED
            except Exception:
                # A bug in apply_fix() itself, distinct from
                # FixValidationError above: mark it so the orchestrator's
                # post-fix re-check reports this specific violation as
                # [FIX ERRORED] rather than an ordinary, retryable
                # [FIXABLE] — re-running --fix would just fail here
                # identically again.
                # Debug-only: the outcome assignment below already reports
                # this cleanly as [FIX ERRORED] — an ERROR-level
                # .exception() call here would just leak a redundant raw
                # traceback onto the user's stderr by default (nothing
                # in this codebase configures logging, so Python's own
                # lastResort handler prints WARNING+ straight to
                # stderr).
                logger_check.debug("Failed to apply fix for %s in %s", suggestion.func_name, filepath, exc_info=True)
                outcomes[index] = FixOutcome.ERRORED

        return FixResult(tuple(outcomes))
