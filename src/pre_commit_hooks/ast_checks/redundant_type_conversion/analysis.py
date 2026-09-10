from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from pre_commit_hooks._lsp import LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError, byte_col_to_char_col, split_lines_like_ast

from .confidence import (
    MUTABLE_CONSTRUCTORS,
    hover_passes_gate,
    is_comparison_safe_hover,
    is_exact_match,
    is_purepath_hover,
)

if TYPE_CHECKING:
    import contextlib
    from pathlib import Path

    from .candidates import Candidate
    from .confidence import ConfidenceLevel

logger = logging.getLogger("ast_checks")

_SESSION_LOST_HINT = (
    "redundant-type-conversion (TR6) lost its connection to `ty` mid-run (the `ty server` process likely "
    "crashed or exited). Re-run to start a fresh session; if this keeps happening, try a different installed "
    "`ty` version. See docs/rules/redundant-type-conversion.md."
)

_OUT_OF_ROOT_HINT = (
    "redundant-type-conversion (TR6) is skipping %s: it lies outside the `ty` session's own project root, so "
    "`ty` cannot reliably report diagnostics for it -- treating every conversion in it as verified-redundant "
    "would be unsound. Run this check from inside the project that owns the file. "
    "See docs/rules/redundant-type-conversion.md."
)

_EXCLUDED_FROM_TY_HINT = (
    "redundant-type-conversion (TR6) is skipping %s: `ty` reported no diagnostics for it, even for two "
    "obviously-invalid probe statements of unrelated diagnostic rules, meaning `ty`'s own project "
    "configuration (e.g. `[tool.ty.src] exclude`) has this file outside its checked scope -- treating "
    "every conversion in it as verified-redundant would be unsound. See docs/rules/redundant-type-conversion.md."
)

_DIAGNOSTICS_PROBE = (
    "\n_pre_commit_hooks_tr6_untrusted_diagnostics_probe: None = 5\n"
    "\n\ndef _pre_commit_hooks_tr6_untrusted_diagnostics_probe_fn(x: None) -> None:\n    pass\n\n\n"
    "_pre_commit_hooks_tr6_untrusted_diagnostics_probe_fn(5)\n"
)


class RedundancySession(Protocol):
    def is_within_root(self, filepath: Path, /) -> bool: ...

    def open_or_update(self, filepath: Path, content: str, /) -> frozenset[tuple[object, ...]]: ...

    def hover(self, filepath: Path, line0: int, char_utf16: int, /) -> str | None: ...

    def analysis_transaction(self) -> contextlib.AbstractContextManager[None]: ...

    def finalize(self, filepath: Path, source: str, /) -> None: ...


def _open_or_raise(session: RedundancySession, filepath: Path, content: str) -> frozenset[tuple[object, ...]]:
    try:
        return session.open_or_update(filepath, content)
    except LSPError as error:
        raise CheckUnavailableError(_SESSION_LOST_HINT) from error


def _is_within_root_or_raise(session: RedundancySession, filepath: Path) -> bool:
    try:
        return session.is_within_root(filepath)
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

    if not _is_within_root_or_raise(session, filepath):
        logger.warning(_OUT_OF_ROOT_HINT, filepath)
        return []

    source_lines = split_lines_like_ast(source)

    redundant: list[RedundantConversion] = []
    with session.analysis_transaction():
        try:
            baseline = _open_or_raise(session, filepath, source)
            if not baseline:
                probe = _open_or_raise(session, filepath, source + _DIAGNOSTICS_PROBE)
                baseline = _open_or_raise(session, filepath, source)
                if not probe:
                    logger.warning(_EXCLUDED_FROM_TY_HINT, filepath)
                    return []

            candidates_with_hovers: list[tuple[Candidate, str]] = []
            for candidate in candidates:
                line_text = source_lines[candidate.line - 1]
                arg_end_char = byte_col_to_char_col(line_text, candidate.arg_end_col)
                hover_char = len(line_text[: arg_end_char - 1].encode("utf-16-le")) // 2
                hover_text = session.hover(filepath, candidate.line - 1, hover_char)
                if not hover_passes_gate(hover_text, level, candidate.constructor):
                    continue
                assert hover_text is not None

                if candidate.constructor in MUTABLE_CONSTRUCTORS and candidate.mutated_after_copy:
                    continue

                if candidate.wrapped_in_len and not is_exact_match(hover_text, candidate.constructor):
                    continue

                if candidate.in_identity_comparison and (
                    candidate.constructor in MUTABLE_CONSTRUCTORS
                    or not is_exact_match(hover_text, candidate.constructor)
                ):
                    continue

                if candidate.in_comparison_operand and not (
                    is_exact_match(hover_text, candidate.constructor)
                    or (
                        is_comparison_safe_hover(hover_text, candidate.constructor) and not candidate.in_membership_test
                    )
                    or (candidate.purepath_ambiguous and is_purepath_hover(hover_text))
                ):
                    continue

                if candidate.used_in_string_interpolation and not is_exact_match(hover_text, candidate.constructor):
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
