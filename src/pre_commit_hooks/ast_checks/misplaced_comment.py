"""misplaced-comment - move trailing comments off closing-bracket-only lines.

TR7: a comment trailing a line that contains only closing brackets
should move to the expression line instead (inline if it fits within 88
chars, otherwise as a preceding comment). Linter pragma comments (noqa,
type-checker ignores, coverage pragmas, etc.) are never moved.

Inline ignore: # pytriage: TR7
"""

from __future__ import annotations

import ast
import functools
import logging
import re
import tokenize
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._base import (
    BaseCheck,
    FixOutcome,
    FixResult,
    Violation,
    atomic_write_text,
    ignore_pattern_for,
    ignored_lines_from_tokens,
    line_terminator,
    tokenize_source,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("misplaced_comment")

CHECK_ID = "misplaced-comment"
ERROR_CODE = "TR7"

IGNORE_PATTERN = ignore_pattern_for("TR7")

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
    tree: ast.Module,
) -> list[_MisplacedComment]:
    """Find comments trailing bracket-only closing lines.

    Shared by check() and fix() so both agree on what counts as a violation.
    Dedupes by bracket_line: a line like `))  # comment` visits the scan once
    per closing bracket token, but is one violation, not one per bracket.

    Groups tokens by physical line once, up front: a physical line's own
    trailing comment, if any, is always among that same line's own tokens
    (`tokenize` never emits a `COMMENT` past its own line's `NEWLINE`), so
    a bracket's verdict only ever depends on its own line's token group,
    never on anything past it.
    """
    tokens_by_line: dict[int, list[tokenize.TokenInfo]] = {}
    for token in tokens:
        tokens_by_line.setdefault(token.start[0], []).append(token)

    sphinx_attribute_comment_lines = {
        node.end_lineno
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.end_lineno is not None
    }
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
        is_sphinx_attribute_comment = (
            comment_token is not None
            and comment_token.string.startswith("#:")
            and bracket_line in sphinx_attribute_comment_lines
        )
        if (
            comment_token is not None
            and not is_linter_pragma(comment_token.string)
            and not is_sphinx_attribute_comment
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

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        tokens = tuple(tokenize_source(source))
        found = _scan_misplaced_comments(tokens, tree)
        if not found:
            return []

        ignored_lines = ignored_lines_from_tokens(tokens, IGNORE_PATTERN)
        return [
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
            for item in found
            if item.bracket_line not in ignored_lines
        ]

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> FixResult:
        tokens = tuple(tokenize_source(source))
        found = _scan_misplaced_comments(tokens, tree)
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
                # Debug-only: the returned outcome already reports this
                # cleanly as [FIX FAILED] — an ERROR-level .exception() call
                # here would just leak a redundant raw traceback onto the
                # user's stderr by default (nothing in this codebase
                # configures logging, so Python's own lastResort handler
                # prints WARNING+ straight to stderr).
                logger.debug("Failed to write %s", filepath, exc_info=True)
                return FixResult(
                    tuple(FixOutcome.FAILED if v.line in fixed_lines else FixOutcome.DECLINED for v in violations)
                )

        return FixResult(
            tuple(FixOutcome.APPLIED if v.line in fixed_lines else FixOutcome.DECLINED for v in violations)
        )
