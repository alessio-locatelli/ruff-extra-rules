from __future__ import annotations

import ast
import logging
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
    find_ignored_lines,
    find_ignored_lines_and_pytriage_comments,
    ignore_pattern_for,
    record_suppression_usage_if_ignored,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("excessive_blank_lines")

IGNORE_PATTERN = ignore_pattern_for("TR2")


@dataclass(frozen=True, slots=True)
class _BlankRunViolation:
    line: int
    anchor_line: int
    message: str


def _format_message(blank_count: int, target: int) -> str:
    return (
        f"Excessive blank lines ({blank_count}) should be collapsed to {target}. "
        "Add '# pytriage: TR2' to the line following the blank run "
        "to suppress."
    )


def find_module_header_end(lines: list[str], tree: ast.Module) -> int:
    start_idx = 0

    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        start_idx = tree.body[0].end_lineno or 0

    for i in range(start_idx, len(lines)):
        stripped = lines[i].strip()

        if not stripped or stripped.startswith("#"):
            continue

        return i

    return len(lines)


def check_file_violations(source: str, tree: ast.Module) -> list[_BlankRunViolation]:
    lines = source.splitlines(keepends=True)

    if not lines:
        return []

    violations = []
    header_end = find_module_header_end(lines, tree)

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
            if not found_first_code_line and blank_count >= 2 and start_blank is not None:
                anchor_line = i + 1
                if _is_class_or_function_def(line):
                    if blank_count > 2:
                        violations.append(
                            _BlankRunViolation(
                                line=start_blank + 1,
                                anchor_line=anchor_line,
                                message=_format_message(blank_count, target=2),
                            )
                        )
                else:
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

    last_header_line = 0
    for i in range(header_end - 1, -1, -1):
        if lines[i].strip():
            last_header_line = i + 1
            break

    # Excludes the header's own trailing blank lines.
    new_lines = lines[:last_header_line]

    # Blank lines are only collapsed between the header and the first code
    # line; once the first code line is seen, every blank line is preserved.
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
                # Handled once we see what comes next (below).
                pass
            else:
                new_lines.append(line)
        else:
            if not found_first_code_line and blank_count > 0:
                # PEP 8 requires 2 blank lines before top-level class/function definitions.
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
    __slots__ = ()

    @property
    def check_id(self) -> str:
        return "excessive-blank-lines"

    @property
    def error_code(self) -> str:
        return "TR2"

    def get_prefilter_pattern(self) -> list[str] | None:
        return None

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        file_violations = check_file_violations(source, tree)
        if not file_violations:
            return CheckResult()

        ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(source, IGNORE_PATTERN)
        violations = []
        suppression_usages: list[SuppressionUsage] = []
        for fv in file_violations:
            if record_suppression_usage_if_ignored(
                suppression_usages,
                comments,
                ignored_lines=ignored_lines,
                format_suppressed=format_suppressed,
                check_id=self.check_id,
                error_code=self.error_code,
                candidate_lines=(fv.anchor_line,),
            ):
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

        return CheckResult(violations, suppression_usages)

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> FixResult:
        if not violations:
            return FixResult.for_violations(violations, FixOutcome.DECLINED)

        # Recompute independently rather than trusting the passed
        # violations, same as misplaced_comment.fix(): a stale or
        # caller-supplied violations list must never cause an ignored blank
        # run to be collapsed anyway.
        file_violations = check_file_violations(source, tree)
        if not file_violations:
            return FixResult.for_violations(violations, FixOutcome.DECLINED)

        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
        if any(fv.anchor_line in ignored_lines for fv in file_violations):
            return FixResult.for_violations(violations, FixOutcome.DECLINED)

        fixed_content = fix_file_content(source, tree)
        try:
            atomic_write_text(filepath, fixed_content, encoding, source)
        except OSError:
            # Debug-only: the returned outcome already reports this
            # cleanly as [FIX FAILED] — an ERROR-level .exception() call
            # here would just leak a redundant raw traceback onto the
            # user's stderr by default (nothing in this codebase configures
            # logging, so Python's own lastResort handler prints WARNING+
            # straight to stderr).
            logger.debug("Failed to write %s", filepath, exc_info=True)
            return FixResult.for_violations(violations, FixOutcome.FAILED)
        else:
            return FixResult.for_violations(violations, FixOutcome.APPLIED)
