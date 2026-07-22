"""Auto-fix implementation for TRI005 redundant assignments."""

from __future__ import annotations

import ast
import logging
import re
from typing import TYPE_CHECKING, TypedDict, cast

from pre_commit_hooks.ast_checks._base import Violation, atomic_write_text, byte_col_to_char_col, mark_fix_failed

from .semantic import exceeds_line_length_when_inlined, is_safe_to_splice_into_fstring

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
    # Byte-offset span of the enclosing f-string replacement field (see
    # analysis.UsageInfo.fstring_field_span) when the single use is a
    # string literal spliced directly into an f-string's text (issue #72).
    # None for every other fix (the overwhelming majority).
    fstring_field_start_col: int | None
    fstring_field_end_col: int | None


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

        # A field span is recorded whenever the use is a whole f-string
        # replacement field, regardless of the RHS's type (see
        # analysis.UsageInfo.fstring_field_span). Only a string-literal RHS
        # needs the splice path below — falls through to the ordinary
        # inlining path for any other RHS type (e.g. a Name or a number),
        # which needs no requoting and so isn't buggy as-is.
        fstring_start = fix_data.get("fstring_field_start_col")
        fstring_end = fix_data.get("fstring_field_end_col")
        fstring_literal_value = (
            _try_literal_eval_str(rhs_source) if fstring_start is not None and fstring_end is not None else None
        )
        if fstring_literal_value is not None:
            assert fstring_start is not None
            assert fstring_end is not None
            # Once it's confirmed a string literal, the splice is the
            # *only* correct fix for it — declining here (rather than
            # falling through to the generic path below) is what avoids
            # issue #72's original bug: the generic path's plain text
            # substitution would re-quote this same value inside `{}`.
            if not is_safe_to_splice_into_fstring(fstring_literal_value, encoding):
                continue
            if not _apply_fstring_splice_fix(
                source_lines, use_line_idx, fstring_start, fstring_end, fstring_literal_value
            ):
                continue
            source_lines[assign_line_idx] = ""
            removed_lines.add(assign_line_idx)
            fixed_any = True
            applied_violations.append(violation)
            continue

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


def _try_literal_eval_str(rhs_source: str) -> str | None:
    """`rhs_source`'s decoded value if it's a string-literal expression,
    None otherwise (not a literal at all, e.g. a Name/Attribute/Call, or a
    literal of some other type). Deliberately doesn't check splice safety
    (see semantic.is_safe_to_splice_into_fstring) — the caller must treat
    "is a string literal" and "is safe to splice" as separate questions:
    once RHS is confirmed a string literal, the splice is the *only*
    correct fix (see apply_fixes), so an unsafe one must be declined
    outright, not silently swapped for the generic (buggy, re-quoting)
    path used for non-string RHS types.
    """
    try:
        value = ast.literal_eval(rhs_source)
    except ValueError, SyntaxError:
        return None
    return value if isinstance(value, str) else None


def _apply_fstring_splice_fix(
    source_lines: list[str],
    use_line_idx: int,
    fstring_start: int,
    fstring_end: int,
    literal_value: str,
) -> bool:
    """Replaces an entire f-string replacement field (e.g. `{org}`, braces
    included) with `literal_value` spliced directly into the surrounding
    text — mutates `source_lines` in place. Returns False (and leaves
    `source_lines` untouched) if inlining would exceed the line length.
    """
    use_line = source_lines[use_line_idx]
    start_char = byte_col_to_char_col(use_line, fstring_start)
    end_char = byte_col_to_char_col(use_line, fstring_end)

    if exceeds_line_length_when_inlined(use_line[start_char:end_char], literal_value, use_line):
        return False

    source_lines[use_line_idx] = use_line[:start_char] + literal_value + use_line[end_char:]
    return True
