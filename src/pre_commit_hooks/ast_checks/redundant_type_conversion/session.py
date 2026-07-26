"""Drives one long-lived `ty server` LSP session for TRI006 — see
docs/audits/type-checker-selection-for-redundant-type-conversion.md for why
`ty`, driven over LSP, was chosen, and ADR-0035 for the self-test/session
design this module implements.
"""

from __future__ import annotations

import atexit
import logging
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pre_commit_hooks._lsp import LSPClient, LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError

if TYPE_CHECKING:
    from .analysis import RedundancySession

logger = logging.getLogger("ast_checks")

_TY_COMMAND = ("ty", "server")

_INSTALL_HINT = (
    "redundant-type-conversion (TRI006) requires Astral's `ty` type checker on PATH, but it could not be "
    "started. Install it with `uv tool install ty`, `uvx --from ty ty --version` once to warm the uvx cache "
    "and then ensure `ty` resolves on PATH, or add `ty` as a dev dependency of your own project. "
    "See https://github.com/astral-sh/ty."
)

_SELF_TEST_FAILED_HINT = (
    "redundant-type-conversion (TRI006) found `ty` on PATH, but it failed this check's own compatibility "
    "self-test: a known redundant/necessary type-conversion pair didn't produce the diagnostics this check "
    "expects. `ty` is pre-1.0 and its own diagnostics may change between versions — try a different "
    "installed `ty` version. See docs/rules/redundant-type-conversion.md."
)

_REDUNDANT_CONTROL_BEFORE = """\
from collections.abc import Iterable


def takes_iterable(names: Iterable[str]) -> int:
    return sum(1 for _ in names)


def caller(names: list[str]) -> None:
    takes_iterable(list(names))
"""

_REDUNDANT_CONTROL_AFTER = """\
from collections.abc import Iterable


def takes_iterable(names: Iterable[str]) -> int:
    return sum(1 for _ in names)


def caller(names: list[str]) -> None:
    takes_iterable(names)
"""

_NECESSARY_CONTROL_BEFORE = """\
from collections.abc import Iterator


def takes_list(items: list[int]) -> int:
    return len(items)


def caller(it: Iterator[int]) -> None:
    takes_list(list(it))
"""

_NECESSARY_CONTROL_AFTER = """\
from collections.abc import Iterator


def takes_list(items: list[int]) -> int:
    return len(items)


def caller(it: Iterator[int]) -> None:
    takes_list(it)
"""


def _diagnostic_key(diagnostic: dict[str, Any]) -> tuple[Any, ...]:
    """Identity for one `ty` diagnostic, used to diff a "before" and
    "after" diagnostic set (see `TySession.open_or_update`'s own callers)
    against each other -- specifically, to answer "did removing this one
    candidate's conversion introduce any diagnostic that wasn't already
    there".

    Deliberately excludes:
    - `data` (`ty` attaches a `redundant-cast`-style quick-fix edit map to
      some diagnostics, keyed by absolute file URI, which is irrelevant to
      *identifying* a diagnostic).
    - Character-column positions. The synthetic rewrite only ever removes
      text from one line (see `analysis._build_modified_text`), which
      shifts the column position of every *other*, unrelated diagnostic
      later on that same line -- confirmed empirically: an untouched
      `invalid-argument-type` diagnostic about a call's second argument
      keeps its exact `code`/`message` but its `range` shifts left by
      however many characters the removed conversion's own syntax spanned.
      Including columns in this key would make that unrelated, unchanged
      diagnostic look "new" after the rewrite purely because of the
      shift, wrongly blocking a genuinely redundant conversion from being
      reported. Line numbers are kept (a same-line-only rewrite never
      shifts *those*), which is precise enough in practice: two distinct
      diagnostics sharing both an identical `code` and an identical
      `message` on the same line are not a realistic collision, since
      `ty`'s own diagnostic messages routinely embed the specific
      types/names involved.
    """
    rng = diagnostic.get("range") or {}
    start = rng.get("start") or {}
    end = rng.get("end") or {}
    return (
        diagnostic.get("code"),
        diagnostic.get("message"),
        start.get("line"),
        end.get("line"),
    )


class TySession:
    """One `ty server` child process, driven over LSP, kept open for as
    long as this object lives. Not responsible for deciding *when* a
    conversion is redundant — that's `analysis.decide_candidates()`'s job;
    this class only exposes the LSP primitives it needs (open/update a
    document's content and get back its current diagnostics, plus hover).
    """

    __slots__ = ("_client", "_open_versions")

    def __init__(self, *, root: Path) -> None:
        self._client = _spawn(root)
        self._open_versions: dict[str, int] = {}

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[Any, ...]]:
        """Opens `filepath` in this session (if not already open) or
        replaces its entire in-memory content (if it is), then returns its
        current diagnostics. Never touches `filepath` on disk — `content`
        is only ever held in the `ty server` process's own in-memory
        buffer for this document, exactly like an editor's unsaved buffer.
        """
        uri = filepath.resolve().as_uri()
        if uri in self._open_versions:
            self._open_versions[uri] += 1
            self._client.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": self._open_versions[uri]},
                    "contentChanges": [{"text": content}],
                },
            )
        else:
            self._open_versions[uri] = 1
            self._client.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": content}},
            )
        return self._pull_diagnostics(uri)

    def _pull_diagnostics(self, uri: str) -> frozenset[tuple[Any, ...]]:
        response = self._client.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}, timeout=20.0)
        items = (response or {}).get("items", [])
        return frozenset(_diagnostic_key(item) for item in items)

    def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None:
        """The statically-inferred type at `filepath`'s (0-indexed line,
        UTF-16 code-unit column) as plain text, or `None` if hovering
        fails or resolves to nothing -- a soft failure by design (see
        `confidence.hover_passes_gate`, which treats `None` the same as an
        unhelpful result), since a hover is only ever this check's own
        cheap pre-filter, never its final redundancy decision.
        """
        uri = filepath.resolve().as_uri()
        try:
            response = self._client.request(
                "textDocument/hover",
                {"textDocument": {"uri": uri}, "position": {"line": line0, "character": char_utf16}},
                timeout=10.0,
            )
        except LSPError:
            logger.debug("TRI006 hover failed for %s", filepath, exc_info=True)
            return None
        if not response:
            return None
        contents = response.get("contents")
        if isinstance(contents, dict):
            value = contents.get("value")
            return value if isinstance(value, str) else None
        return None

    def close_file(self, filepath: Path) -> None:
        """Discards `filepath`'s in-memory document from this session,
        bounding this session's own memory across a whole-repo run — a
        long-lived session, by design (see this module's own docstring),
        must not just accumulate every file it's ever looked at.
        """
        uri = filepath.resolve().as_uri()
        if uri in self._open_versions:
            self._client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            del self._open_versions[uri]

    def close(self) -> None:
        self._client.close()


def _spawn(root: Path) -> LSPClient:
    try:
        client = LSPClient(_TY_COMMAND, cwd=root)
    except OSError as error:
        # Not just FileNotFoundError: `ty` resolving on PATH but failing to
        # actually launch (no execute permission, a corrupt/wrong-format
        # binary, ...) raises a different OSError subclass -- all of these
        # mean the same thing to this check ("ty could not be started"),
        # so all of them get the same install-hint CheckUnavailableError
        # rather than only FileNotFoundError being caught here and the
        # rest surfacing as an ordinary per-file check() crash instead.
        raise CheckUnavailableError(_INSTALL_HINT) from error

    try:
        client.request(
            "initialize",
            {"processId": None, "rootUri": root.resolve().as_uri(), "capabilities": {}},
            timeout=20.0,
        )
        client.notify("initialized", {})
    except LSPError as error:
        client.close()
        raise CheckUnavailableError(_INSTALL_HINT) from error
    return client


def _run_self_test(session: RedundancySession, root: Path) -> None:
    """One redundant-case and one necessary-case positive/negative control
    (the exact fixtures issue #108 specifies), run against `session` --
    the caller (`get_session()`) is responsible for making that a
    throwaway session scoped to its own scratch directory, isolated from
    whatever real project this check is actually about to analyze, so a
    real project's own configuration/imports can't influence the
    self-test's own verdict. Takes `session` as a `RedundancySession`
    (structural, not the concrete `TySession`) so a test can substitute a
    fake one to exercise this function's own pass/fail logic without a
    real `ty` process.

    Raises:
        CheckUnavailableError: if either control doesn't produce the
            expected before/after diagnostics, or `session` itself raises
            `LSPError` (e.g. `ty` crashed partway through).
    """
    try:
        redundant_path = root / "redundant_control.py"
        redundant_path.write_text(_REDUNDANT_CONTROL_BEFORE, encoding="utf-8")
        redundant_before = session.open_or_update(redundant_path, _REDUNDANT_CONTROL_BEFORE)
        redundant_after = session.open_or_update(redundant_path, _REDUNDANT_CONTROL_AFTER)
        # Diffed the same way decide_candidates() itself diffs a real
        # candidate's before/after diagnostics, rather than requiring the
        # raw sets be literally empty -- robust to an unrelated diagnostic
        # (e.g. a future `ty` version adding an advisory lint neither
        # fixture is designed to avoid) appearing identically in both
        # snapshots, which would otherwise fail this self-test for a
        # reason that has nothing to do with what it's actually verifying.
        if redundant_after - redundant_before:
            raise CheckUnavailableError(_SELF_TEST_FAILED_HINT)

        necessary_path = root / "necessary_control.py"
        necessary_path.write_text(_NECESSARY_CONTROL_BEFORE, encoding="utf-8")
        necessary_before = session.open_or_update(necessary_path, _NECESSARY_CONTROL_BEFORE)
        necessary_after = session.open_or_update(necessary_path, _NECESSARY_CONTROL_AFTER)
        if not (necessary_after - necessary_before):
            raise CheckUnavailableError(_SELF_TEST_FAILED_HINT)
    except LSPError as error:
        raise CheckUnavailableError(_SELF_TEST_FAILED_HINT) from error


_session: TySession | None = None
_session_lock = threading.Lock()


def get_session() -> TySession:  # pytriage: ignore=TRI004 -- lazy singleton false positive, see issue #110
    """The single `TySession` every `RedundantTypeConversionCheck` instance
    shares within one process — created lazily, on the first file this
    check actually examines (never at `__init__`/CLI-startup time, so a
    run with nothing for this check to look at never pays `ty`'s startup
    cost or risks failing its self-test for no reason). Runs the
    behavioral self-test exactly once per process, before the real session
    (scoped to the current working directory — pre-commit/prek always
    invoke a hook from the repository root) is ever handed out.
    """
    global _session  # noqa: PLW0603 -- the documented, deliberate one-session-per-process singleton this whole module exists for
    with _session_lock:
        if _session is None:
            with tempfile.TemporaryDirectory(prefix="ruff-extra-rules-tri006-selftest-") as scratch_dir:
                scratch_root = Path(scratch_dir)
                self_test_session = TySession(root=scratch_root)
                try:
                    _run_self_test(self_test_session, scratch_root)
                finally:
                    self_test_session.close()
            _session = TySession(root=Path.cwd())
            atexit.register(_session.close)
        return _session
