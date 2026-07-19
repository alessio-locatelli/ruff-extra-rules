"""Auto-fix implementation for TRI005 redundant assignments."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, TypedDict, cast

from pre_commit_hooks.ast_checks._base import Violation, atomic_write_text, byte_col_to_char_col

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
    """Apply auto-fixes for redundant assignment violations.

    This is a VERY conservative implementation that only fixes violations marked
    as fixable by strict semantic analysis. It only handles the simplest cases:
    - Not in loops or control flow
    - Immediate single use
    - Simple RHS (constants, names, single-level attributes)
    - Short variable names
    - Very low semantic value

    Args:
        filepath: Path to file to fix
        violations: List of violations to fix
        source: Original source code
        encoding: Encoding to write the file back with

    Returns:
        True if fixes were successfully applied, False otherwise
    """
    # Filter to only fixable violations
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
    removed_lines: set[int] = set()  # Track which lines we removed

    for violation in fixable_violations:
        # Extract fix data. use_line/use_col are None when the violation
        # didn't have exactly one use (see RedundantAssignmentCheck.check) —
        # that's the only shape this can safely auto-fix.
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

        # Get the RHS expression
        rhs_source = fix_data["rhs_source"].strip()
        var_name = fix_data["var_name"]

        # Check if inlining is safe
        if not _can_safely_inline(var_name, rhs_source, use_line_idx, source_lines):
            continue

        # Perform the inline replacement using word boundaries
        use_line = source_lines[use_line_idx]

        # Use regex with word boundaries to replace only the exact variable
        # This prevents 'x' from matching 'max' or 'index'
        pattern = r"\b" + re.escape(var_name) + r"\b"

        # Find all matches to verify we're replacing the right one
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

        # Replace the specific occurrence
        before = use_line[: target_match.start()]
        after = use_line[target_match.end() :]
        new_use_line = before + rhs_source + after

        source_lines[use_line_idx] = new_use_line

        # Remove the assignment line
        source_lines[assign_line_idx] = ""
        removed_lines.add(assign_line_idx)

        fixed_any = True

    if fixed_any:
        # Clean up blank lines only around removed assignments
        _cleanup_blank_lines_around_removals(source_lines, removed_lines)

        # Write the fixed source back to file
        new_source = "".join(source_lines)
        try:
            atomic_write_text(filepath, new_source, encoding)
        except OSError:
            logger.exception("Failed to write %s", filepath)
            return False
        return True

    return False


def _cleanup_blank_lines_around_removals(source_lines: list[str], removed_lines: set[int]) -> None:
    """Remove excessive blank lines only around lines we removed.

    This ensures we don't affect blank lines elsewhere in the file.

    Args:
        source_lines: List of source lines (modified in place)
        removed_lines: Set of line indices that were removed (set to "")
    """
    # For each removed line, check if it creates excessive blank lines
    for removed_idx in sorted(removed_lines):
        # Count consecutive blank lines around this removal
        # Check upward
        blank_above = 0
        idx = removed_idx - 1
        while idx >= 0 and source_lines[idx].strip() == "":
            blank_above += 1
            idx -= 1

        # Check downward
        blank_below = 0
        idx = removed_idx + 1
        while idx < len(source_lines) and source_lines[idx].strip() == "":
            blank_below += 1
            idx += 1

        # Total blanks including the removed line
        total_blanks = blank_above + 1 + blank_below  # +1 for removed line itself

        # If we have 3+ consecutive blanks, reduce to 2
        if total_blanks >= 3:
            # Keep at most 2 blank lines total
            # Strategy: keep 1 above, 1 below, remove the rest

            # Remove excess blanks from above
            if blank_above > 1:
                for i in range(removed_idx - blank_above, removed_idx - 1):
                    source_lines[i] = ""

            # Remove excess blanks from below
            if blank_below > 1:
                for i in range(removed_idx + 2, removed_idx + 1 + blank_below):
                    source_lines[i] = ""


def _can_safely_inline(
    var_name: str,
    rhs_source: str,
    use_line_idx: int,
    source_lines: list[str],
) -> bool:
    """Check if inlining is safe (no line length violations, comments intact, etc.).

    Args:
        var_name: Variable name being inlined
        rhs_source: RHS source code to inline
        use_line_idx: Line index where variable is used (0-indexed)
        source_lines: List of source code lines

    Returns:
        True if safe to inline
    """
    if use_line_idx < 0 or use_line_idx >= len(source_lines):
        return False

    # Get the line where variable is used
    use_line = source_lines[use_line_idx]

    # Check if new line would exceed reasonable length (79 chars, PEP 8 default)
    if exceeds_line_length_when_inlined(var_name, rhs_source, use_line):
        return False

    # Check if the RHS expression contains newlines (multiline expressions)
    # These are complex and shouldn't be auto-fixed
    return not ("\n" in rhs_source or "\r" in rhs_source)
