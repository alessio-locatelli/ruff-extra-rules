"""Ties AST candidate detection (`candidates.py`), confidence tiering
(`confidence.py`), and `ty`'s own redundancy decision (`session.py`)
together for TRI006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pre_commit_hooks.ast_checks._base import byte_col_to_char_col, split_lines_like_ast

from .candidates import find_candidates
from .confidence import eligible_constructors, hover_passes_gate

if TYPE_CHECKING:
    import ast
    from pathlib import Path

    from .candidates import Candidate
    from .confidence import ConfidenceLevel


class RedundancySession(Protocol):
    """The subset of `TySession`'s own interface `decide_candidates()`
    actually depends on -- a Protocol (structural typing) rather than the
    concrete class, so a test double (see `tests/redundant_type_conversion/
    _helpers.py`'s `FakeSession`) can stand in for a real `ty` session
    without inheriting from it.
    """

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[object, ...]]: ...

    def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None: ...

    def close_file(self, filepath: Path) -> None: ...


class RedundantConversion:
    """One candidate `ty` actually confirmed is redundant, plus everything
    `__init__.py` needs to build a `Violation` from it without re-deriving
    anything already computed here.
    """

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
    """Every syntactic candidate in `tree` that `ty` confirms is actually
    redundant at `level` — see `candidates.find_candidates` for what makes
    a candidate at all, `confidence.hover_passes_gate` for the cheap
    pre-filter, and this function's own body for the synthetic
    rewrite-and-recheck that makes the final call.
    """
    eligible = eligible_constructors(level)
    candidates = [candidate for candidate in find_candidates(tree, eligible) if candidate.line not in ignored_lines]
    if not candidates:
        return []

    source_lines = split_lines_like_ast(source)

    redundant: list[RedundantConversion] = []
    for candidate in candidates:
        # Restore the document to its pristine state before every hover:
        # a previous candidate's own recheck below leaves the document at
        # that candidate's rewrite, not the original source, and hovering
        # against a rewritten document can report a different type for a
        # later candidate than the file's own real, unmodified state has
        # (e.g. an earlier removed conversion changing what a later,
        # unrelated line's flow-narrowed type looks like) -- or, if two
        # candidates share a line, misalign this candidate's own column
        # against text a previous rewrite already shifted. Also serves as
        # this candidate's own recheck baseline, so it's never a wasted
        # call even on the very first candidate.
        baseline = session.open_or_update(filepath, source)
        line_text = source_lines[candidate.line - 1]
        # A position one character before the argument's own end, so
        # hover lands inside the argument's own text rather than on
        # whatever follows it (the closing paren, a comma, ...). Computed
        # in two steps -- byte offset to *character* index first, then
        # one character back, then character index to UTF-16 -- rather
        # than subtracting 1 directly in UTF-8 *byte* space: arg_end_col
        # is always a valid byte boundary (a real ast.col_offset), but
        # arg_end_col - 1 is not whenever the argument's own last
        # character is multi-byte in UTF-8 (e.g. `str(é)`), which used to
        # slice mid-character and raise UnicodeDecodeError on otherwise
        # ordinary source.
        arg_end_char = byte_col_to_char_col(line_text, candidate.arg_end_col)
        hover_char = len(line_text[: arg_end_char - 1].encode("utf-16-le")) // 2
        hover_text = session.hover(filepath, candidate.line - 1, hover_char)
        if not hover_passes_gate(hover_text, level, candidate.constructor):
            continue

        modified_text = _build_modified_text(source_lines, candidate)
        after = session.open_or_update(filepath, modified_text)
        if after - baseline:
            # Removing the conversion introduced a diagnostic that wasn't
            # there before -- it's still doing real, type-relevant work.
            continue

        assert hover_text is not None  # hover_passes_gate() already rejected None/empty above
        redundant.append(
            RedundantConversion(
                candidate=candidate,
                line=candidate.line,
                col=byte_col_to_char_col(line_text, candidate.call_start_col),
                argument_type=hover_text,
            )
        )

    session.close_file(filepath)
    return redundant


def _build_modified_text(source_lines: list[str], candidate: Candidate) -> str:
    """`source_lines` with `candidate`'s own wrapping call syntax —
    `constructor(` ... `)` — spliced out, leaving its argument's own source
    untouched (`list(bar)` becomes `bar`). Operates on UTF-8 bytes,
    matching `ast.col_offset`'s own unit (see
    `ast_checks._base.byte_col_to_char_col`'s docstring), so a non-ASCII
    character earlier on the line can't shift the splice onto the wrong
    bytes.

    Only ever touches the single physical line `candidate.line`:
    `candidates.find_candidates` only yields a candidate whose entire call
    (open paren through close paren) sits on that one line, so every other
    line's own numbering is left completely intact — required for a
    before/after diagnostic comparison at an unrelated position elsewhere
    in the file to mean anything.
    """
    line = source_lines[candidate.line - 1]
    line_bytes = line.encode("utf-8")
    new_line_bytes = (
        line_bytes[: candidate.call_start_col]
        + line_bytes[candidate.arg_start_col : candidate.arg_end_col]
        + line_bytes[candidate.call_end_col :]
    )
    new_lines = list(source_lines)
    new_lines[candidate.line - 1] = new_line_bytes.decode("utf-8")
    return "".join(new_lines)
