from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import pre_commit_hooks.ast_checks.redundant_type_conversion.session as session_module
from pre_commit_hooks._lsp import LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError
from pre_commit_hooks.ast_checks.redundant_type_conversion.session import (
    TySession,
    _diagnostic_key,
    _run_self_test,
    _spawn,
    get_session,
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
    # Regression: a same-line synthetic rewrite shifts every *other*
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
    # Regression: _spawn only caught FileNotFoundError, so `ty` resolving
    # on PATH but failing to actually launch (missing execute permission
    # here; a corrupt/wrong-format binary raises the same way) surfaced as
    # an uncaught PermissionError instead of this check's own actionable
    # CheckUnavailableError.
    not_executable = tmp_path / "not-really-ty"
    not_executable.write_text("#!/bin/sh\necho fake ty\n")
    not_executable.chmod(0o644)
    monkeypatch.setattr(session_module, "_TY_COMMAND", (str(not_executable),))  # pytriage: ignore=TRI006
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

    monkeypatch.setattr(session_module, "_run_self_test", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(session_module, "TySession", _FakeTySession)

    assert get_session() is get_session()
    # Exactly two TySession constructions total, both from the *first*
    # get_session() call: one throwaway scratch session for the self-test,
    # one long-lived real session handed back to callers. The second
    # get_session() call must construct neither -- that's the whole point
    # of the singleton.
    assert sentinel_calls == [1, 1]


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


def _session_with_stub_client(client: _StubLSPClient) -> TySession:
    session = TySession.__new__(TySession)
    session._client = client  # type: ignore[assignment]
    session._open_versions = {}
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


def test_min_ty_version_matches_pyproject_pin() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    dev_dependencies = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["dependency-groups"]["dev"]
    (ty_pin,) = (dep for dep in dev_dependencies if re.match(r"^ty(?:[<>=~!]|$)", dep))
    match = re.fullmatch(r"ty>=([\d.]+)", ty_pin)
    assert match is not None, f"unexpected `ty` pin syntax in pyproject.toml: {ty_pin!r}"
    assert match.group(1) == session_module._MIN_TY_VERSION
