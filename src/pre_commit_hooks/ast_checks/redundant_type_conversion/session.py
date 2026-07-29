"""Drives one long-lived `ty server` LSP session for TR6 — see
docs/audits/type-checker-selection-for-redundant-type-conversion.md for why
`ty`, driven over LSP, was chosen, ADR-0035 for the self-test/session
design this module implements, and ADR-0041 for its `keep_open` persistent
mode.
"""

from __future__ import annotations

import atexit
import logging
import re
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pre_commit_hooks._lsp import LSPClient, LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

    from .analysis import RedundancySession

logger = logging.getLogger("ast_checks")


class PersistentSession(Protocol):
    """The full surface `get_session()`'s result exposes to this check's own top-level `check()` -- a
    superset of `RedundancySession` (which `decide_candidates()` alone needs), covering the cross-file
    primitives (ADR-0041) `decide_candidates()` never calls directly. `TySession` and
    `daemon.RemoteTySession` both satisfy this structurally.
    """

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[Any, ...]]: ...

    def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None: ...

    def finalize(self, filepath: Path, source: str) -> None: ...

    def notify_changed_on_disk(self, filepath: Path, source: str) -> None: ...

    def drain_cross_file_candidates(self, already_processed: list[Path]) -> list[Path]: ...

    def close(self) -> None: ...


_TY_COMMAND = ("ty", "server")

# See ADR-0035's "Detection method" for why this separator is stripped.
_HOVER_DOC_SEPARATOR = re.compile(r"\n-{3,}\n")

# Kept in lockstep with pyproject.toml's `dependency-groups.dev` `ty>=X.Y.Z` pin by
# test_min_ty_version_matches_pyproject_pin() -- see docs/adr/0039-tri006-unavailable-message-scope-and-wording.md.
_MIN_TY_VERSION = "0.0.64"

_INSTALL_HINT = (
    "redundant-type-conversion (TR6) requires Astral's `ty` type checker on PATH. Install it with "
    "`uv tool install ty`, or add `ty` as a dev dependency of your own project and commit with that virtual "
    "environment active, or opt out with `--ignore=redundant-type-conversion`. "
    "See https://github.com/astral-sh/ty."
)

_SELF_TEST_FAILED_HINT = (
    "redundant-type-conversion (TR6) found `ty` on PATH, but it failed this check's own compatibility "
    "self-test: a known redundant/necessary type-conversion pair didn't produce the diagnostics this check "
    f"expects. `ty` is pre-1.0 and its diagnostics can change between versions in either direction. This "
    f"release of ruff-extra-rules was validated against ty>={_MIN_TY_VERSION} -- if your `ty` predates that, "
    "upgrade it; if it's newer than that, pinning to an older `ty` (or waiting for a ruff-extra-rules update) "
    "may help instead -- or opt out with `--ignore=redundant-type-conversion`. "
    "See docs/rules/redundant-type-conversion.md."
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
    """One `ty server` child process over LSP. Exposes only the primitives `analysis.decide_candidates()` needs.

    `keep_open=True` (see ADR-0041) is this session's persistent-daemon
    mode: `finalize()` re-syncs a file to its real content and leaves it
    tracked instead of discarding it, and `drain_cross_file_candidates()`
    reports other, already-tracked files that received an unsolicited
    diagnostics push as a result -- `ty`'s own confirmed cross-file
    dependency awareness. The default (`False`) is today's original,
    per-invocation behavior: every file is discarded once this check is
    done with it.
    """

    __slots__ = ("_client", "_dirty_uris", "_dirty_uris_lock", "_keep_open", "_open_versions", "_root")

    def __init__(self, *, root: Path, keep_open: bool = False) -> None:
        self._root = root.resolve()
        self._keep_open = keep_open
        self._dirty_uris: set[str] = set()
        self._dirty_uris_lock = threading.Lock()
        self._client = _spawn(root, on_notification=self._on_notification if keep_open else None)
        self._open_versions: dict[str, int] = {}

    def _is_within_root(self, filepath: Path) -> bool:
        return filepath.resolve().is_relative_to(self._root)

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        """Captures an unsolicited `publishDiagnostics` push -- see `drain_cross_file_candidates()`.

        Only registered with `_client` when `keep_open` is true: a
        per-invocation session never keeps a file open long enough for a
        later push about it to mean anything. Runs on `LSPClient`'s own
        background reader thread, never the caller's -- `_dirty_uris_lock`
        keeps this add() from interleaving with `drain_cross_file_candidates()`'s
        own read-then-clear on the caller's thread.
        """
        if method != "textDocument/publishDiagnostics":
            return
        uri = params.get("uri")
        if isinstance(uri, str):
            with self._dirty_uris_lock:
                self._dirty_uris.add(uri)

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
            logger.debug("TR6 hover failed for %s", filepath, exc_info=True)
            return None
        if not response:
            return None
        contents = response.get("contents")
        if not isinstance(contents, dict):
            return None
        value = contents.get("value")
        if not isinstance(value, str):
            return None
        match = _HOVER_DOC_SEPARATOR.search(value)
        return value[: match.start()] if match else value

    def close_file(self, filepath: Path) -> None:
        """Discards `filepath`'s in-memory document; never raises since it runs from a `finally` block."""
        uri = filepath.resolve().as_uri()
        if uri in self._open_versions:
            try:
                self._client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            except LSPError:
                logger.debug("TR6 close_file failed for %s", filepath, exc_info=True)
            del self._open_versions[uri]

    def notify_changed_on_disk(self, filepath: Path, source: str) -> None:
        """Tells `ty` `filepath` changed, so it can proactively push updated diagnostics for whichever
        other, already-tracked file depends on it -- #123's own spike confirmed `workspace/
        didChangeWatchedFiles` specifically (not an ordinary `didChange`) is what triggers this, even
        for a file that's already open, confirmed empirically in both shapes. Never raises: called from
        `finalize()`, which must not fail this check's own analysis over a cross-file side effect.

        Two things this does beyond a bare notification, both required for the cross-file push to be
        both correct and observable by the time this returns:

        - If `filepath` is already open (e.g. from an earlier commit's own candidate analysis), it's
          first re-synced to `source` -- its real, current content -- via an ordinary `didChange`: `ty`
          treats an explicitly opened document's own buffer as authoritative over a mere watched-file
          event, so skipping this would leave it looking at stale, pre-edit content.
        - `didChangeWatchedFiles` is a notification, not a request: nothing here waits for `ty` to
          actually finish recomputing and pushing dependents' diagnostics before this returns. A
          synchronous round trip against any already-tracked file forces that -- confirmed empirically
          that `ty` answers requests in the order it receives them, so a response to a request sent
          right after this notification means `ty` has already finished acting on it.

        A no-op for a file outside `root`: this daemon's own cross-file awareness is scoped to the
        repository it was spawned for, and telling `ty` about an unrelated file elsewhere on disk would
        only be work spent on a file this session was never meant to track in the first place (see
        `finalize()`'s own root check, which is what keeps such a file from ever reaching here already-open).
        """
        if not self._is_within_root(filepath):
            return
        uri = filepath.resolve().as_uri()
        if uri in self._open_versions:
            try:
                self.open_or_update(filepath, source)
            except LSPError:
                logger.debug("TR6 notify_changed_on_disk (re-sync) failed for %s", filepath, exc_info=True)
                return
        try:
            self._client.notify("workspace/didChangeWatchedFiles", {"changes": [{"uri": uri, "type": 2}]})
        except LSPError:
            logger.debug("TR6 notify_changed_on_disk failed for %s", filepath, exc_info=True)
            return
        self._await_ty_catching_up()

    def _await_ty_catching_up(self) -> None:
        """A synchronous round trip against any already-tracked file, forcing `ty` to have finished
        processing whatever was just sent to it -- see `notify_changed_on_disk`'s own docstring. Which
        tracked file this asks about doesn't matter, only that `ty` must answer before returning. A
        no-op when nothing is tracked yet, since nothing could be dirty either in that case.
        """
        barrier_uri = next(iter(self._open_versions), None)
        if barrier_uri is None:
            return
        try:
            self._pull_diagnostics(barrier_uri)
        except LSPError:
            logger.debug("TR6 barrier pull failed for %s", barrier_uri, exc_info=True)

    def finalize(self, filepath: Path, source: str) -> None:
        """Ends this candidate-analysis pass over `filepath` -- see `RedundancySession.finalize`'s own docstring
        for the two modes this picks between. Never raises, matching `close_file()`'s own contract: this runs
        from a `finally` block, so a lost connection here must be swallowed (already reported, since the
        preceding candidate loop's own calls hit the same dead session first) rather than replacing whatever
        this `finally` is unwinding from.

        In `keep_open` mode, `notify_changed_on_disk()` alone both re-syncs `filepath` back to its real,
        pristine content (undoing whatever synthetic rewrite the candidate loop above last left it as) and
        tells `ty` it changed -- `filepath` is already tracked by this point (the candidate loop that
        called this from its own `finally` never runs with zero candidates), so no separate resync call
        is needed here first.

        A file outside `root` is always closed here instead, even in `keep_open` mode: this session's own
        daemon (ADR-0041) persists every file it keeps open indefinitely, across separate, later commits --
        a file that happens to live outside the repository this daemon was spawned for (e.g. a stray CLI
        invocation naming a path elsewhere on disk) must not be adopted into that same persistent state,
        or an unrelated future run against this daemon's own repository would end up reporting on it too.
        """
        if self._keep_open and self._is_within_root(filepath):
            self.notify_changed_on_disk(filepath, source)
        else:
            self.close_file(filepath)

    def drain_cross_file_candidates(self, already_processed: list[Path]) -> list[Path]:
        """Previously-tracked files (still open in this session) that received an unsolicited diagnostics push
        since the last call, excluding `already_processed` -- the files a run already examined directly, which
        don't need reporting as an extra, side-effect file. Always empty when this session wasn't constructed
        with `keep_open=True`, since nothing ever registers `_on_notification` to populate `_dirty_uris`
        otherwise. See `ASTCheck.drain_cross_file_candidates` (ADR-0041) for this method's own caller.
        """
        exclude_uris = {path.resolve().as_uri() for path in already_processed}
        with self._dirty_uris_lock:
            dirty_uris = self._dirty_uris - exclude_uris
            self._dirty_uris.clear()
        return [Path.from_uri(uri) for uri in dirty_uris if uri in self._open_versions]

    def close(self) -> None:
        self._client.close()


def _spawn(root: Path, *, on_notification: Callable[[str, dict[str, Any]], None] | None = None) -> LSPClient:
    try:
        client = LSPClient(_TY_COMMAND, cwd=root, on_notification=on_notification)
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


_session: PersistentSession | None = None
_session_lock = threading.Lock()
_daemon_probe_failed = False


def get_session() -> PersistentSession:
    """Process-wide session singleton, created lazily on the first file with a real, non-suppressed
    candidate -- see ADR-0035's "Invocation"/"Failure handling" for why, and ADR-0041 for the
    persistent-daemon-first, local-fallback choice this now makes.
    """
    global _session  # noqa: PLW0603 -- the documented, deliberate one-session-per-process singleton this whole module exists for
    with _session_lock:
        if _session is None:
            _session = _acquire_session()
            atexit.register(_session.close)
        return _session


def _acquire_session() -> PersistentSession:
    """Prefers a persistent daemon (spawning one if none is reachable yet); falls back to today's original,
    private per-invocation session for an operational reason (no socket support, spawn timeout, ...) -- `ty`
    itself being missing or broken is a different, non-fallback-worthy failure (`CheckUnavailableError`),
    since a local session would fail identically.
    """
    # Function-local: daemon.py imports this module at its own top level, so a module-level import here
    # would be circular.
    from . import daemon  # noqa: PLC0415

    try:
        return daemon.connect(Path.cwd())
    except OSError:
        logger.debug("Could not use a persistent ty daemon; falling back to a private session", exc_info=True)
        return _local_session()


def _local_session() -> TySession:
    with tempfile.TemporaryDirectory(prefix="ruff-extra-rules-tri006-selftest-") as scratch_dir:
        scratch_root = Path(scratch_dir)
        self_test_session = TySession(root=scratch_root)
        try:
            _run_self_test(self_test_session, scratch_root)
        finally:
            self_test_session.close()
    return TySession(root=Path.cwd())


def peek_session() -> PersistentSession | None:
    """The process-wide session if one has already been created this run, without ever creating one.

    Used by `RedundantTypeConversionCheck.drain_cross_file_candidates()`, which must not spawn a session (or
    a daemon) of its own just to ask one that a run with no real candidate anywhere never needed in the
    first place.
    """
    return _session


def notify_disk_change_if_session_active(filepath: Path, source: str) -> None:
    """Cheaply tells an already-connected session that `filepath` changed on disk, without ever spawning
    anything new.

    A file with no syntactic candidates of its own -- the common case for a pure signature change, #109's
    own headline scenario -- never reaches `decide_candidates()`'s own notification (see
    `RedundantTypeConversionCheck.check()`'s early returns), so this is the only place that gap gets closed
    for such a file. Deliberately does not spawn a fresh daemon on its own: a repository that has never
    triggered a real candidate (and so never paid `ty`'s own startup cost) shouldn't start paying it just
    because some unrelated, candidate-less file happened to change.

    A failed probe (no daemon reachable, or one confirmed alive but too busy to free up in time) is
    memoized for the rest of this run, the same way a successful one already is via `_session` itself:
    without this, every later candidate-less file in the same run would repeat the same probe -- including
    its own busy-daemon wait (ADR-0041) -- even though the first attempt already answered the only question
    this run needs answered once.
    """
    global _session, _daemon_probe_failed  # noqa: PLW0603
    with _session_lock:
        if _session is not None:
            _session.notify_changed_on_disk(filepath, source)
            return
        if _daemon_probe_failed:
            return

        from . import daemon  # noqa: PLC0415 -- see _acquire_session()'s own comment on why this must be local

        probed = daemon.try_connect_existing(Path.cwd())
        if probed is not None:
            _session = probed
            atexit.register(_session.close)
            _session.notify_changed_on_disk(filepath, source)
        else:
            _daemon_probe_failed = True
