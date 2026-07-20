"""Check and fix excessive blank lines after module headers.

TRI002: Collapse 2+ consecutive blank lines after module headers (copyright,
docstring, or comments) to a single blank line.

Inline ignore: # pytriage: ignore=TRI002, placed on the first code line after
the blank run (the violation's own line is blank, so it can't carry a
trailing comment itself).
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._base import (
    BaseCheck,
    Violation,
    atomic_write_text,
    find_ignored_lines,
    ignore_pattern_for,
    mark_fix_failed,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("excessive_blank_lines")

IGNORE_PATTERN = ignore_pattern_for("TRI002")


@dataclass(frozen=True)
class _BlankRunViolation:
    line: int
    anchor_line: int
    message: str


def _format_message(blank_count: int, target: int) -> str:
    return (
        f"Excessive blank lines ({blank_count}) should be collapsed to {target}. "
        "Add '# pytriage: ignore=TRI002' to the line following the blank run "
        "to suppress."
    )


def find_module_header_end(lines: list[str], tree: ast.Module) -> int:
    """Module header includes: shebang, encoding, docstring, copyright/comments.

    Comments aren't part of the AST, so they still need a text scan, but the
    docstring's own extent is taken directly from the parsed module rather
    than re-derived from raw text. This correctly handles raw-prefixed
    docstrings (an r-string) that a naive quote-prefix text scan would miss
    (byte strings can't be docstrings at all, per Python's own semantics).

    Returns index (0-based) where module header ends.
    """
    start_idx = 0

    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        # end_lineno is 1-indexed, so it's already the 0-indexed line after it
        start_idx = tree.body[0].end_lineno or 0

    for i in range(start_idx, len(lines)):
        stripped = lines[i].strip()

        # Empty lines and comments (shebang, encoding, copyright) are header
        if not stripped or stripped.startswith("#"):
            continue

        # First code line (import, class, def, assignment, etc)
        return i

    return len(lines)


def check_file_violations(source: str, tree: ast.Module) -> list[_BlankRunViolation]:
    lines = source.splitlines(keepends=True)

    if not lines:
        return []

    violations = []
    header_end = find_module_header_end(lines, tree)

    # Find the last non-blank line in the header region
    last_header_line = 0
    for i in range(header_end - 1, -1, -1):
        if lines[i].strip():
            last_header_line = i + 1
            break

    blank_count = 0
    start_blank = None
    found_first_code_line = False

    for i in range(last_header_line, len(lines)):
        line = lines[i]
        if line.strip() == "":
            if blank_count == 0:
                start_blank = i
            blank_count += 1
        else:
            # Non-blank line found
            # Only report violations before the first code line
            if not found_first_code_line and blank_count >= 2 and start_blank is not None:
                # anchor_line is this line — the violation's own start_blank
                # line is blank and can't carry a trailing ignore comment.
                anchor_line = i + 1
                # Check if this line is a class or function definition
                # PEP 8 allows 2 blank lines before top-level class/function definitions
                if _is_class_or_function_def(line):
                    # Only report if more than 2 blank lines
                    if blank_count > 2:
                        violations.append(
                            _BlankRunViolation(
                                line=start_blank + 1,
                                anchor_line=anchor_line,
                                message=_format_message(blank_count, target=2),
                            )
                        )
                else:
                    # For non-class/function definitions, report if >= 2 blank lines
                    violations.append(
                        _BlankRunViolation(
                            line=start_blank + 1,
                            anchor_line=anchor_line,
                            message=_format_message(blank_count, target=1),
                        )
                    )
            blank_count = 0
            start_blank = None
            found_first_code_line = True

    return violations


def _is_class_or_function_def(line: str) -> bool:
    return line.lstrip().startswith(("class ", "def ", "async def "))


def fix_file_content(source: str, tree: ast.Module) -> str:
    lines = source.splitlines(keepends=True)

    if not lines:
        return source

    header_end = find_module_header_end(lines, tree)

    # Find the last non-blank line in the header region
    last_header_line = 0
    for i in range(header_end - 1, -1, -1):
        if lines[i].strip():
            last_header_line = i + 1
            break

    # Copy header lines (excluding trailing blank lines)
    new_lines = lines[:last_header_line]

    # Only collapse blank lines between header and first code line
    # After first code line, preserve all blank lines
    blank_count = 0
    found_first_code_line = False
    blank_line_start_idx = last_header_line

    for i in range(last_header_line, len(lines)):
        line = lines[i]
        is_blank = line.strip() == ""

        if is_blank:
            if blank_count == 0:
                blank_line_start_idx = i
            blank_count += 1
            if not found_first_code_line:
                # Before first code line: will handle after we see what comes next
                pass
            else:
                # After first code line: preserve all blank lines
                new_lines.append(line)
        else:
            # Non-blank line found
            if not found_first_code_line and blank_count > 0:
                # Check if this line is a class or function definition
                # PEP 8 requires 2 blank lines before top-level class/function
                # definitions
                # Preserve up to 2 blank lines before a class/function def,
                # else collapse to 1 blank line.
                target_blank_count = min(2, blank_count) if _is_class_or_function_def(line) else 1

                # Append the appropriate number of blank lines. target_blank_count
                # is always <= blank_count (min(2, blank_count) or 1 when
                # blank_count > 0), and i == blank_line_start_idx + blank_count,
                # so blank_line_start_idx + j < i holds for every j in range.
                for j in range(target_blank_count):
                    new_lines.append(lines[blank_line_start_idx + j])

            blank_count = 0
            found_first_code_line = True
            new_lines.append(line)

    return "".join(new_lines)


class ExcessiveBlankLinesCheck(BaseCheck):
    """Check for excessive blank lines after module headers."""

    @property
    def check_id(self) -> str:
        return "excessive-blank-lines"

    @property
    def error_code(self) -> str:
        return "TRI002"

    def get_prefilter_pattern(self) -> list[str] | None:
        """Returns None because all files should be checked."""
        return None

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        file_violations = check_file_violations(source, tree)
        if not file_violations:
            return []

        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
        violations = []
        for fv in file_violations:
            if fv.anchor_line in ignored_lines:
                continue
            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=fv.line,
                    col=0,
                    message=fv.message,
                    fixable=True,
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
        if not violations:
            return False

        # Recompute independently rather than trusting the passed
        # violations, same as misplaced_comment.fix(): a stale or
        # caller-supplied violations list must never cause an ignored blank
        # run to be collapsed anyway.
        file_violations = check_file_violations(source, tree)
        if not file_violations:
            return False

        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
        if any(fv.anchor_line in ignored_lines for fv in file_violations):
            return False

        try:
            fixed_content = fix_file_content(source, tree)

            # Write back to file
            atomic_write_text(filepath, fixed_content, encoding)
        except OSError:
            # Debug-only: mark_fix_failed() below already reports this
            # cleanly as [FIX FAILED] — an ERROR-level .exception() call
            # here would just leak a redundant raw traceback onto the
            # user's stderr by default (nothing in this codebase configures
            # logging, so Python's own lastResort handler prints WARNING+
            # straight to stderr).
            logger.debug("Failed to write %s", filepath, exc_info=True)
            for v in violations:
                mark_fix_failed(v)
            return False
        else:
            return True
