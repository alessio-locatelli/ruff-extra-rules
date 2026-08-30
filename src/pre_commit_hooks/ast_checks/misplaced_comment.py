from __future__ import annotations

import functools
import logging
import re
import tokenize
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
    atomic_write_text,
    ignore_pattern_for,
    ignored_lines_and_pytriage_comments_from_tokens,
    ignored_lines_from_tokens,
    line_terminator,
    record_suppression_usage_if_ignored,
    tokenize_source,
)

if TYPE_CHECKING:
    import ast
    from pathlib import Path

logger = logging.getLogger("misplaced_comment")

CHECK_ID = "misplaced-comment"
ERROR_CODE = "TR7"

IGNORE_PATTERN = ignore_pattern_for("TR7")

LINTER_PRAGMA_PATTERNS = [
    r"#\s*noqa",
    r"#\s*type:\s*ignore",
    r"#\s*pragma:",
    r"#\s*pylint:",
    r"#\s*pyright:",
    r"#\s*mypy:",
    r"#\s*flake8:",
    r"#\s*ruff:",
    r"#\s*bandit:",
    r"#\s*nosec",
    r"#\s*isort:",
]
_COMPILED_LINTER_PATTERNS = {re.compile(p) for p in LINTER_PRAGMA_PATTERNS}


@functools.cache
def is_linter_pragma(comment_text: str) -> bool:
    return any(pattern.search(comment_text) for pattern in _COMPILED_LINTER_PATTERNS)


def is_bracket_only_line(line_tokens: list[tokenize.TokenInfo]) -> bool:
    code_tokens = [
        t
        for t in line_tokens
        if t.type
        not in (
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.COMMENT,
            tokenize.ENCODING,
        )
    ]

    return all(t.type == tokenize.OP and t.string in ")}]" for t in code_tokens)


@dataclass(frozen=True, slots=True)
class _MisplacedComment:
    bracket_line: int
    comment_line: int
    comment_col: int
    comment_text: str


def _scan_misplaced_comments(
    tokens: tuple[tokenize.TokenInfo, ...],
) -> list[_MisplacedComment]:
    tokens_by_line: dict[int, list[tokenize.TokenInfo]] = {}
    for token in tokens:
        tokens_by_line.setdefault(token.start[0], []).append(token)

    found: list[_MisplacedComment] = []
    seen_bracket_lines: set[int] = set()

    for token in tokens:
        if token.type != tokenize.OP or token.string not in ")}]":
            continue
        bracket_line = token.start[0]
        if bracket_line in seen_bracket_lines:
            continue
        seen_bracket_lines.add(bracket_line)

        line_tokens = tokens_by_line[bracket_line]
        comment_token = next((t for t in line_tokens if t.type == tokenize.COMMENT), None)
        if (
            comment_token is not None
            and not is_linter_pragma(comment_token.string)
            and not comment_token.string.startswith("#:")
            and is_bracket_only_line(line_tokens)
        ):
            found.append(
                _MisplacedComment(
                    bracket_line=bracket_line,
                    comment_line=comment_token.start[0],
                    comment_col=comment_token.start[1],
                    comment_text=comment_token.string,
                )
            )

    return found


class MisplacedCommentCheck(BaseCheck):
    __slots__ = ()

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["#"]

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> CheckResult:
        tokens = tuple(tokenize_source(source))
        found = _scan_misplaced_comments(tokens)
        if not found:
            return CheckResult()

        ignored_lines, format_suppressed, comments = ignored_lines_and_pytriage_comments_from_tokens(
            tokens, IGNORE_PATTERN
        )
        violations = []
        suppression_usages: list[SuppressionUsage] = []
        for item in found:
            if record_suppression_usage_if_ignored(
                suppression_usages,
                comments,
                ignored_lines=ignored_lines,
                format_suppressed=format_suppressed,
                check_id=self.check_id,
                error_code=self.error_code,
                candidate_lines=(item.bracket_line,),
            ):
                continue
            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=item.bracket_line,
                    col=item.comment_col,
                    message=(
                        f"Comment on line {item.comment_line} should not be on "
                        "closing bracket line. Or add "
                        "'# pytriage: TR7' to suppress."
                    ),
                    fixable=True,
                )
            )
        return CheckResult(violations, suppression_usages)

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        _tree: ast.Module,
        encoding: str = "utf-8",
    ) -> FixResult:
        tokens = tuple(tokenize_source(source))
        found = _scan_misplaced_comments(tokens)
        if not found:
            return FixResult.for_violations(violations, FixOutcome.DECLINED)

        ignored_lines = ignored_lines_from_tokens(tokens, IGNORE_PATTERN)
        lines = source.splitlines(keepends=True)
        fixed_any = False
        fixed_lines: set[int] = set()

        for item in found:
            if item.bracket_line in ignored_lines:
                continue

            bracket_line_idx = item.bracket_line - 1
            prev_line_idx = bracket_line_idx - 1
            # A bracket-only line can only exist if its opening bracket
            # precedes it on an earlier line, so prev_line_idx is never < 0.
            assert prev_line_idx >= 0

            # Reuse each touched line's own terminator instead of a bare
            # "\n": a CRLF file must not end up with mixed line endings on
            # exactly the lines this fix rewrites (ch. 3/21: preserve the
            # newline convention and avoid unrelated formatting changes).
            prev_terminator = line_terminator(lines[prev_line_idx])
            prev_line = lines[prev_line_idx].rstrip()
            indent = len(lines[prev_line_idx]) - len(lines[prev_line_idx].lstrip())
            potential_inline = f"{prev_line}  {item.comment_text}"

            if len(potential_inline) <= 88:
                lines[prev_line_idx] = f"{prev_line}  {item.comment_text}{prev_terminator}"
            else:
                lines[prev_line_idx] = f"{' ' * indent}{item.comment_text}{prev_terminator}{prev_line}{prev_terminator}"

            bracket_terminator = line_terminator(lines[bracket_line_idx])
            lines[bracket_line_idx] = lines[bracket_line_idx][: item.comment_col].rstrip() + bracket_terminator
            fixed_any = True
            fixed_lines.add(item.bracket_line)

        if fixed_any:
            try:
                atomic_write_text(filepath, "".join(lines), encoding, source)
            except OSError:
                logger.debug("Failed to write %s", filepath, exc_info=True)
                return FixResult(
                    tuple(FixOutcome.FAILED if v.line in fixed_lines else FixOutcome.DECLINED for v in violations)
                )

        return FixResult(
            tuple(FixOutcome.APPLIED if v.line in fixed_lines else FixOutcome.DECLINED for v in violations)
        )
