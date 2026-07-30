"""Ties AST candidate detection (`candidates.py`), confidence tiering
(`confidence.py`), and `ty`'s own redundancy decision (`session.py`)
together for TR6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pre_commit_hooks._lsp import LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError, byte_col_to_char_col, split_lines_like_ast

from .confidence import hover_passes_gate, is_exact_match, is_purepath_hover

if TYPE_CHECKING:
    import contextlib
    from pathlib import Path

    from .candidates import Candidate
    from .confidence import ConfidenceLevel

_SESSION_LOST_HINT = (
    "redundant-type-conversion (TR6) lost its connection to `ty` mid-run (the `ty server` process likely "
    "crashed or exited). Re-run to start a fresh session; if this keeps happening, try a different installed "
    "`ty` version. See docs/rules/redundant-type-conversion.md."
)


class RedundancySession(Protocol):
    """The subset of `TySession` `decide_candidates()` depends on -- lets a fake stand in without inheriting it."""

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[object, ...]]: ...

    def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None: ...

    def analysis_transaction(self) -> contextlib.AbstractContextManager[None]: ...

    def finalize(self, filepath: Path, source: str) -> None:
        """Ends this candidate-analysis pass over `filepath`.

        A short-lived, per-invocation session discards it outright. A
        persistent session (see ADR-0041) instead re-syncs it back to
        `source` -- its real, on-disk content, since the per-candidate
        rewrite-and-diff loop above may have left a synthetic variant open
        -- and keeps it tracked, so `ty`'s own cross-file dependency
        tracking keeps seeing it as a dependent of whatever it imports
        across future, separate invocations sharing that same session.
        """
        ...


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
    all_candidates: list[Candidate],
    source: str,
    *,
    level: ConfidenceLevel,
    ignored_lines: set[int],
) -> list[RedundantConversion]:
    """Every one of `all_candidates` (already found by `find_candidates()` --
    see that function's own docstring for what qualifies) that `ty` confirms
    is actually redundant.

    Takes the already-found candidate list rather than re-deriving it from
    `tree` itself: `find_candidates()` re-walks the whole tree, and the only
    caller (`RedundantTypeConversionCheck.check()`) already has to run it
    once anyway (to know whether tokenizing `source` for `ignored_lines` is
    even worth doing) -- running it a second time here just to filter by
    `ignored_lines` would repeat that whole-tree walk for nothing new.
    """
    candidates = [candidate for candidate in all_candidates if candidate.line not in ignored_lines]
    if not candidates:
        return []

    source_lines = split_lines_like_ast(source)

    redundant: list[RedundantConversion] = []
    with session.analysis_transaction():
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
            # Runs even on an unexpected raise, or a persistent session leaks
            # this file open (and possibly still mid-rewrite) for the rest of
            # its own lifetime.
            session.finalize(filepath, source)

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
    new_lines = list(source_lines)  # pytriage: TR6 -- copy, not redundant: avoids aliasing the caller's list
    new_lines[candidate.line - 1] = new_line_bytes.decode("utf-8")
    return "".join(new_lines)
