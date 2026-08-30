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
    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[object, ...]]: ...

    def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None: ...

    def analysis_transaction(self) -> contextlib.AbstractContextManager[None]: ...

    def finalize(self, filepath: Path, source: str) -> None: ...


def _open_or_raise(session: RedundancySession, filepath: Path, content: str) -> frozenset[tuple[object, ...]]:
    try:
        return session.open_or_update(filepath, content)
    except LSPError as error:
        raise CheckUnavailableError(_SESSION_LOST_HINT) from error


class RedundantConversion:
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
    candidates = [candidate for candidate in all_candidates if candidate.line not in ignored_lines]
    if not candidates:
        return []

    source_lines = split_lines_like_ast(source)

    redundant: list[RedundantConversion] = []
    with session.analysis_transaction():
        try:
            baseline = _open_or_raise(session, filepath, source)
            candidates_with_hovers: list[tuple[Candidate, str]] = []
            for candidate in candidates:
                line_text = source_lines[candidate.line - 1]
                arg_end_char = byte_col_to_char_col(line_text, candidate.arg_end_col)
                hover_char = len(line_text[: arg_end_char - 1].encode("utf-16-le")) // 2
                hover_text = session.hover(filepath, candidate.line - 1, hover_char)
                if not hover_passes_gate(hover_text, level, candidate.constructor):
                    continue
                assert hover_text is not None

                if candidate.wrapped_in_len and not is_exact_match(hover_text, candidate.constructor):
                    continue

                if candidate.in_equality_comparison and is_purepath_hover(hover_text):
                    continue

                candidates_with_hovers.append((candidate, hover_text))

            for candidate, hover_text in candidates_with_hovers:
                line_text = source_lines[candidate.line - 1]
                modified_text = _build_modified_text(source_lines, candidate)
                after = _open_or_raise(session, filepath, modified_text)
                if after - baseline:
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
            session.finalize(filepath, source)

    return redundant


def _build_modified_text(source_lines: list[str], candidate: Candidate) -> str:
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
