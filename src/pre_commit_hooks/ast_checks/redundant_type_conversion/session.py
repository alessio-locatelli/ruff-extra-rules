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
    # Excludes character columns: the synthetic rewrite shifts every other
    # diagnostic's own column on the same line, which would make an
    # unrelated, unchanged diagnostic look "new" after the rewrite --
    # confirmed empirically against real ty. Line numbers are kept (a
    # same-line rewrite never shifts those).
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
    """One `ty server` child process over LSP. Exposes only the primitives `analysis.decide_candidates()` needs."""

    __slots__ = ("_client", "_open_versions")

    def __init__(self, *, root: Path) -> None:
        self._client = _spawn(root)
        self._open_versions: dict[str, int] = {}

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[Any, ...]]:
        """Opens or updates `filepath`'s in-memory-only content (never touches disk) and returns its diagnostics."""
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
        """The statically-inferred type at (0-indexed line, UTF-16 column), or `None` on failure by design."""
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
        """Discards `filepath`'s in-memory document, bounding this long-lived session's own memory use."""
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
        # Not just FileNotFoundError: ty resolving on PATH but failing to
        # launch (no execute permission, a corrupt binary, ...) raises a
        # different OSError subclass, but means the same thing here.
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
    """Positive/negative control pair against `session` -- see ADR-0035's "Failure handling".

    `session` is typed as the structural `RedundancySession`, not the
    concrete `TySession`, so a test can substitute a fake one.
    """
    try:
        redundant_path = root / "redundant_control.py"
        redundant_path.write_text(_REDUNDANT_CONTROL_BEFORE, encoding="utf-8")
        redundant_before = session.open_or_update(redundant_path, _REDUNDANT_CONTROL_BEFORE)
        redundant_after = session.open_or_update(redundant_path, _REDUNDANT_CONTROL_AFTER)
        # Diffed, not required to be literally empty -- see _diagnostic_key.
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
    """Process-wide `TySession` singleton, created lazily. See ADR-0035's "Invocation"/"Failure handling"."""
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
