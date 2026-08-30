from __future__ import annotations

import re
import threading
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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
    peek_session,
    record_direct_input_if_session_active,
)

from ._helpers import FakeSession

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolated_session_singleton() -> Iterator[None]:
    original = session_module._session
    original_probe_failed = session_module._daemon_probe_failed
    original_probe_next_retry_at = session_module._daemon_probe_next_retry_at
    session_module._session = None
    session_module._daemon_probe_failed = False
    session_module._daemon_probe_next_retry_at = 0.0
    yield
    session_module._session = original
    session_module._daemon_probe_failed = original_probe_failed
    session_module._daemon_probe_next_retry_at = original_probe_next_retry_at


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

        def close_file(self, _filepath: Path) -> None:
            return

    def _raise_os_error(_root: Path) -> None:
        msg = "simulated: no daemon reachable"
        raise OSError(msg)

    monkeypatch.setattr(daemon_module, "connect", _raise_os_error)
    monkeypatch.setattr(session_module, "_run_self_test", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(session_module, "TySession", _FakeTySession)

    session = get_session()
    assert session is get_session()
    session.close()
    assert sentinel_calls == [1]


def test_local_session_closes_when_its_self_test_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _FakeTySession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def close(self) -> None:
            calls.append("close")

        def close_file(self, _filepath: Path) -> None:
            calls.append("close_file")

    def fail_self_test(*_args: object, **_kwargs: object) -> None:
        raise CheckUnavailableError("simulated: self-test failure")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session_module, "TySession", _FakeTySession)
    monkeypatch.setattr(session_module, "_run_self_test", fail_self_test)

    with pytest.raises(CheckUnavailableError, match="self-test failure"):
        session_module._local_session()

    assert calls == ["close_file", "close_file", "close"]


class _FakeNotifiableSession:
    __slots__ = ("direct_inputs",)

    def __init__(self) -> None:
        self.direct_inputs: list[tuple[Path, str]] = []

    def record_direct_input(self, filepath: Path, source: str) -> None:
        self.direct_inputs.append((filepath, source))

    def close(self) -> None:
        return


def test_peek_session_returns_none_when_no_session_exists() -> None:
    assert peek_session() is None


def test_peek_session_returns_the_active_session() -> None:
    session_module._session = _FakeNotifiableSession()  # type: ignore[assignment]
    assert peek_session() is session_module._session


def test_record_direct_input_if_session_active_uses_the_existing_session(tmp_path: Path) -> None:
    fake = _FakeNotifiableSession()
    session_module._session = fake  # type: ignore[assignment]
    filepath = tmp_path / "callee.py"

    record_direct_input_if_session_active(filepath, "source\n")

    assert fake.direct_inputs == [(filepath, "source\n")]


def test_record_direct_input_if_session_active_promotes_a_probed_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeNotifiableSession()
    monkeypatch.setattr(
        daemon_module,
        "probe_existing",
        lambda _root: daemon_module.ExistingDaemonProbe(cast("daemon_module.RemoteTySession", fake)),
    )
    filepath = tmp_path / "callee.py"

    record_direct_input_if_session_active(filepath, "source\n")

    assert fake.direct_inputs == [(filepath, "source\n")]
    fake.close()


def test_record_direct_input_if_session_active_is_a_no_op_when_no_daemon_is_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "probe_existing", lambda _root: daemon_module.ExistingDaemonProbe(None))

    record_direct_input_if_session_active(tmp_path / "callee.py", "source\n")


def test_record_direct_input_if_session_active_retries_a_transient_probe_after_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_calls: list[Path] = []
    now = 0.0
    fake = _FakeNotifiableSession()

    def _probe(root: Path) -> daemon_module.ExistingDaemonProbe:
        probe_calls.append(root)
        if len(probe_calls) == 1:
            return daemon_module.ExistingDaemonProbe(None)
        return daemon_module.ExistingDaemonProbe(cast("daemon_module.RemoteTySession", fake))

    monkeypatch.setattr(daemon_module, "probe_existing", _probe)
    monkeypatch.setattr(session_module.time, "monotonic", lambda: now)

    record_direct_input_if_session_active(tmp_path / "first.py", "source\n")
    record_direct_input_if_session_active(tmp_path / "second.py", "source\n")
    now = session_module._DAEMON_PROBE_RETRY_INTERVAL_SECONDS
    record_direct_input_if_session_active(tmp_path / "third.py", "source\n")

    assert len(probe_calls) == 2
    assert fake.direct_inputs == [(tmp_path / "third.py", "source\n")]


def test_record_direct_input_if_session_active_memoizes_a_terminal_probe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_calls: list[Path] = []

    def _probe(root: Path) -> daemon_module.ExistingDaemonProbe:
        probe_calls.append(root)
        return daemon_module.ExistingDaemonProbe(None, terminal_failure=True)

    monkeypatch.setattr(daemon_module, "probe_existing", _probe)

    record_direct_input_if_session_active(tmp_path / "first.py", "source\n")
    record_direct_input_if_session_active(tmp_path / "second.py", "source\n")

    assert len(probe_calls) == 1


class _StubLSPClient:
    __slots__ = ("hover_raises", "hover_result", "notify_calls", "notify_hook", "notify_raises")

    def __init__(self, *, hover_result: object = None, hover_raises: bool = False, notify_raises: bool = False) -> None:
        self.hover_result = hover_result
        self.hover_raises = hover_raises
        self.notify_raises = notify_raises
        self.notify_hook: Any = None
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
        if self.notify_hook is not None:
            self.notify_hook(method, params)


def _session_with_stub_client(
    client: _StubLSPClient, *, keep_open: bool = False, root: Path | None = None
) -> TySession:
    session = TySession.__new__(TySession)
    session._root = (root or Path.cwd()).resolve()
    session._client = client  # type: ignore[assignment]
    session._cached_redundancies = {}
    session._cache_identity = session_module._cache_context(session._root)
    session._direct_input_digests = {}
    session._open_versions = {}
    session._dirty_uris = set()
    session._dirty_uris_lock = threading.Lock()
    session._keep_open = keep_open
    session._last_reconciled_digests = {}
    return session


def _session_with_dirty_uris(tmp_path: Path, dirty_uris: list[str]) -> tuple[TySession, _StubLSPClient]:
    client = _StubLSPClient()
    session = _session_with_stub_client(client, keep_open=True, root=tmp_path)

    def record_diagnostics(method: str, _params: dict[str, object]) -> None:
        if method == "workspace/didChangeWatchedFiles":
            for uri in dirty_uris:
                session._on_notification("textDocument/publishDiagnostics", {"uri": uri})

    client.notify_hook = record_diagnostics
    return session, client


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
    client = _StubLSPClient(notify_raises=True)
    session = _session_with_stub_client(client)
    filepath = tmp_path / "opened.py"
    uri = filepath.resolve().as_uri()
    session._open_versions[uri] = 1

    session.close_file(filepath)  # must not raise

    assert uri not in session._open_versions


def test_record_direct_input_ignores_a_file_outside_root(tmp_path: Path) -> None:
    client = _StubLSPClient()
    session = _session_with_stub_client(client, keep_open=True, root=tmp_path / "repo")
    outside_root = tmp_path / "elsewhere.py"

    session.record_direct_input(outside_root, "pristine source\n")

    assert session._direct_input_digests == {}
    assert client.notify_calls == []


def test_cached_redundancies_require_matching_source_within_a_persistent_root(tmp_path: Path) -> None:
    session = _session_with_stub_client(_StubLSPClient(), keep_open=True, root=tmp_path)
    filepath = tmp_path / "module.py"
    redundancies = [("str", 1, 4, "str")]

    session.cache_redundancies(filepath, "value = str(name)\n", "strict", redundancies)

    assert session.cached_redundancies(filepath, "value = str(name)\n", "strict") == redundancies
    assert session.cached_redundancies(filepath, "value = str(name)\n", "permissive") is None
    assert session.cached_redundancies(filepath, "value = str(other)\n", "strict") is None


def test_cached_redundancies_are_invalidated_when_ty_configuration_changes(tmp_path: Path) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.ty]\npython-version = "3.14"\n')
    session = _session_with_stub_client(_StubLSPClient(), keep_open=True, root=tmp_path)
    filepath = tmp_path / "module.py"
    session.cache_redundancies(filepath, "value = str(name)\n", "strict", [("str", 1, 4, "str")])

    config.write_text('[tool.ty]\npython-version = "3.15"\n')

    assert session.cached_redundancies(filepath, "value = str(name)\n", "strict") is None


def test_reconcile_direct_inputs_invalidates_dirty_redundancies(tmp_path: Path) -> None:
    direct = tmp_path / "direct.py"
    dependent = tmp_path / "dependent.py"
    unaffected = tmp_path / "unaffected.py"
    dependent_uri = dependent.resolve().as_uri()
    session, client = _session_with_dirty_uris(tmp_path, [dependent_uri])
    session._open_versions[dependent_uri] = 1
    session.cache_redundancies(dependent, "value = str(name)\n", "strict", [("str", 1, 8, "str")])
    session.cache_redundancies(unaffected, "value = str(name)\n", "strict", [("str", 1, 8, "str")])
    assert client.notify_hook is not None
    client.notify_hook("unrelated", {})

    session.record_direct_input(direct, "source\n")

    assert session.reconcile_direct_inputs() == [dependent.resolve()]
    assert session.cached_redundancies(dependent, "value = str(name)\n", "strict") is None
    assert session.cached_redundancies(unaffected, "value = str(name)\n", "strict") is not None


def test_reconcile_direct_inputs_is_a_no_op_for_nonpersistent_sessions(tmp_path: Path) -> None:
    session = _session_with_stub_client(_StubLSPClient(), keep_open=False, root=tmp_path)

    assert session.reconcile_direct_inputs() == []


def test_await_ty_catching_up_ignores_a_failed_barrier_pull(tmp_path: Path) -> None:
    session = _session_with_stub_client(_StubLSPClient(hover_raises=True), keep_open=True, root=tmp_path)
    session._open_versions[(tmp_path / "opened.py").resolve().as_uri()] = 1

    session._await_ty_catching_up()


def test_reconcile_direct_inputs_batches_changed_files_and_returns_dirty_open_files(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    caller = tmp_path / "caller.py"
    caller_uri = caller.resolve().as_uri()
    session, client = _session_with_dirty_uris(tmp_path, [caller_uri])
    session._open_versions[caller_uri] = 1

    session.record_direct_input(second, "second\n")
    session.record_direct_input(first, "first\n")

    assert session.reconcile_direct_inputs() == [caller.resolve()]

    assert client.notify_calls == [
        (
            "workspace/didChangeWatchedFiles",
            {
                "changes": [
                    {"uri": first.resolve().as_uri(), "type": 2},
                    {"uri": second.resolve().as_uri(), "type": 2},
                ]
            },
        )
    ]


def test_reconcile_direct_inputs_skips_unchanged_content(tmp_path: Path) -> None:
    client = _StubLSPClient(hover_result={"items": []})
    session = _session_with_stub_client(client, keep_open=True, root=tmp_path)
    filepath = tmp_path / "callee.py"
    session.record_direct_input(filepath, "source\n")
    assert session.reconcile_direct_inputs() == []
    session.record_direct_input(filepath, "source\n")
    assert session.reconcile_direct_inputs() == []

    assert len(client.notify_calls) == 1


def test_reconcile_direct_inputs_retains_failed_notifications_for_retry(tmp_path: Path) -> None:
    client = _StubLSPClient(notify_raises=True)
    session = _session_with_stub_client(client, keep_open=True, root=tmp_path)
    filepath = tmp_path / "callee.py"

    session.record_direct_input(filepath, "source\n")

    assert session.reconcile_direct_inputs() == []
    assert filepath.resolve().as_uri() in session._direct_input_digests


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

    assert uri in session._open_versions
    assert client.notify_calls == [
        (
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "pristine source\n"}]},
        ),
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


def test_reconcile_direct_inputs_returns_only_open_dirty_files(tmp_path: Path) -> None:
    client = _StubLSPClient()
    session = _session_with_stub_client(client, keep_open=True, root=tmp_path)
    direct = tmp_path / "direct.py"
    still_open = tmp_path / "caller.py"
    no_longer_open = tmp_path / "closed.py"
    still_open_uri = still_open.resolve().as_uri()
    session._open_versions[still_open_uri] = 1

    def record_diagnostics(method: str, _params: dict[str, object]) -> None:
        if method == "workspace/didChangeWatchedFiles":
            session._on_notification("textDocument/publishDiagnostics", {"uri": still_open_uri})
            session._on_notification("textDocument/publishDiagnostics", {"uri": no_longer_open.resolve().as_uri()})

    client.notify_hook = record_diagnostics
    record_diagnostics("unrelated", {})
    session.record_direct_input(direct, "source\n")

    assert session.reconcile_direct_inputs() == [still_open.resolve()]
    assert session._dirty_uris == set()


@pytest.mark.parametrize("keep_open", [True, False], ids=["keep-open", "not-keep-open"])
def test_on_notification_wiring_matches_keep_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, keep_open: bool
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

    session = TySession(root=tmp_path, keep_open=keep_open)

    assert captured["on_notification"] == (session._on_notification if keep_open else None)


def test_min_ty_version_matches_pyproject_pin() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    dev_dependencies = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["dependency-groups"]["dev"]
    (ty_pin,) = (dep for dep in dev_dependencies if re.match(r"^ty(?:[<>=~!]|$)", dep))
    match = re.fullmatch(r"ty>=([\d.]+)", ty_pin)
    assert match is not None, f"unexpected `ty` pin syntax in pyproject.toml: {ty_pin!r}"
    assert match.group(1) == session_module._MIN_TY_VERSION
