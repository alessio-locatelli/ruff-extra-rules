"""Auto-fix implementation for TRI005 redundant assignments."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, TypedDict, cast

from pre_commit_hooks.ast_checks._base import Violation, atomic_write_text, byte_col_to_char_col, mark_fix_failed

from .semantic import exceeds_line_length_when_inlined

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("redundant_assignment")


class RedundantAssignmentFixData(TypedDict):
    """Constructed by RedundantAssignmentCheck.check(), read back here by
    apply_fixes(). Must stay JSON-serializable (no AST nodes/lifecycle
    objects) — detect_redundancy() only returns a pattern for single-use
    lifecycles, so use_line/use_col are always concrete ints, never absent.
    """

    pattern: str
    assign_line: int
    var_name: str
    rhs_source: str
    use_line: int
    use_col: int


def apply_fixes(
    filepath: Path,
    violations: list[Violation],
    source: str,
    encoding: str = "utf-8",
) -> bool:
    """A VERY conservative implementation that only fixes violations marked as
    fixable by strict semantic analysis. It only handles the simplest cases:
    - Not in loops or control flow
    - Immediate single use
    - Simple RHS (constants, names, single-level attributes)
    - Short variable names
    - Very low semantic value
    """
    fixable_violations = [v for v in violations if v.fixable]

    if not fixable_violations:
        return False

    source_lines = source.splitlines(keepends=True)

    # Sort by (use_line, use_col) descending: when two fixable assignments
    # are inlined on the same use line, the rightmost one must be replaced
    # first, since a replacement's length can differ from the variable name
    # it replaces and shift every column after it on that line. Violations
    # missing use_line (filtered out in the loop below) sort last.
    def _use_position(v: Violation) -> tuple[int, int]:
        raw_fix_data = v.fix_data
        if not raw_fix_data or raw_fix_data.get("use_line") is None:
            return (-1, -1)
        fix_data = cast("RedundantAssignmentFixData", raw_fix_data)
        return (fix_data["use_line"], fix_data["use_col"])

    fixable_violations.sort(key=_use_position, reverse=True)

    fixed_any = False
    applied_violations: list[Violation] = []
    removed_lines: set[int] = set()

    for violation in fixable_violations:
        # use_line/use_col are None when the violation didn't have exactly
        # one use (see RedundantAssignmentCheck.check) — that's the only
        # shape this can safely auto-fix.
        raw_fix_data = violation.fix_data
        if not raw_fix_data or raw_fix_data.get("use_line") is None:
            continue
        fix_data = cast("RedundantAssignmentFixData", raw_fix_data)

        assign_line_idx = fix_data["assign_line"] - 1
        use_line_idx = fix_data["use_line"] - 1
        use_col = fix_data["use_col"]

        if assign_line_idx < 0 or assign_line_idx >= len(source_lines):
            continue
        if use_line_idx < 0 or use_line_idx >= len(source_lines):
            continue

        rhs_source = fix_data["rhs_source"].strip()
        var_name = fix_data["var_name"]

        if not _can_safely_inline(var_name, rhs_source, use_line_idx, source_lines):
            continue

        use_line = source_lines[use_line_idx]

        # Word boundaries so 'x' doesn't match inside 'max' or 'index'.
        pattern = r"\b" + re.escape(var_name) + r"\b"

        matches = tuple(re.finditer(pattern, use_line))

        # use_col is a UTF-8 byte offset (from ast.col_offset); match.start()
        # is a character offset, so convert before comparing.
        use_char_col = byte_col_to_char_col(use_line, use_col)

        # Usually resolves to one of the regex matches directly. But a
        # chained assignment's use line can coincide with another
        # violation's assign line (e.g. `x = 1; y = x; return y`): y's fix
        # is applied first and blanks the `y = x` line, so x's own use on
        # that same line is gone by the time x's fix runs. Skip rather than
        # inline into now-unrelated text.
        target_match = next((m for m in matches if m.start() == use_char_col), None)
        if target_match is None:
            continue

        before = use_line[: target_match.start()]
        after = use_line[target_match.end() :]
        new_use_line = before + rhs_source + after

        source_lines[use_line_idx] = new_use_line

        source_lines[assign_line_idx] = ""
        removed_lines.add(assign_line_idx)

        fixed_any = True
        applied_violations.append(violation)

    if fixed_any:
        _cleanup_blank_lines_around_removals(source_lines, removed_lines)

        new_source = "".join(source_lines)
        try:
            atomic_write_text(filepath, new_source, encoding)
        except OSError:
            # Debug-only: mark_fix_failed() below already reports this
            # cleanly as [FIX FAILED] — an ERROR-level .exception() call
            # here would just leak a redundant raw traceback onto the
            # user's stderr by default (nothing in this codebase configures
            # logging, so Python's own lastResort handler prints WARNING+
            # straight to stderr).
            logger.debug("Failed to write %s", filepath, exc_info=True)
            for v in applied_violations:
                mark_fix_failed(v)
            return False
        return True

    return False


def _cleanup_blank_lines_around_removals(source_lines: list[str], removed_lines: set[int]) -> None:
    """Modifies `source_lines` in place. Only touches blank lines adjacent to a
    `removed_lines` entry, leaving the rest of the file's blank lines untouched.
    """
    for removed_idx in sorted(removed_lines):
        blank_above = 0
        idx = removed_idx - 1
        while idx >= 0 and source_lines[idx].strip() == "":
            blank_above += 1
            idx -= 1

        blank_below = 0
        idx = removed_idx + 1
        while idx < len(source_lines) and source_lines[idx].strip() == "":
            blank_below += 1
            idx += 1

        total_blanks = blank_above + 1 + blank_below  # +1 for removed line itself

        # 3+ consecutive blanks: collapse to at most 1 above, 1 below.
        if total_blanks >= 3:
            if blank_above > 1:
                for i in range(removed_idx - blank_above, removed_idx - 1):
                    source_lines[i] = ""

            if blank_below > 1:
                for i in range(removed_idx + 2, removed_idx + 1 + blank_below):
                    source_lines[i] = ""


def _can_safely_inline(
    var_name: str,
    rhs_source: str,
    use_line_idx: int,
    source_lines: list[str],
) -> bool:
    if use_line_idx < 0 or use_line_idx >= len(source_lines):
        return False

    use_line = source_lines[use_line_idx]

    if exceeds_line_length_when_inlined(var_name, rhs_source, use_line):
        return False

    # Multiline RHS expressions are complex and shouldn't be auto-fixed.
    return not ("\n" in rhs_source or "\r" in rhs_source)
