from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pre_commit_hooks.ast_checks._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    Violation,
    byte_col_to_char_col,
    find_ignored_lines,
    find_ignored_lines_and_pytriage_comments,
    find_suppression_usage,
    ignore_pattern_for,
)

from .candidates import find_candidates
from .local import find_proofs as find_local_proofs

if TYPE_CHECKING:
    import ast
    from pathlib import Path

    from pre_commit_hooks.ast_checks._options import CheckOption

CHECK_ID = "redundant-dict-get"
ERROR_CODE = "TR9"
IGNORE_PATTERN = ignore_pattern_for(ERROR_CODE)


class RedundantDictGetCheck(BaseCheck):
    __slots__ = ()

    OPTIONS: ClassVar[tuple[CheckOption, ...]] = ()

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    @property
    def default_enabled(self) -> bool:
        return True

    def get_prefilter_pattern(self) -> list[str] | None:
        return [".get("]

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        return self._check(tree, source, collect_suppression_usage=False)

    def check_with_suppression_tracking(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        return self._check(tree, source, collect_suppression_usage=True)

    def _check(self, tree: ast.Module, source: str, *, collect_suppression_usage: bool) -> CheckResult:
        candidates = find_candidates(tree)
        if not candidates:
            return CheckResult()
        if collect_suppression_usage:
            ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(
                source, IGNORE_PATTERN
            )
            pytriage_lines = {
                comment.line
                for comment in comments
                if self.error_code in comment.codes and comment.line not in format_suppressed
            }
            analysis_ignored_lines = ignored_lines - pytriage_lines
        else:
            ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
            format_suppressed = set()
            comments = ()
            analysis_ignored_lines = ignored_lines
        active = [candidate for candidate in candidates if candidate.call.lineno not in analysis_ignored_lines]
        if not active:
            return CheckResult()
        local_proofs = {proof.candidate: proof.reason for proof in find_local_proofs(tree, active)}
        proofs = local_proofs
        violations = []
        usages = []
        source_lines = source.splitlines(keepends=True)
        for candidate in candidates:
            if candidate not in proofs:
                continue
            if candidate.call.lineno in ignored_lines:
                usage = find_suppression_usage(
                    comments, format_suppressed, self.check_id, self.error_code, (candidate.call.lineno,)
                )
                assert usage is not None
                usages.append(usage)
                continue
            violations.append(
                Violation(
                    check_id=CHECK_ID,
                    error_code=ERROR_CODE,
                    line=candidate.call.lineno,
                    col=byte_col_to_char_col(
                        source_lines[candidate.call.func.lineno - 1], candidate.call.func.col_offset
                    ),
                    message=(
                        f"Redundant `dict.get()`: {proofs[candidate]}; "
                        f"use `{candidate.receiver}[{candidate.replacement_key}]`. "
                        f"Or add '# pytriage: {ERROR_CODE}' to suppress."
                    ),
                    fixable=False,
                )
            )
        return CheckResult(violations, usages)

    def fix(
        self,
        _filepath: Path,
        violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.DECLINED)
