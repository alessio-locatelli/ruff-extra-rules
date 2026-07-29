from __future__ import annotations

import re
import threading
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import pre_commit_hooks.ast_checks.redundant_type_conversion.daemon as daemon_module
import pre_commit_hooks.ast_checks.redundant_type_conversion.session as session_module
from pre_commit_hooks._lsp import LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError
from pre_commit_hooks.ast_checks.redundant_type_conversion.session import (
    TySession,
    _diagnostic_key,
    _run_self_test,
    _spawn,
    get_session,
    notify_disk_change_if_session_active,
    peek_session,
)

from ._helpers import FakeSession

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolated_session_singleton() -> Iterator[None]:
    """`get_session()`'s module-global singleton must never leak state
    between tests: a test exercising a *failure* path here (mocking `ty`
    away entirely) must not leave a broken or fake session behind for a
    later, unrelated test -- including the real-`ty` integration suite,
    which relies on getting a genuine session from a clean process state.
    """
    original = session_module._session
    session_module._session = None
    yield
    session_module._session = original


def test_diagnostic_key_extracts_stable_identity_fields() -> None:
    diagnostic = {
        "code": "invalid-argument-type",
        "message": "boom",
        "range": {"start": {"line": 3, "character": 4}, "end": {"line": 3, "character": 10}},
        "data": {"edits": {"file:///whatever": []}},
    }
    assert _diagnostic_key(diagnostic) == ("invalid-argument-type", "boom", 3, 3)


def test_diagnostic_key_tolerates_a_missing_range() -> None:
    assert _diagnostic_key({"code": "x", "message": "y"}) == ("x", "y", None, None)


def test_diagnostic_key_ignores_a_character_column_shift() -> None:
    # A same-line synthetic rewrite shifts every *other*
    # diagnostic's own column positions on that line without changing the
    # diagnostic itself -- confirmed empirically against real ty (an
    # untouched invalid-argument-type diagnostic's range shifted left by
    # exactly the removed conversion's own character count). Two
    # otherwise-identical diagnostics differing only in column must
    # compare equal, or the shift alone would make an unrelated diagnostic
    # look "new" and wrongly block flagging a genuinely redundant
    # conversion on the same line.
    before = {
        "code": "invalid-argument-type",
        "message": "boom",
        "range": {"start": {"line": 5, "character": 22}, "end": {"line": 5, "character": 34}},
    }
    after = {
        "code": "invalid-argument-type",
        "message": "boom",
        "range": {"start": {"line": 5, "character": 17}, "end": {"line": 5, "character": 29}},
    }
    assert _diagnostic_key(before) == _diagnostic_key(after)


class _ScriptedSelfTestSession(FakeSession):
    """A `FakeSession` pre-loaded with the exact before/after diagnostics
    `_run_self_test` will ask for, keyed by control fixture content --
    lets a test drive `_run_self_test`'s own pass/fail branches without a
    real `ty` process.
    """

    __slots__ = ("_raises",)

    def __init__(
        self, *, redundant_after_is_new: bool = False, necessary_after_is_new: bool = True, raises: bool = False
    ) -> None:
        redundant_after_diagnostics = (
            frozenset({("x", "unexpected", 0, 0, 0, 1)}) if redundant_after_is_new else frozenset()
        )
        necessary_after_diagnostics = (
            frozenset({("invalid-argument-type", "boom", 5, 4, 5, 6)}) if necessary_after_is_new else frozenset()
        )
        super().__init__(
            diagnostics_by_content={
                session_module._REDUNDANT_CONTROL_BEFORE: frozenset(),
                session_module._REDUNDANT_CONTROL_AFTER: redundant_after_diagnostics,
                session_module._NECESSARY_CONTROL_BEFORE: frozenset(),
                session_module._NECESSARY_CONTROL_AFTER: necessary_after_diagnostics,
            },
            hover_by_position={},
        )
        self._raises = raises

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[object, ...]]:
        if self._raises:
            raise LSPError("simulated ty crash")
        return super().open_or_update(filepath, content)


def test_run_self_test_passes_with_correctly_behaving_diagnostics(tmp_path: Path) -> None:
    session = _ScriptedSelfTestSession()
    _run_self_test(session, tmp_path)  # must not raise


@pytest.mark.parametrize(
    "scripted_kwargs",
    [
        {"redundant_after_is_new": True},
        {"necessary_after_is_new": False},
        {"raises": True},
    ],
    ids=["redundant-control-gets-a-new-diagnostic", "necessary-control-gets-no-new-diagnostic", "session-raises"],
)
def test_run_self_test_raises_when_a_control_misbehaves(tmp_path: Path, scripted_kwargs: dict[str, Any]) -> None:
    session = _ScriptedSelfTestSession(**scripted_kwargs)
    with pytest.raises(CheckUnavailableError, match="self-test"):
        _run_self_test(session, tmp_path)


def test_spawn_raises_check_unavailable_error_when_ty_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module, "_TY_COMMAND", ("definitely-not-a-real-executable-xyz",))
    with pytest.raises(CheckUnavailableError, match="requires Astral's `ty`"):
        _spawn(tmp_path)


def test_spawn_raises_check_unavailable_error_when_ty_exists_but_is_not_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _spawn must catch more than just FileNotFoundError -- `ty` resolving
    # on PATH but failing to actually launch (missing execute permission
    # here; a corrupt/wrong-format binary raises the same way) must raise
    # this check's own actionable CheckUnavailableError, not an uncaught
    # PermissionError.
    not_executable = tmp_path / "not-really-ty"
    not_executable.write_text("#!/bin/sh\necho fake ty\n")
    not_executable.chmod(0o644)
    monkeypatch.setattr(session_module, "_TY_COMMAND", (str(not_executable),))  # pytriage: TR6
    with pytest.raises(CheckUnavailableError, match="requires Astral's `ty`"):
        _spawn(tmp_path)


def test_spawn_raises_check_unavailable_error_when_initialize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BrokenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            msg = "simulated initialize failure"
            raise LSPError(msg)

        def close(self) -> None:
            return

    monkeypatch.setattr(session_module, "LSPClient", _BrokenClient)
    with pytest.raises(CheckUnavailableError, match="requires Astral's `ty`"):
        _spawn(tmp_path)


def test_get_session_returns_the_same_instance_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel_calls: list[int] = []

    class _FakeTySession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            sentinel_calls.append(1)

        def close(self) -> None:
            return

    def _raise_os_error(_root: Path) -> None:
        msg = "simulated: no daemon reachable"
        raise OSError(msg)

    monkeypatch.setattr(daemon_module, "connect", _raise_os_error)
    monkeypatch.setattr(session_module, "_run_self_test", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(session_module, "TySession", _FakeTySession)

    assert get_session() is get_session()
    # Exactly two TySession constructions total, both from the *first*
    # get_session() call: one throwaway scratch session for the self-test,
    # one long-lived real session handed back to callers. The second
    # get_session() call must construct neither -- that's the whole point
    # of the singleton.
    assert sentinel_calls == [1, 1]


class _FakeNotifiableSession:
    __slots__ = ("notified",)

    def __init__(self) -> None:
        self.notified: list[tuple[Path, str]] = []

    def notify_changed_on_disk(self, filepath: Path, source: str) -> None:
        self.notified.append((filepath, source))

    def close(self) -> None:
        return


def test_peek_session_returns_none_when_no_session_exists() -> None:
    assert peek_session() is None


def test_peek_session_returns_the_active_session() -> None:
    session_module._session = _FakeNotifiableSession()  # type: ignore[assignment]
    assert peek_session() is session_module._session


def test_notify_disk_change_if_session_active_uses_the_existing_session(tmp_path: Path) -> None:
    fake = _FakeNotifiableSession()
    session_module._session = fake  # type: ignore[assignment]
    filepath = tmp_path / "callee.py"

    notify_disk_change_if_session_active(filepath, "source\n")

    assert fake.notified == [(filepath, "source\n")]


def test_notify_disk_change_if_session_active_promotes_a_probed_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeNotifiableSession()
    monkeypatch.setattr(daemon_module, "try_connect_existing", lambda _root: fake)
    filepath = tmp_path / "callee.py"

    notify_disk_change_if_session_active(filepath, "source\n")

    # fake having recorded the notification is itself proof it became the active session --
    # comparing identity directly against a `_FakeNotifiableSession` (not a full `PersistentSession`)
    # is a mypy non-overlapping-identity error, not just redundant.
    assert fake.notified == [(filepath, "source\n")]
    fake.close()  # atexit.register(_session.close) only stores the reference; this proves it's callable


def test_notify_disk_change_if_session_active_is_a_no_op_when_no_daemon_is_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "try_connect_existing", lambda _root: None)

    notify_disk_change_if_session_active(tmp_path / "callee.py", "source\n")  # must not raise

    assert peek_session() is None


class _StubLSPClient:
    """A minimal stand-in for `LSPClient`'s own request/notify interface --
    lets `TySession.hover`/`close_file`'s own logic be unit-tested directly
    without a real `ty` process, mirroring `FakeSession`'s role one layer
    down (that fakes a whole `TySession`; this fakes the LSP client a real
    `TySession` drives).
    """

    __slots__ = ("hover_raises", "hover_result", "notify_calls", "notify_raises")

    def __init__(self, *, hover_result: object = None, hover_raises: bool = False, notify_raises: bool = False) -> None:
        self.hover_result = hover_result
        self.hover_raises = hover_raises
        self.notify_raises = notify_raises
        self.notify_calls: list[tuple[str, dict[str, object]]] = []

    def request(self, _method: str, _params: dict[str, object], *, timeout: float = 10.0) -> object:  # noqa: ARG002
        if self.hover_raises:
            msg = "simulated hover failure"
            raise LSPError(msg)
        return self.hover_result

    def notify(self, method: str, params: dict[str, object]) -> None:
        if self.notify_raises:
            msg = "simulated notify failure"
            raise LSPError(msg)
        self.notify_calls.append((method, params))


def _session_with_stub_client(
    client: _StubLSPClient, *, keep_open: bool = False, root: Path | None = None
) -> TySession:
    session = TySession.__new__(TySession)
    session._root = (root or Path.cwd()).resolve()
    session._client = client  # type: ignore[assignment]
    session._open_versions = {}
    session._dirty_uris = set()
    session._dirty_uris_lock = threading.Lock()
    session._keep_open = keep_open
    return session


@pytest.mark.parametrize(
    ("hover_result", "hover_raises", "expected"),
    [
        (None, True, None),
        (None, False, None),
        ({}, False, None),
        ({"contents": "not-a-dict"}, False, None),
        ({"contents": {"kind": "plaintext", "value": 123}}, False, None),
        ({"contents": {"kind": "plaintext", "value": "list[int]"}}, False, "list[int]"),
    ],
    ids=["client-raises", "none-result", "empty-dict-result", "contents-not-a-dict", "value-not-a-string", "success"],
)
def test_hover(tmp_path: Path, hover_result: object, *, hover_raises: bool, expected: str | None) -> None:
    client = _StubLSPClient(hover_result=hover_result, hover_raises=hover_raises)
    session = _session_with_stub_client(client)

    assert session.hover(tmp_path / "f.py", 0, 0) == expected


def test_hover_strips_a_docstring_appended_after_tys_own_separator(tmp_path: Path) -> None:
    # See ADR-0035's "Detection method": `ty` appends a symbol's own
    # docstring after a fixed dashed-line separator in the same hover
    # response -- only the part before it is a type/signature.
    raw = "<class 'CallsiteParameter'>\n---------------------------------------------\nDocstring here.\n"
    client = _StubLSPClient(hover_result={"contents": {"kind": "plaintext", "value": raw}})
    session = _session_with_stub_client(client)

    assert session.hover(tmp_path / "f.py", 0, 0) == "<class 'CallsiteParameter'>"


def test_open_or_update_opens_a_new_file_and_returns_its_diagnostics(tmp_path: Path) -> None:
    diagnostic = {
        "code": "invalid-argument-type",
        "message": "boom",
        "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}},
    }
    client = _StubLSPClient(hover_result={"items": [diagnostic]})
    session = _session_with_stub_client(client)
    filepath = tmp_path / "f.py"

    diagnostics = session.open_or_update(filepath, "y = x\n")

    assert diagnostics == {_diagnostic_key(diagnostic)}
    assert client.notify_calls == [
        (
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": filepath.resolve().as_uri(),
                    "languageId": "python",
                    "version": 1,
                    "text": "y = x\n",
                }
            },
        )
    ]


def test_open_or_update_sends_a_didchange_for_an_already_open_file(tmp_path: Path) -> None:
    client = _StubLSPClient(hover_result={"items": []})
    session = _session_with_stub_client(client)
    filepath = tmp_path / "f.py"
    uri = filepath.resolve().as_uri()
    session._open_versions[uri] = 1

    diagnostics = session.open_or_update(filepath, "y = x\n")

    assert diagnostics == frozenset()
    assert client.notify_calls == [
        (
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "y = x\n"}]},
        )
    ]


@pytest.mark.parametrize(
    "stub_kwargs", [{"hover_raises": True}, {"notify_raises": True}], ids=["diagnostic-pull-fails", "notify-fails"]
)
def test_open_or_update_propagates_lsp_error_when_the_server_fails(
    tmp_path: Path, stub_kwargs: dict[str, bool]
) -> None:
    # Left uncaught: analysis.decide_candidates() converts this to CheckUnavailableError, since silently
    # treating it as "inconclusive" would let a dead ty session masquerade as a clean, empty result.
    client = _StubLSPClient(**stub_kwargs)
    session = _session_with_stub_client(client)

    with pytest.raises(LSPError):
        session.open_or_update(tmp_path / "f.py", "y = x\n")


def test_close_file_is_a_no_op_for_a_file_that_was_never_opened(tmp_path: Path) -> None:
    client = _StubLSPClient()
    session = _session_with_stub_client(client)

    session.close_file(tmp_path / "never_opened.py")

    assert client.notify_calls == []


def test_close_file_notifies_didclose_and_forgets_an_open_file(tmp_path: Path) -> None:
    client = _StubLSPClient()
    session = _session_with_stub_client(client)
    filepath = tmp_path / "opened.py"
    uri = filepath.resolve().as_uri()
    session._open_versions[uri] = 1

    session.close_file(filepath)

    assert client.notify_calls == [("textDocument/didClose", {"textDocument": {"uri": uri}})]
    assert uri not in session._open_versions


def test_close_file_forgets_the_file_even_when_the_didclose_notification_fails(tmp_path: Path) -> None:
    # close_file() runs from decide_candidates()'s own `finally` -- it must never raise.
    client = _StubLSPClient(notify_raises=True)
    session = _session_with_stub_client(client)
    filepath = tmp_path / "opened.py"
    uri = filepath.resolve().as_uri()
    session._open_versions[uri] = 1

    session.close_file(filepath)  # must not raise

    assert uri not in session._open_versions


def test_notify_changed_on_disk_is_a_no_op_for_a_file_outside_root(tmp_path: Path) -> None:
    # A stray CLI invocation naming a path elsewhere on disk must never involve `ty` at all here -- this
    # session's own cross-file awareness is scoped to its own root (ADR-0041), and reaching outside it
    # would only be work spent on a file this session was never meant to track in the first place.
    client = _StubLSPClient()
    session = _session_with_stub_client(client, root=tmp_path / "repo")
    outside_root = tmp_path / "elsewhere.py"

    session.notify_changed_on_disk(outside_root, "pristine source\n")

    assert client.notify_calls == []


def test_notify_changed_on_disk_sends_did_change_watched_files(tmp_path: Path) -> None:
    client = _StubLSPClient()
    session = _session_with_stub_client(client, root=tmp_path)
    filepath = tmp_path / "callee.py"

    session.notify_changed_on_disk(filepath, "pristine source\n")

    # Not already tracked, so no resync; nothing else tracked either, so no barrier pull.
    assert client.notify_calls == [
        ("workspace/didChangeWatchedFiles", {"changes": [{"uri": filepath.resolve().as_uri(), "type": 2}]})
    ]


def test_notify_changed_on_disk_resyncs_and_barrier_pulls_when_already_tracked(tmp_path: Path) -> None:
    client = _StubLSPClient(hover_result={"items": []})
    session = _session_with_stub_client(client, root=tmp_path)
    filepath = tmp_path / "callee.py"
    uri = filepath.resolve().as_uri()
    session._open_versions[uri] = 1

    session.notify_changed_on_disk(filepath, "pristine source\n")

    assert client.notify_calls == [
        (
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "pristine source\n"}]},
        ),
        ("workspace/didChangeWatchedFiles", {"changes": [{"uri": uri, "type": 2}]}),
    ]


def test_notify_changed_on_disk_never_raises_when_the_server_fails(tmp_path: Path) -> None:
    client = _StubLSPClient(notify_raises=True)
    session = _session_with_stub_client(client, root=tmp_path)

    session.notify_changed_on_disk(tmp_path / "callee.py", "pristine source\n")  # must not raise


def test_notify_changed_on_disk_never_raises_when_the_resync_fails(tmp_path: Path) -> None:
    client = _StubLSPClient(notify_raises=True)
    session = _session_with_stub_client(client, root=tmp_path)
    filepath = tmp_path / "callee.py"
    session._open_versions[filepath.resolve().as_uri()] = 1

    session.notify_changed_on_disk(filepath, "pristine source\n")  # must not raise


def test_notify_changed_on_disk_swallows_a_failed_barrier_pull(tmp_path: Path) -> None:
    # filepath itself is deliberately not already tracked, so notify_changed_on_disk skips straight to
    # didChangeWatchedFiles (a notify, unaffected by hover_raises) and then the barrier pull (a request,
    # which hover_raises does affect) against the one other file that is already tracked.
    client = _StubLSPClient(hover_raises=True)
    session = _session_with_stub_client(client, root=tmp_path)
    already_tracked = tmp_path / "other.py"
    session._open_versions[already_tracked.resolve().as_uri()] = 1
    filepath = tmp_path / "callee.py"

    session.notify_changed_on_disk(filepath, "pristine source\n")  # must not raise


def test_finalize_discards_when_not_persistent(tmp_path: Path) -> None:
    client = _StubLSPClient()
    session = _session_with_stub_client(client, keep_open=False)
    filepath = tmp_path / "f.py"
    uri = filepath.resolve().as_uri()
    session._open_versions[uri] = 1

    session.finalize(filepath, "y = x\n")

    assert client.notify_calls == [("textDocument/didClose", {"textDocument": {"uri": uri}})]
    assert uri not in session._open_versions


def test_finalize_discards_a_file_outside_root_even_when_persistent(tmp_path: Path) -> None:
    # A daemon (keep_open=True) must never adopt a file outside its own root into its own persistent
    # state (ADR-0041) -- treated exactly like a non-persistent session instead: closed, not kept open,
    # so it can never resurface via drain_cross_file_candidates() in some later, unrelated run.
    client = _StubLSPClient()
    session = _session_with_stub_client(client, keep_open=True, root=tmp_path / "repo")
    outside_root = tmp_path / "elsewhere.py"
    uri = outside_root.resolve().as_uri()
    session._open_versions[uri] = 1

    session.finalize(outside_root, "y = x\n")

    assert client.notify_calls == [("textDocument/didClose", {"textDocument": {"uri": uri}})]
    assert uri not in session._open_versions


def test_finalize_resyncs_and_keeps_open_when_persistent(tmp_path: Path) -> None:
    client = _StubLSPClient(hover_result={"items": []})
    session = _session_with_stub_client(client, keep_open=True, root=tmp_path)
    filepath = tmp_path / "f.py"
    uri = filepath.resolve().as_uri()
    session._open_versions[uri] = 1

    session.finalize(filepath, "pristine source\n")

    assert uri in session._open_versions  # still tracked, not discarded
    assert client.notify_calls == [
        (
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "pristine source\n"}]},
        ),
        ("workspace/didChangeWatchedFiles", {"changes": [{"uri": uri, "type": 2}]}),
    ]


def test_finalize_swallows_lsp_error_when_persistent_resync_fails(tmp_path: Path) -> None:
    # finalize() runs from decide_candidates()'s own `finally` -- it must
    # never raise, even when the connection to `ty` is already lost by the
    # time it runs.
    client = _StubLSPClient(notify_raises=True)
    session = _session_with_stub_client(client, keep_open=True, root=tmp_path)
    filepath = tmp_path / "f.py"
    uri = filepath.resolve().as_uri()
    session._open_versions[uri] = 1

    session.finalize(filepath, "pristine source\n")  # must not raise


def test_on_notification_records_a_publish_diagnostics_uri() -> None:
    session = _session_with_stub_client(_StubLSPClient(), keep_open=True)

    session._on_notification("textDocument/publishDiagnostics", {"uri": "file:///caller.py"})

    assert session._dirty_uris == {"file:///caller.py"}


@pytest.mark.parametrize("params", [{"uri": 123}, {}], ids=["uri-not-a-string", "uri-missing"])
def test_on_notification_ignores_a_malformed_uri(params: dict[str, object]) -> None:
    session = _session_with_stub_client(_StubLSPClient(), keep_open=True)

    session._on_notification("textDocument/publishDiagnostics", params)

    assert session._dirty_uris == set()


def test_on_notification_ignores_an_unrelated_method() -> None:
    session = _session_with_stub_client(_StubLSPClient(), keep_open=True)

    session._on_notification("window/logMessage", {"uri": "file:///caller.py"})

    assert session._dirty_uris == set()


def test_drain_cross_file_candidates_excludes_given_paths_and_requires_the_file_still_tracked(
    tmp_path: Path,
) -> None:
    session = _session_with_stub_client(_StubLSPClient(), keep_open=True)
    still_open = tmp_path / "caller.py"
    already_given = tmp_path / "callee.py"
    no_longer_tracked = tmp_path / "closed.py"
    session._open_versions[still_open.resolve().as_uri()] = 1
    session._open_versions[already_given.resolve().as_uri()] = 1
    session._dirty_uris = {
        still_open.resolve().as_uri(),
        already_given.resolve().as_uri(),
        no_longer_tracked.resolve().as_uri(),
    }

    drained = session.drain_cross_file_candidates([already_given])

    assert drained == [still_open.resolve()]
    assert session._dirty_uris == set()  # drained -- a second call returns nothing new


def test_drain_cross_file_candidates_is_empty_when_nothing_is_dirty() -> None:
    session = _session_with_stub_client(_StubLSPClient(), keep_open=True)

    assert session.drain_cross_file_candidates([]) == []


def test_drain_cross_file_candidates_serializes_against_a_concurrent_notification() -> None:
    # _on_notification() runs on LSPClient's own background reader thread while
    # drain_cross_file_candidates() runs on the caller's thread; without _dirty_uris_lock serializing the
    # two, a notification landing between the read and the clear() could be wiped without ever being
    # returned, silently dropping a cross-file reanalysis candidate. Forces the interleaving deterministically
    # (a real race's timing can't be relied on to reproduce it) via a set subclass whose own clear() pauses
    # mid-call, standing in for the exact window between the read and the real clear().
    client = _StubLSPClient()
    session = _session_with_stub_client(client, keep_open=True)
    early_uri = "file:///early.py"
    late_uri = "file:///late.py"
    session._open_versions[early_uri] = 1
    session._open_versions[late_uri] = 1

    entered_critical_section = threading.Event()
    release_critical_section = threading.Event()

    class _SlowClearSet(set[str]):
        def clear(self) -> None:
            entered_critical_section.set()
            release_critical_section.wait(timeout=5)
            super().clear()

    session._dirty_uris = _SlowClearSet({early_uri})

    drained: list[Path] = []
    drain_thread = threading.Thread(target=lambda: drained.extend(session.drain_cross_file_candidates([])))
    drain_thread.start()
    assert entered_critical_section.wait(timeout=5)  # now inside the locked section, mid-clear()

    # Attempts to add a notification while drain is mid-critical-section -- must block on the same lock
    # rather than interleaving with the read-then-clear, or this add would be silently wiped by clear().
    notify_thread = threading.Thread(
        target=session._on_notification, args=("textDocument/publishDiagnostics", {"uri": late_uri})
    )
    notify_thread.start()
    time.sleep(0.05)  # gives the notifier every chance to (wrongly) interleave, if it weren't locked out
    assert notify_thread.is_alive()  # correctly blocked on _dirty_uris_lock, not lost

    release_critical_section.set()
    drain_thread.join(timeout=5)
    notify_thread.join(timeout=5)

    assert not drain_thread.is_alive()
    assert not notify_thread.is_alive()
    assert {path.as_uri() for path in drained} == {early_uri}  # late_uri arrived after this drain -- not in it
    assert session._dirty_uris == {late_uri}  # ...but wasn't lost either, still there for the next drain

    drained_next = session.drain_cross_file_candidates([])
    assert {path.as_uri() for path in drained_next} == {late_uri}


def test_keep_open_registers_on_notification_with_the_lsp_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class _RecordingClient:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def request(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {}

        def notify(self, *_args: object, **_kwargs: object) -> None:
            return

    monkeypatch.setattr(session_module, "LSPClient", _RecordingClient)

    session = TySession(root=tmp_path, keep_open=True)

    assert captured["on_notification"] == session._on_notification


def test_not_keep_open_passes_no_on_notification_to_the_lsp_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class _RecordingClient:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def request(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {}

        def notify(self, *_args: object, **_kwargs: object) -> None:
            return

    monkeypatch.setattr(session_module, "LSPClient", _RecordingClient)

    TySession(root=tmp_path, keep_open=False)

    assert captured["on_notification"] is None


def test_min_ty_version_matches_pyproject_pin() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    dev_dependencies = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["dependency-groups"]["dev"]
    (ty_pin,) = (dep for dep in dev_dependencies if re.match(r"^ty(?:[<>=~!]|$)", dep))
    match = re.fullmatch(r"ty>=([\d.]+)", ty_pin)
    assert match is not None, f"unexpected `ty` pin syntax in pyproject.toml: {ty_pin!r}"
    assert match.group(1) == session_module._MIN_TY_VERSION
