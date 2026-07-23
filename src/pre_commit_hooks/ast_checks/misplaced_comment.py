"""misplaced-comment - move trailing comments off closing-bracket-only lines.

STYLE-001: a comment trailing a line that contains only closing brackets
should move to the expression line instead (inline if it fits within 88
chars, otherwise as a preceding comment). Linter pragma comments (noqa,
type-checker ignores, coverage pragmas, etc.) are never moved.

Inline ignore: # pytriage: ignore=STYLE-001
"""

from __future__ import annotations

import functools
import logging
import re
import tokenize
from dataclasses import dataclass
from io import StringIO
from typing import TYPE_CHECKING

from ._base import (
    BaseCheck,
    Violation,
    atomic_write_text,
    find_ignored_lines,
    ignore_pattern_for,
    line_terminator,
    mark_fix_failed,
    normalize_for_tokenize,
)

if TYPE_CHECKING:
    import ast
    from pathlib import Path

logger = logging.getLogger("misplaced_comment")

CHECK_ID = "misplaced-comment"
ERROR_CODE = "STYLE-001"

IGNORE_PATTERN = ignore_pattern_for("STYLE-001")

# Linter pragma patterns that should NEVER be moved
LINTER_PRAGMA_PATTERNS = [
    r"#\s*noqa",  # flake8, ruff
    r"#\s*type:\s*ignore",  # mypy, pyright
    r"#\s*pragma:",  # coverage, general pragma
    r"#\s*pylint:",  # pylint
    r"#\s*pyright:",  # pyright
    r"#\s*mypy:",  # mypy
    r"#\s*flake8:",  # flake8
    r"#\s*ruff:",  # ruff
    r"#\s*bandit:",  # bandit
    r"#\s*nosec",  # bandit
    r"#\s*isort:",  # isort
]
_COMPILED_LINTER_PATTERNS = {re.compile(p) for p in LINTER_PRAGMA_PATTERNS}


@functools.cache
def is_linter_pragma(comment_text: str) -> bool:
    return any(pattern.search(comment_text) for pattern in _COMPILED_LINTER_PATTERNS)


def is_bracket_only_line(tokens: tuple[tokenize.TokenInfo, ...], bracket_token_idx: int) -> bool:
    bracket_token = tokens[bracket_token_idx]
    line_num = bracket_token.start[0]

    line_tokens = [t for t in tokens if t.start[0] == line_num]
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
    """Find comments trailing bracket-only closing lines.

    Shared by check() and fix() so both agree on what counts as a violation.
    Dedupes by bracket_line: a line like `))  # comment` visits the scan once
    per closing bracket token, but is one violation, not one per bracket.
    """
    found: list[_MisplacedComment] = []
    seen_bracket_lines: set[int] = set()

    for i, token in enumerate(tokens):
        if token.type != tokenize.OP or token.string not in ")}]":
            continue
        bracket_line = token.start[0]
        if bracket_line in seen_bracket_lines:
            continue

        # Find the first token that's either a comment (a candidate, since
        # anything else on this same line doesn't matter) or on a later
        # line (nothing to find on this bracket's line). Every token stream
        # ends with an ENDMARKER on a line past the last real line, so this
        # always finds something.
        next_token = next(t for t in tokens[i + 1 :] if t.start[0] > bracket_line or t.type == tokenize.COMMENT)
        if (
            next_token.start[0] == bracket_line
            and not is_linter_pragma(next_token.string)
            and is_bracket_only_line(tokens, i)
        ):
            found.append(
                _MisplacedComment(
                    bracket_line=bracket_line,
                    comment_line=next_token.start[0],
                    comment_col=next_token.start[1],
                    comment_text=next_token.string,
                )
            )
            seen_bracket_lines.add(bracket_line)

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

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> list[Violation]:
        try:
            tokens = tuple(tokenize.generate_tokens(StringIO(normalize_for_tokenize(source)).readline))
        # Defensive: source is already parsed by AST, so tokenizing it can't
        # realistically fail. If it ever does, treat it as no violations.
        except tokenize.TokenError as token_error:  # pragma: no cover
            logger.debug(repr(token_error))
            return []

        found = _scan_misplaced_comments(tokens)
        if not found:
            return []

        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
        return [
            Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=item.bracket_line,
                col=item.comment_col,
                message=(
                    f"Comment on line {item.comment_line} should not be on "
                    "closing bracket line. Or add "
                    "'# pytriage: ignore=STYLE-001' to suppress."
                ),
                fixable=True,
            )
            for item in found
            if item.bracket_line not in ignored_lines
        ]

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        _tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool:
        try:
            tokens = tuple(tokenize.generate_tokens(StringIO(normalize_for_tokenize(source)).readline))
        # Defensive: source is already parsed by AST, so tokenizing it can't
        # realistically fail. If it ever does, skip fixing rather than crash.
        except tokenize.TokenError as token_error:  # pragma: no cover
            logger.debug(repr(token_error))
            return False

        found = _scan_misplaced_comments(tokens)
        if not found:
            return False

        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
        lines = source.splitlines(keepends=True)
        fixed_any = False

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

        if fixed_any:
            try:
                atomic_write_text(filepath, "".join(lines), encoding)
            except OSError:
                # Debug-only: mark_fix_failed() below already reports this
                # cleanly as [FIX FAILED] — an ERROR-level .exception() call
                # here would just leak a redundant raw traceback onto the
                # user's stderr by default (nothing in this codebase
                # configures logging, so Python's own lastResort handler
                # prints WARNING+ straight to stderr).
                logger.debug("Failed to write %s", filepath, exc_info=True)
                for v in violations:
                    mark_fix_failed(v)
                return False

        return fixed_any
