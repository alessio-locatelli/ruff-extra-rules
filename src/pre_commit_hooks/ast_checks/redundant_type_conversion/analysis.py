"""Ties AST candidate detection (`candidates.py`), confidence tiering
(`confidence.py`), and `ty`'s own redundancy decision (`session.py`)
together for TRI006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pre_commit_hooks._lsp import LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError, byte_col_to_char_col, split_lines_like_ast

from .candidates import find_candidates
from .confidence import eligible_constructors, hover_passes_gate, is_exact_match, is_purepath_hover

if TYPE_CHECKING:
    import ast
    from pathlib import Path

    from .candidates import Candidate
    from .confidence import ConfidenceLevel

_SESSION_LOST_HINT = (
    "redundant-type-conversion (TRI006) lost its connection to `ty` mid-run (the `ty server` process likely "
    "crashed or exited). Re-run to start a fresh session; if this keeps happening, try a different installed "
    "`ty` version. See docs/rules/redundant-type-conversion.md."
)


class RedundancySession(Protocol):
    """The subset of `TySession` `decide_candidates()` depends on -- lets a fake stand in without inheriting it."""

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[object, ...]]: ...

    def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None: ...

    def close_file(self, filepath: Path) -> None: ...


def _open_or_raise(session: RedundancySession, filepath: Path, content: str) -> frozenset[tuple[object, ...]]:
    try:
        return session.open_or_update(filepath, content)
    except LSPError as error:
        raise CheckUnavailableError(_SESSION_LOST_HINT) from error


class RedundantConversion:
    """One candidate `ty` confirmed is redundant, plus everything `__init__.py` needs to build a `Violation`."""

    __slots__ = ("argument_type", "candidate", "col", "line")

    def __init__(self, candidate: Candidate, *, line: int, col: int, argument_type: str) -> None:
        self.candidate = candidate
        self.line = line
        self.col = col
        self.argument_type = argument_type


def decide_candidates(
    session: RedundancySession,
    filepath: Path,
    tree: ast.Module,
    source: str,
    *,
    level: ConfidenceLevel,
    ignored_lines: set[int],
) -> list[RedundantConversion]:
    """Every candidate in `tree` that `ty` confirms is actually redundant at `level`."""
    eligible = eligible_constructors(level)
    candidates = [candidate for candidate in find_candidates(tree, eligible) if candidate.line not in ignored_lines]
    if not candidates:
        return []

    source_lines = split_lines_like_ast(source)

    redundant: list[RedundantConversion] = []
    try:
        for candidate in candidates:
            # Restores the pristine source before every hover: a previous
            # candidate's own rewrite below would otherwise still be open,
            # which can change what this candidate's own hover reports. Also
            # doubles as this candidate's own recheck baseline.
            baseline = _open_or_raise(session, filepath, source)
            line_text = source_lines[candidate.line - 1]
            # One character before the argument's own end, in char (not byte)
            # space first -- arg_end_col - 1 in byte space can land mid-
            # character for a multi-byte final character (e.g. `str(é)`).
            arg_end_char = byte_col_to_char_col(line_text, candidate.arg_end_col)
            hover_char = len(line_text[: arg_end_char - 1].encode("utf-16-le")) // 2
            hover_text = session.hover(filepath, candidate.line - 1, hover_char)
            if not hover_passes_gate(hover_text, level, candidate.constructor):
                continue
            assert hover_text is not None  # hover_passes_gate() already rejected None/empty above

            if candidate.wrapped_in_len and not is_exact_match(hover_text, candidate.constructor):
                # See ADR-0035's `len()` sink exclusion.
                continue

            if candidate.in_equality_comparison and is_purepath_hover(hover_text):
                # See ADR-0035's Path-vs-str comparison exclusion.
                continue

            modified_text = _build_modified_text(source_lines, candidate)
            after = _open_or_raise(session, filepath, modified_text)
            if after - baseline:
                # Removing the conversion introduced a diagnostic that wasn't there before.
                continue

            redundant.append(
                RedundantConversion(
                    candidate=candidate,
                    line=candidate.line,
                    col=byte_col_to_char_col(line_text, candidate.call_start_col),
                    argument_type=hover_text,
                )
            )
    finally:
        # Closes even on an unexpected raise, or it leaks for the rest of this process-wide session.
        session.close_file(filepath)

    return redundant


def _build_modified_text(source_lines: list[str], candidate: Candidate) -> str:
    """`source_lines` with `candidate`'s own wrapping call spliced out (`list(bar)` becomes `bar`).

    Operates in UTF-8 byte space, matching `ast.col_offset`. Only touches
    `candidate.line` -- see ADR-0035's "Detection method" for why every
    other line's numbering must stay intact.
    """
    line = source_lines[candidate.line - 1]
    line_bytes = line.encode("utf-8")
    new_line_bytes = (
        line_bytes[: candidate.call_start_col]
        + line_bytes[candidate.arg_start_col : candidate.arg_end_col]
        + line_bytes[candidate.call_end_col :]
    )
    new_lines = list(source_lines)  # pytriage: ignore=TRI006 -- copy, not redundant: avoids aliasing the caller's list
    new_lines[candidate.line - 1] = new_line_bytes.decode("utf-8")
    return "".join(new_lines)
