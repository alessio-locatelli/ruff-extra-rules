from __future__ import annotations

import ast
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

import pre_commit_hooks.ast_checks.redundant_type_conversion.daemon as daemon_module
import pre_commit_hooks.ast_checks.redundant_type_conversion.session as session_module
from pre_commit_hooks._lsp import LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError
from pre_commit_hooks.ast_checks.redundant_type_conversion import RedundantTypeConversionCheck
from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import ConfidenceLevel
from pre_commit_hooks.ast_checks.redundant_type_conversion.daemon import (
    RemoteTySession,
    _accept_loop,
    _dispatch,
    _handle_connection,
    _ShutdownRequestedError,
    _spawn_daemon,
    _ty_version,
    _VersionMismatchError,
    _wait_for_busy_daemon,
    connect,
    shutdown_if_running,
    try_connect_existing,
)
from tests._helpers import restricted_permissions

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def socketpair_peer() -> Iterator[socket.socket]:
    peer_sock, unused_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        yield peer_sock
    finally:
        peer_sock.close()
        unused_sock.close()


class _FakeSession:
    """A `PersistentSession`-conforming stub for `_dispatch`/`_handle_connection` unit tests."""

    __slots__ = ("close_calls", "drained", "hover_delay_seconds", "hover_result", "notified", "raises")

    def __init__(
        self,
        *,
        hover_result: str | None = None,
        drained: list[Path] | None = None,
        raises: bool = False,
        hover_delay_seconds: float = 0.0,
    ) -> None:
        self.hover_result = hover_result
        self.drained = drained or []
        self.notified: list[Path] = []
        self.close_calls = 0
        self.raises = raises
        self.hover_delay_seconds = hover_delay_seconds

    def open_or_update(self, _filepath: Path, _content: str) -> frozenset[tuple[Any, ...]]:
        if self.raises:
            raise LSPError("simulated ty crash")
        return frozenset({("code", "msg", 1, 1)})

    def hover(self, _filepath: Path, _line0: int, _char_utf16: int) -> str | None:
        if self.hover_delay_seconds:
            time.sleep(self.hover_delay_seconds)
        return self.hover_result

    def finalize(self, _filepath: Path, _source: str) -> None:
        return

    def notify_changed_on_disk(self, filepath: Path, _source: str) -> None:
        self.notified.append(filepath)

    def drain_cross_file_candidates(self, _already_processed: list[Path]) -> list[Path]:
        return self.drained

    def close(self) -> None:
        self.close_calls += 1


def test_ty_version_returns_stripped_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="ty 0.0.99\n")
    )
    assert _ty_version() == "ty 0.0.99"


@pytest.mark.parametrize(
    "run",
    [
        pytest.param(lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("ty")), id="ty-missing"),
        pytest.param(
            lambda *_a, **_k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["ty"])),
            id="ty-exits-non-zero",
        ),
    ],
)
def test_ty_version_normalizes_any_failure_to_os_error(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:  # noqa: ANN401
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OSError, match="could not determine"):
        _ty_version()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            {"op": "open_or_update", "filepath": "f.py", "content": "x"},
            # A tuple, not a list -- json.dumps() serializes both identically as a JSON array, so
            # _dispatch() no longer converts one to the other before this ever reaches the wire.
            {"result": [("code", "msg", 1, 1)]},
        ),
        ({"op": "hover", "filepath": "f.py", "line0": 0, "char_utf16": 0}, {"result": "str"}),
        ({"op": "finalize", "filepath": "f.py", "source": "x"}, {"result": None}),
        ({"op": "notify_changed_on_disk", "filepath": "f.py", "source": "x"}, {"result": None}),
        ({"op": "drain_cross_file_candidates", "exclude": []}, {"result": []}),
        ({"op": "bogus"}, {"error": "unknown op: 'bogus'"}),
    ],
    ids=["open_or_update", "hover", "finalize", "notify_changed_on_disk", "drain_cross_file_candidates", "unknown-op"],
)
def test_dispatch_routes_known_ops(message: dict[str, Any], expected: dict[str, Any]) -> None:
    session = _FakeSession(hover_result="str")
    assert _dispatch(message, session) == expected


def test_dispatch_converts_lsp_error_to_an_error_response() -> None:
    session = _FakeSession(raises=True)
    response = _dispatch({"op": "open_or_update", "filepath": "f.py", "content": "x"}, session)
    assert "error" in response
    assert "simulated ty crash" in response["error"]


def test_dispatch_converts_a_malformed_request_to_an_error_response() -> None:
    # Client and daemon are always the same, handshake-matched code, so a missing field should never
    # happen in practice -- but it must not escape as a raw KeyError/TypeError and end this whole
    # daemon's process (ADR-0041) the way an uncaught exception escaping _accept_loop already would.
    session = _FakeSession()
    response = _dispatch({"op": "open_or_update", "content": "x"}, session)  # missing "filepath"
    assert "error" in response
    assert "open_or_update" in response["error"]


def test_call_raises_lsp_error_when_the_connection_is_already_broken() -> None:
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.close()
    client = RemoteTySession(client_sock)

    with pytest.raises(LSPError, match="connection failed"):
        client.hover(Path("f.py"), 0, 0)

    client.close()


def test_call_raises_lsp_error_when_the_daemon_closes_without_responding() -> None:
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client = RemoteTySession(client_sock)

    def _server() -> None:
        with server_sock:
            daemon_module.read_framed_message(server_sock.makefile("rb"))  # consume the request, then just close

    thread = threading.Thread(target=_server)
    thread.start()
    try:
        with pytest.raises(LSPError, match="closed the connection"):
            client.hover(Path("f.py"), 0, 0)
    finally:
        thread.join(timeout=5)
        client.close()


def test_call_raises_lsp_error_when_the_daemon_reports_an_error() -> None:
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client = RemoteTySession(client_sock)

    def _server() -> None:
        with server_sock:
            rfile = server_sock.makefile("rb")
            wfile = server_sock.makefile("wb")
            daemon_module.read_framed_message(rfile)
            daemon_module.write_framed_message(wfile, {"error": "simulated failure"})

    thread = threading.Thread(target=_server)
    thread.start()
    try:
        with pytest.raises(LSPError, match="simulated failure"):
            client.hover(Path("f.py"), 0, 0)
    finally:
        thread.join(timeout=5)
        client.close()


def test_handle_connection_rejects_a_version_mismatch() -> None:
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with server_sock, client_sock:
        client = RemoteTySession(client_sock)
        daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "old-version"})

        with pytest.raises(_VersionMismatchError):
            _handle_connection(server_sock, _FakeSession(), ty_version="new-version", session_lock=threading.Lock())

        response = daemon_module.read_framed_message(client._rfile)
        assert response == {"error": "version_mismatch"}


def test_handle_connection_serves_a_real_round_trip_over_a_background_thread() -> None:
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    session = _FakeSession(hover_result="int")

    def _run_server() -> None:
        with server_sock, pytest.raises(_ShutdownRequestedError):
            _handle_connection(server_sock, session, ty_version="v1", session_lock=threading.Lock())

    server_thread = threading.Thread(target=_run_server)
    server_thread.start()
    try:
        with client_sock:
            client = RemoteTySession(client_sock)
            daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "v1"})
            handshake_response = daemon_module.read_framed_message(client._rfile)
            assert handshake_response == {"result": "ok"}

            assert client.hover(Path("f.py"), 0, 0) == "int"
            assert client.drain_cross_file_candidates([]) == []
            client._call("shutdown")
    finally:
        server_thread.join(timeout=5)
        assert not server_thread.is_alive()


def test_handle_connection_returns_when_the_client_disconnects_before_handshake() -> None:
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client_sock.close()  # disconnect immediately, before ever sending a handshake

    with server_sock:
        _handle_connection(
            # must return, not raise
            server_sock,
            _FakeSession(),
            ty_version="v1",
            session_lock=threading.Lock(),
        )


def test_handle_connection_returns_when_the_client_disconnects_mid_session() -> None:
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client = RemoteTySession(client_sock)
    daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "v1"})

    def _server() -> None:
        with server_sock:
            _handle_connection(
                # must return, not raise
                server_sock,
                _FakeSession(),
                ty_version="v1",
                session_lock=threading.Lock(),
            )

    thread = threading.Thread(target=_server)
    thread.start()
    handshake_response = daemon_module.read_framed_message(client._rfile)
    assert handshake_response == {"result": "ok"}
    client.close()  # disconnect without ever sending "shutdown"
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_accept_loop_ends_when_a_served_connection_requests_shutdown(tmp_path: Path) -> None:
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(1)

        def _client() -> None:
            client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with client_sock:
                client_sock.connect(str(socket_path))
                client = RemoteTySession(client_sock)
                daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "v1"})
                daemon_module.read_framed_message(client._rfile)
                client._call("shutdown")

        thread = threading.Thread(target=_client)
        thread.start()
        _accept_loop(sock, _FakeSession(), ty_version="v1")  # must return once the client asks to shut down
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_accept_loop_ends_on_a_version_mismatch(tmp_path: Path) -> None:
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(1)

        def _client() -> None:
            client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with client_sock:
                client_sock.connect(str(socket_path))
                daemon_module.write_framed_message(
                    client_sock.makefile("wb"), {"op": "handshake", "ty_version": "mismatched"}
                )
                # Waits for the daemon's own error response before disconnecting -- closing
                # immediately after the write races the daemon's own reply with a BrokenPipeError.
                daemon_module.read_framed_message(client_sock.makefile("rb"))

        thread = threading.Thread(target=_client)
        thread.start()
        _accept_loop(sock, _FakeSession(), ty_version="v1")  # must return once the mismatch is detected
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_accept_loop_returns_on_idle_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # _accept_loop now owns sock's own timeout itself (min(_ACCEPT_POLL_INTERVAL_SECONDS,
    # _IDLE_TIMEOUT_SECONDS)), so shortening _IDLE_TIMEOUT_SECONDS -- not sock.settimeout() directly -- is
    # what makes this test return promptly.
    monkeypatch.setattr(daemon_module, "_IDLE_TIMEOUT_SECONDS", 0.2)
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    session = _FakeSession()
    with sock:
        sock.bind(str(socket_path))
        sock.listen(1)

        _accept_loop(sock, session, ty_version="v1")  # must return promptly, not hang

    # Mirrors what _serve()'s own finally block does once _accept_loop returns.
    session.close()
    assert session.close_calls == 1


def test_accept_loop_drops_a_stalled_connection_and_still_serves_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without its own per-connection timeout, a connection that's accepted and then never completes a
    # request would block _handle_connection forever, wedging this whole daemon -- it would never return
    # to accept() for any later, well-behaved client (see ADR-0041).
    monkeypatch.setattr(daemon_module, "_CLIENT_REQUEST_TIMEOUT_SECONDS", 0.2)
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(2)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stalled_sock:
            stalled_sock.connect(str(socket_path))  # connects, then never sends a single byte

            def _well_behaved_client() -> None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client_sock:
                    client_sock.connect(str(socket_path))
                    client = RemoteTySession(client_sock)
                    daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "v1"})
                    response = daemon_module.read_framed_message(client._rfile)
                    assert response == {"result": "ok"}
                    client._call("shutdown")

            thread = threading.Thread(target=_well_behaved_client)
            thread.start()

            # Must drop the stalled connection once it times out and still serve the next, real client.
            _accept_loop(sock, _FakeSession(), ty_version="v1")

            thread.join(timeout=5)
            assert not thread.is_alive()


def test_accept_loop_drops_a_connection_with_a_truncated_frame_and_still_serves_the_next_one(
    tmp_path: Path,
) -> None:
    # An interrupted client (or one that disconnects mid-request) leaves read_framed_message() raising
    # LSPError rather than returning a clean None -- an ordinary client-side failure, not this daemon's
    # own, so it must not escape _accept_loop and end this whole daemon's process (ADR-0041), discarding
    # every other file it had been tracking over the one connection that actually failed.
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(2)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as truncated_sock:
            truncated_sock.connect(str(socket_path))
            # Claims a 10-byte body but sends only 5 -- read_framed_message() raises LSPError once the
            # connection closes (via this `with` block's own exit) before the rest ever arrives.
            truncated_sock.sendall(b'Content-Length: 10\r\n\r\n{"a":')

        def _well_behaved_client() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client_sock:
                client_sock.connect(str(socket_path))
                client = RemoteTySession(client_sock)
                daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "v1"})
                response = daemon_module.read_framed_message(client._rfile)
                assert response == {"result": "ok"}
                client._call("shutdown")

        thread = threading.Thread(target=_well_behaved_client)
        thread.start()

        _accept_loop(sock, _FakeSession(), ty_version="v1")

        thread.join(timeout=5)
        assert not thread.is_alive()


def test_accept_loop_drops_a_connection_whose_reply_write_fails_and_still_serves_the_next_one(
    tmp_path: Path,
) -> None:
    # A client that disconnects right after sending its own request, before this daemon ever gets a
    # chance to reply, makes that reply's own write raise a plain OSError (BrokenPipeError or similar) --
    # an ordinary client-side failure, not this daemon's own, so it must not escape _accept_loop either.
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(2)

        def _rude_client() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as rude_sock:
                rude_sock.connect(str(socket_path))
                daemon_module.write_framed_message(rude_sock.makefile("wb"), {"op": "handshake", "ty_version": "v1"})
                # Disconnects immediately, before ever reading this daemon's own handshake reply.

        rude_thread = threading.Thread(target=_rude_client)
        rude_thread.start()
        rude_thread.join(timeout=5)
        time.sleep(0.2)  # gives the kernel time to fully process the disconnect before accept() below

        def _well_behaved_client() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client_sock:
                client_sock.connect(str(socket_path))
                client = RemoteTySession(client_sock)
                daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "v1"})
                response = daemon_module.read_framed_message(client._rfile)
                assert response == {"result": "ok"}
                client._call("shutdown")

        thread = threading.Thread(target=_well_behaved_client)
        thread.start()

        _accept_loop(sock, _FakeSession(), ty_version="v1")

        thread.join(timeout=5)
        assert not thread.is_alive()


def test_accept_loop_serves_a_new_connections_handshake_while_another_is_mid_request(tmp_path: Path) -> None:
    # Proves connections are handled concurrently (ADR-0041), not one at a time: a second client's own
    # handshake must not wait behind a first client's own slow, in-flight request -- only the actual calls
    # into `session` are serialized (`session_lock`), never the accept loop or framing.
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    session = _FakeSession(hover_result="int", hover_delay_seconds=1.0)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(2)

        server_thread = threading.Thread(target=_accept_loop, args=(sock, session, "v1"))
        server_thread.start()

        def _slow_client() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client_sock:
                client_sock.connect(str(socket_path))
                client = RemoteTySession(client_sock)
                daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "v1"})
                daemon_module.read_framed_message(client._rfile)
                assert client.hover(Path("f.py"), 0, 0) == "int"
                client._call("shutdown")

        slow_thread = threading.Thread(target=_slow_client)
        slow_thread.start()
        time.sleep(0.2)  # lets the slow client's own hover() request actually start (and hold the lock)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as fast_client_sock:
            start = time.monotonic()
            fast_client_sock.connect(str(socket_path))
            daemon_module.write_framed_message(fast_client_sock.makefile("wb"), {"op": "handshake", "ty_version": "v1"})
            response = daemon_module.read_framed_message(fast_client_sock.makefile("rb"))
            handshake_elapsed = time.monotonic() - start

        assert response == {"result": "ok"}
        assert handshake_elapsed < 0.5  # well under the slow client's own 1s hold -- proves concurrency

        slow_thread.join(timeout=5)
        assert not slow_thread.is_alive()
        server_thread.join(timeout=5)
        assert not server_thread.is_alive()


def test_accept_loop_serializes_concurrent_calls_into_the_shared_session(tmp_path: Path) -> None:
    # Proves the other half of ADR-0041's own design: even though connections are handled concurrently,
    # `_lsp.LSPClient` is documented as not thread-safe for concurrent request()/notify() calls, so no two
    # calls into the shared `session` may ever actually overlap.
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    concurrent_calls = 0
    max_concurrent_calls = 0
    calls_lock = threading.Lock()

    class _ConcurrencyTrackingSession(_FakeSession):
        __slots__ = ()

        def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None:
            nonlocal concurrent_calls, max_concurrent_calls
            with calls_lock:
                concurrent_calls += 1
                max_concurrent_calls = max(max_concurrent_calls, concurrent_calls)
            try:
                return super().hover(filepath, line0, char_utf16)
            finally:
                with calls_lock:
                    concurrent_calls -= 1

    session = _ConcurrencyTrackingSession(hover_result="int", hover_delay_seconds=0.2)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(4)

        def _client() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client_sock:
                client_sock.connect(str(socket_path))
                client = RemoteTySession(client_sock)
                daemon_module.write_framed_message(client._wfile, {"op": "handshake", "ty_version": "v1"})
                daemon_module.read_framed_message(client._rfile)
                assert client.hover(Path("f.py"), 0, 0) == "int"
                client._call("shutdown")

        threads = [threading.Thread(target=_client) for _ in range(3)]
        for thread in threads:
            thread.start()
            time.sleep(0.05)  # stagger connects so all three overlap mid-request, not just mid-accept

        _accept_loop(sock, session, ty_version="v1")

        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

    assert max_concurrent_calls == 1


def test_try_connect_existing_returns_none_when_nothing_is_listening(tmp_path: Path) -> None:
    assert try_connect_existing(tmp_path) is None


def test_repository_root_reuses_a_daemon_path_from_subdirectories_and_path_aliases(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run([git, "init", "-q", str(repository)], check=True)  # noqa: S603
    nested = repository / "nested"
    nested.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    socket_path = daemon_module._socket_path(repository.resolve())
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()

    assert daemon_module.repository_root(nested) == repository.resolve()
    assert daemon_module.repository_root(alias / "nested") == repository.resolve()
    assert daemon_module.socket_exists_for(nested)
    assert daemon_module.socket_exists_for(alias / "nested")


def test_try_connect_existing_returns_none_when_ty_version_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A socket file must actually exist here, or the cheap socket_exists_for() check short-circuits
    # before ever reaching _ty_version() at all.
    socket_path = daemon_module._socket_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    monkeypatch.setattr(
        daemon_module, "_ty_version", lambda: (_ for _ in ()).throw(OSError("simulated: ty not on PATH"))
    )
    probe = daemon_module.probe_existing(tmp_path)

    assert probe.session is None
    assert probe.terminal_failure


def test_try_connect_existing_cleans_up_a_crashed_daemons_stale_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A socket left behind by a daemon that crashed (no live process behind its own pidfile) would
    # otherwise widen get_prefilter_pattern()'s own "every file" fallback indefinitely in a repository
    # where no later, candidate-containing commit happens to run connect() to notice and replace it.
    socket_path = daemon_module._socket_path(tmp_path)
    pid_path = daemon_module._pid_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    pid_path.write_text("999999999", encoding="utf-8")  # not a real, live process
    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", lambda *_a: None)

    assert try_connect_existing(tmp_path) is None

    assert not socket_path.exists()
    assert not pid_path.exists()


def test_try_connect_existing_leaves_a_departing_but_still_alive_daemons_socket_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A daemon that's still alive but shutting itself down for a version mismatch owns its own cleanup
    # (ADR-0041) -- this candidate-less, non-spawning path must not race it by touching the same files.
    socket_path = daemon_module._socket_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")

    def _raise_mismatch(*_args: object) -> socket.socket | None:
        raise daemon_module._VersionMismatchError

    monkeypatch.setattr(daemon_module, "_try_connect", _raise_mismatch)

    assert try_connect_existing(tmp_path) is None

    assert socket_path.exists()  # left alone -- the still-alive, departing daemon owns cleaning this up


def test_try_connect_existing_waits_for_a_busy_daemon_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socketpair_peer: socket.socket
) -> None:
    # A daemon confirmed alive but too busy to answer must be waited for here too, not treated as
    # unreachable: this call's own result is cached and reused for the rest of the run (see ADR-0041),
    # so giving up on the first overlapping hook invocation would drop a disk-change notification for
    # every later candidate-less file in that same run.
    socket_path = daemon_module._socket_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    peer_sock = socketpair_peer

    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", lambda *_a: None)
    monkeypatch.setattr(daemon_module, "_daemon_process_is_alive", lambda _root: True)
    monkeypatch.setattr(daemon_module, "_wait_for_busy_daemon", lambda *_a: (peer_sock, False))

    session = try_connect_existing(tmp_path)

    assert session is not None
    assert session._sock is peer_sock
    session.close()


def test_try_connect_existing_gives_up_when_a_busy_daemon_never_frees_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = daemon_module._socket_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()

    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", lambda *_a: None)
    monkeypatch.setattr(daemon_module, "_daemon_process_is_alive", lambda _root: True)
    monkeypatch.setattr(daemon_module, "_wait_for_busy_daemon", lambda *_a: (None, False))

    assert try_connect_existing(tmp_path) is None


def test_try_connect_returns_none_when_the_socket_path_is_stale(tmp_path: Path) -> None:
    # A regular file left at the socket path (e.g. by a daemon that crashed before ever reaching its own
    # bind()) makes connect() itself fail with an OSError distinct from "nothing there at all" -- also
    # ambiguous with "busy," left for the caller's own liveness/busy-wait logic to resolve.
    socket_path = tmp_path / "d.sock"
    socket_path.touch()

    assert daemon_module._try_connect(socket_path, "v1") is None


def test_try_connect_closes_the_handshake_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_path = tmp_path / "d.sock"
    socket_path.touch()
    sock = Mock()
    wfile = Mock()
    sock.makefile.side_effect = [Mock(), wfile]
    monkeypatch.setattr(daemon_module.socket, "socket", Mock(return_value=sock))
    monkeypatch.setattr(daemon_module, "read_framed_message", Mock(return_value={"result": "ok"}))

    assert daemon_module._try_connect(socket_path, "v1") is sock

    wfile.close.assert_called_once_with()


def test_try_connect_raises_version_mismatch_on_a_handshake_error_response(tmp_path: Path) -> None:
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(1)
        sock.settimeout(5.0)

        def _server() -> None:
            conn, _peer = sock.accept()
            with conn:
                daemon_module.read_framed_message(conn.makefile("rb"))
                daemon_module.write_framed_message(conn.makefile("wb"), {"error": "version_mismatch"})

        thread = threading.Thread(target=_server)
        thread.start()
        try:
            with pytest.raises(_VersionMismatchError):
                daemon_module._try_connect(socket_path, "v1")
        finally:
            thread.join(timeout=5)


def test_try_connect_returns_none_when_the_daemon_disconnects_without_answering(tmp_path: Path) -> None:
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(1)
        sock.settimeout(5.0)

        def _server() -> None:
            conn, _peer = sock.accept()
            with conn:
                daemon_module.read_framed_message(conn.makefile("rb"))
                # Disconnects without ever answering the handshake -- e.g. a daemon that crashed mid-handshake.
                # Ambiguous with "busy," unlike an explicit version-mismatch rejection above.

        thread = threading.Thread(target=_server)
        thread.start()
        try:
            assert daemon_module._try_connect(socket_path, "v1") is None
        finally:
            thread.join(timeout=5)


def test_try_connect_returns_none_when_the_handshake_response_is_malformed(tmp_path: Path) -> None:
    # A truncated/malformed handshake response (a daemon that died mid-write, or a stale socket
    # answering with garbage) makes read_framed_message() raise LSPError -- as unusable a connection as
    # an OSError, so it must be treated the same way rather than escaping uncaught and being recorded as
    # a rule failure instead of falling back to a local session.
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(1)
        sock.settimeout(5.0)

        def _server() -> None:
            conn, _peer = sock.accept()
            with conn:
                daemon_module.read_framed_message(conn.makefile("rb"))
                # Claims a 10-byte body but sends only 5, then disconnects -- read_framed_message() on
                # the client side raises LSPError once it sees the connection close before the rest
                # of the promised body ever arrives.
                conn.sendall(b'Content-Length: 10\r\n\r\n{"a":')

        thread = threading.Thread(target=_server)
        thread.start()
        try:
            assert daemon_module._try_connect(socket_path, "v1") is None
        finally:
            thread.join(timeout=5)


def test_try_connect_or_departing_converts_version_mismatch_to_a_departing_result(tmp_path: Path) -> None:
    socket_path = tmp_path / "d.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        sock.bind(str(socket_path))
        sock.listen(1)
        sock.settimeout(5.0)

        def _server() -> None:
            conn, _peer = sock.accept()
            with conn:
                daemon_module.read_framed_message(conn.makefile("rb"))
                daemon_module.write_framed_message(conn.makefile("wb"), {"error": "version_mismatch"})

        thread = threading.Thread(target=_server)
        thread.start()
        try:
            result_sock, departing = daemon_module._try_connect_or_departing(socket_path, "v1")
            assert result_sock is None
            assert departing is True
        finally:
            thread.join(timeout=5)


def test_connect_reuses_a_daemon_spawned_by_a_peer_while_waiting_for_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socketpair_peer: socket.socket
) -> None:
    # Simulates another process spawning a daemon in the window between this
    # process's own first _try_connect attempt and acquiring the spawn lock.
    peer_sock = socketpair_peer
    attempts: list[socket.socket | None] = [None, peer_sock]  # pytriage: TR5 -- mutated by pop() on every call below

    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", lambda *_a: attempts.pop(0))
    spawn_calls: list[Path] = []
    monkeypatch.setattr(daemon_module, "_spawn_daemon", spawn_calls.append)

    session = connect(tmp_path)

    assert spawn_calls == []  # never actually spawned -- the retry inside the lock found the peer's daemon
    assert session._sock is peer_sock
    session.close()


def test_connect_remembers_a_pre_lock_version_mismatch_even_once_the_daemon_has_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socketpair_peer: socket.socket
) -> None:
    # The pre-lock attempt can observe an explicit version-mismatch rejection, but by the time this
    # process acquires the spawn lock the old daemon may have already removed its own socket entirely --
    # at that point the in-lock attempt alone sees a plain "nothing answered," indistinguishable from
    # busy, unless the earlier mismatch is carried forward: it must still count as departing rather than
    # triggering the busy-daemon wait for a daemon that already confirmed it's not coming back.
    peer_sock = socketpair_peer
    call_count = 0

    def _try_connect_side_effect(_socket_path: Path, _client_ty_version: str) -> socket.socket | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise daemon_module._VersionMismatchError  # pre-lock: explicit rejection
        if call_count == 2:
            return None  # in-lock: the old daemon has already vanished, socket and all
        return peer_sock  # the attempt right after _spawn_daemon() finds the fresh replacement

    daemon_alive_check = Mock()

    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", _try_connect_side_effect)
    monkeypatch.setattr(daemon_module, "_daemon_process_is_alive", daemon_alive_check)
    monkeypatch.setattr(daemon_module, "_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS", 5.0)
    spawn_calls: list[Path] = []
    monkeypatch.setattr(daemon_module, "_spawn_daemon", spawn_calls.append)

    start = time.monotonic()
    session = connect(tmp_path)
    elapsed = time.monotonic() - start

    daemon_alive_check.assert_not_called()  # already known to be departing -- must not check liveness at all
    assert spawn_calls == [tmp_path]
    assert session._sock is peer_sock
    assert elapsed < 5.0  # must not wait out the busy-daemon retry budget
    session.close()


def test_connect_raises_os_error_when_spawn_succeeds_but_the_socket_stays_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", lambda *_a: None)
    monkeypatch.setattr(daemon_module, "_spawn_daemon", lambda _root: None)

    with pytest.raises(OSError, match="did not become reachable"):
        connect(tmp_path)


def test_daemon_process_is_alive_is_false_with_no_pidfile(tmp_path: Path) -> None:
    assert daemon_module._daemon_process_is_alive(tmp_path) is False


def test_daemon_process_is_alive_is_false_for_a_dead_pid(tmp_path: Path) -> None:
    pid_path = daemon_module._pid_path(tmp_path)
    pid_path.parent.mkdir(parents=True)
    # Waited on below, so its own PID is already reaped and no longer alive by the time it's recorded.
    dead_process = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_process.wait(timeout=5)
    pid_path.write_text(str(dead_process.pid), encoding="utf-8")

    assert daemon_module._daemon_process_is_alive(tmp_path) is False


def test_daemon_process_is_alive_is_true_for_the_current_process(tmp_path: Path) -> None:
    pid_path = daemon_module._pid_path(tmp_path)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    assert daemon_module._daemon_process_is_alive(tmp_path) is True


def test_daemon_process_is_alive_is_true_when_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = daemon_module._pid_path(tmp_path)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def _raise_permission_error(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(daemon_module.os, "kill", _raise_permission_error)

    # Exists, just not killable by this user -- conservatively assumed alive.
    assert daemon_module._daemon_process_is_alive(tmp_path) is True


def test_daemon_process_is_alive_is_false_for_unparseable_pidfile_content(tmp_path: Path) -> None:
    pid_path = daemon_module._pid_path(tmp_path)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("not-a-pid", encoding="utf-8")

    assert daemon_module._daemon_process_is_alive(tmp_path) is False


def test_cleanup_if_still_owned_removes_its_own_socket_and_pidfile(tmp_path: Path) -> None:
    socket_path = daemon_module._socket_path(tmp_path)
    pid_path = daemon_module._pid_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    daemon_module._cleanup_if_still_owned(tmp_path)

    assert not socket_path.exists()
    assert not pid_path.exists()


def test_cleanup_if_still_owned_removes_files_when_no_pidfile_exists_yet(tmp_path: Path) -> None:
    # A daemon that fails before ever reaching its own pid_path.write_text() (e.g. a bind failure) must
    # still be able to clean up whatever it already created (the stale-socket unlink at _serve()'s own
    # startup already handles the socket; this covers the finally block reaching the same state).
    socket_path = daemon_module._socket_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()

    daemon_module._cleanup_if_still_owned(tmp_path)

    assert not socket_path.exists()


def test_cleanup_if_still_owned_leaves_a_replacement_daemons_files_alone(tmp_path: Path) -> None:
    # A replacement daemon spawned right after this one shuts down for a version mismatch (ADR-0041) can
    # already be bound and serving, with its own PID recorded, by the time this daemon's own belated
    # finally-block cleanup runs -- it must not delete files it no longer owns.
    socket_path = daemon_module._socket_path(tmp_path)
    pid_path = daemon_module._pid_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    replacement_pid = os.getpid() + 1
    pid_path.write_text(str(replacement_pid), encoding="utf-8")

    daemon_module._cleanup_if_still_owned(tmp_path)

    assert socket_path.exists()
    assert pid_path.read_text(encoding="utf-8") == str(replacement_pid)  # pytriage: TR6 -- read_text() returns str


def test_connect_waits_for_a_busy_daemon_instead_of_spawning_a_competing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socketpair_peer: socket.socket
) -> None:
    peer_sock = socketpair_peer
    attempts: list[socket.socket | None] = [None, None, None, peer_sock]

    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", lambda *_a: attempts.pop(0))
    monkeypatch.setattr(daemon_module, "_daemon_process_is_alive", lambda _root: True)
    monkeypatch.setattr(daemon_module, "_BUSY_DAEMON_RETRY_INTERVAL_SECONDS", 0.01)
    spawn_daemon = Mock()
    monkeypatch.setattr(daemon_module, "_spawn_daemon", spawn_daemon)

    session = connect(tmp_path)

    spawn_daemon.assert_not_called()  # existing daemon confirmed alive -- must not spawn a competing one
    assert session._sock is peer_sock
    session.close()


def test_connect_raises_os_error_when_a_busy_daemon_never_frees_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", lambda *_a: None)
    monkeypatch.setattr(daemon_module, "_daemon_process_is_alive", lambda _root: True)
    monkeypatch.setattr(daemon_module, "_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(daemon_module, "_BUSY_DAEMON_RETRY_INTERVAL_SECONDS", 0.02)
    spawn_daemon = Mock()
    monkeypatch.setattr(daemon_module, "_spawn_daemon", spawn_daemon)

    with pytest.raises(OSError, match="stayed busy"):
        connect(tmp_path)

    spawn_daemon.assert_not_called()  # existing daemon confirmed alive -- must not spawn a competing one


def test_connect_spawns_a_replacement_when_a_busy_daemon_turns_out_to_be_departing_mid_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socketpair_peer: socket.socket
) -> None:
    # A daemon confirmed alive and busy at first can still turn out to be departing (a version mismatch
    # discovered partway through the busy-wait, see _wait_for_busy_daemon()) -- this must spawn its
    # replacement immediately rather than reporting a misleading "stayed busy" failure and falling back.
    peer_sock = socketpair_peer
    call_count = 0

    def _try_connect_side_effect(_socket_path: Path, _client_ty_version: str) -> socket.socket | None:
        nonlocal call_count
        call_count += 1
        # Calls 1-2 are the pre-lock and in-lock attempts, both plainly busy (not yet known departing).
        return peer_sock if call_count >= 3 else None

    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", _try_connect_side_effect)
    monkeypatch.setattr(daemon_module, "_daemon_process_is_alive", lambda _root: True)
    monkeypatch.setattr(daemon_module, "_wait_for_busy_daemon", lambda *_a: (None, True))
    spawn_calls: list[Path] = []
    monkeypatch.setattr(daemon_module, "_spawn_daemon", spawn_calls.append)

    session = connect(tmp_path)

    assert spawn_calls == [tmp_path]
    assert session._sock is peer_sock
    session.close()


def test_connect_spawns_a_fresh_daemon_immediately_after_a_version_mismatch_instead_of_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socketpair_peer: socket.socket
) -> None:
    # A version-mismatched daemon is already shutting itself down, not merely busy -- waiting for it the
    # way connect() waits for a busy one would burn the full busy-daemon retry budget on an answer that
    # was never coming, instead of spawning its replacement right away (see ADR-0041).
    peer_sock = socketpair_peer
    call_count = 0

    def _try_connect_side_effect(_socket_path: Path, _client_ty_version: str) -> socket.socket | None:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:  # the pre-lock and in-lock attempts both find the departing daemon
            raise daemon_module._VersionMismatchError
        return peer_sock  # the attempt right after _spawn_daemon() finds the fresh replacement

    daemon_alive_check = Mock()

    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "_try_connect", _try_connect_side_effect)
    monkeypatch.setattr(daemon_module, "_daemon_process_is_alive", daemon_alive_check)
    monkeypatch.setattr(daemon_module, "_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS", 5.0)
    spawn_calls: list[Path] = []
    monkeypatch.setattr(daemon_module, "_spawn_daemon", spawn_calls.append)

    start = time.monotonic()
    session = connect(tmp_path)
    elapsed = time.monotonic() - start

    daemon_alive_check.assert_not_called()  # already known to be departing -- must not check liveness at all
    assert spawn_calls == [tmp_path]
    assert session._sock is peer_sock
    assert elapsed < 5.0  # must not wait out the busy-daemon retry budget
    session.close()


def test_wait_for_busy_daemon_bails_out_early_once_the_daemon_starts_departing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A daemon confirmed alive can still turn out to be departing (e.g. a peer's own version-mismatched
    # handshake) partway through the busy-wait retry loop -- this must return None immediately rather than
    # burning the rest of the retry budget on an answer that's never coming.
    monkeypatch.setattr(daemon_module, "_BUSY_DAEMON_RETRY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(daemon_module, "_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS", 5.0)
    attempts: list[socket.socket | None] = [None]

    def _try_connect_side_effect(_socket_path: Path, _client_ty_version: str) -> socket.socket | None:
        if attempts:
            return attempts.pop(0)
        raise daemon_module._VersionMismatchError

    monkeypatch.setattr(daemon_module, "_try_connect", _try_connect_side_effect)

    start = time.monotonic()
    sock, departing = _wait_for_busy_daemon(Path("unused.sock"), "v1")
    elapsed = time.monotonic() - start

    assert sock is None
    assert departing is True
    assert elapsed < 5.0  # must not wait out the full retry budget


def test_shutdown_if_running_is_a_silent_no_op_when_nothing_is_listening(tmp_path: Path) -> None:
    shutdown_if_running(tmp_path)  # must not raise


def test_connect_raises_os_error_when_ty_is_entirely_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A totally-missing ty fails this client's own _ty_version() call before connect() ever tries to reach
    # or spawn a daemon -- an operational failure (OSError), not a self-test failure (CheckUnavailableError):
    # get_session()'s own caller is the one that falls back to a local session from here.
    monkeypatch.setattr(
        daemon_module, "_ty_version", lambda: (_ for _ in ()).throw(OSError("simulated: ty not on PATH"))
    )
    with pytest.raises(OSError, match="simulated"):
        connect(tmp_path)


def test_connect_raises_os_error_instead_of_asserting_when_locking_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # locked() itself asserts fcntl is not None -- connect() must check locking_is_available() and raise
    # a plain, fallback-worthy OSError before ever reaching that, or a platform without fcntl would crash
    # with an uncaught AssertionError instead of falling back to a local session the way every other
    # operational failure here does.
    monkeypatch.setattr(daemon_module, "_ty_version", lambda: "v1")
    monkeypatch.setattr(daemon_module, "locking_is_available", lambda: False)
    with pytest.raises(OSError, match="locking is unavailable"):
        connect(tmp_path)


_REAL_POPEN = subprocess.Popen


def test_spawn_daemon_raises_check_unavailable_error_on_a_reported_self_test_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_popen(_args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        return _REAL_POPEN([sys.executable, "-c", "print('FAILED: simulated self-test failure', flush=True)"], **kwargs)

    monkeypatch.setattr(daemon_module.subprocess, "Popen", _fake_popen)
    with pytest.raises(CheckUnavailableError, match="simulated self-test failure"):
        _spawn_daemon(tmp_path)


def test_spawn_daemon_raises_os_error_on_a_reported_bind_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bind failure is an operational problem unrelated to ty itself, so it must surface as OSError
    # (fallback-worthy) rather than CheckUnavailableError, unlike a self-test failure above.
    def _fake_popen(_args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        return _REAL_POPEN([sys.executable, "-c", "print('BIND_FAILED: simulated bind failure', flush=True)"], **kwargs)

    monkeypatch.setattr(daemon_module.subprocess, "Popen", _fake_popen)
    with pytest.raises(OSError, match="simulated bind failure"):
        _spawn_daemon(tmp_path)


def test_spawn_daemon_raises_os_error_when_nothing_is_printed_within_the_wait_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "_SPAWN_WAIT_TIMEOUT_SECONDS", 0.2)
    spawned: list[subprocess.Popen[bytes]] = []

    def _fake_popen(_args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        process = _REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(daemon_module.subprocess, "Popen", _fake_popen)
    with pytest.raises(OSError, match="did not start within"):
        _spawn_daemon(tmp_path)

    # A daemon that never reached READY must not be left running -- it could still finish starting and
    # bind the socket later, after this caller already fell back locally. _spawn_daemon() already waits
    # for it to exit before raising, so no timeout is needed here.
    (process,) = spawned
    assert process.poll() is not None


def test_kill_spawn_attempt_escalates_to_sigkill_when_sigterm_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_module, "_KILL_WAIT_TIMEOUT_SECONDS", 0.2)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('ready', flush=True)\n"
                "time.sleep(30)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdout is not None
    process.stdout.readline()  # wait until SIGTERM is actually ignored before trying to kill it

    daemon_module._kill_spawn_attempt(process)

    assert process.poll() is not None


def test_spawn_daemon_raises_os_error_on_an_unexpected_startup_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_popen(_args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        return _REAL_POPEN([sys.executable, "-c", "print('nonsense', flush=True)"], **kwargs)

    monkeypatch.setattr(daemon_module.subprocess, "Popen", _fake_popen)
    with pytest.raises(OSError, match="unexpected startup line"):
        _spawn_daemon(tmp_path)


def test_self_test_passes_with_the_real_installed_ty() -> None:
    # Deliberately unmocked: a failed self-test raises CheckUnavailableError, failing this test on its own.
    daemon_module._self_test()


def test_serve_binds_prints_ready_and_exits_on_idle_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # _detach_stdio() would otherwise sever this test process's own stdio;
    # _IDLE_TIMEOUT_SECONDS is shortened so _serve() returns quickly with no client ever connecting.
    monkeypatch.setattr(daemon_module, "_detach_stdio", lambda: None)
    monkeypatch.setattr(daemon_module, "_IDLE_TIMEOUT_SECONDS", 0.2)

    daemon_module._serve(tmp_path)

    assert "READY" in capsys.readouterr().out
    assert not daemon_module._socket_path(tmp_path).exists()
    assert not daemon_module._pid_path(tmp_path).exists()


def test_serve_prints_failed_when_ty_version_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(daemon_module, "_ty_version", Mock(side_effect=OSError("simulated: ty not on PATH")))

    daemon_module._serve(tmp_path)

    assert "FAILED: simulated: ty not on PATH" in capsys.readouterr().out
    assert not daemon_module._socket_path(tmp_path).exists()


def test_serve_prints_failed_when_the_session_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(daemon_module, "_self_test", lambda: None)
    monkeypatch.setattr(daemon_module, "TySession", Mock(side_effect=CheckUnavailableError("simulated: ty not found")))

    daemon_module._serve(tmp_path)

    assert f"FAILED: could not start a ty session for {tmp_path}: simulated: ty not found" in capsys.readouterr().out
    assert not daemon_module._socket_path(tmp_path).exists()


def test_serve_prints_failed_when_the_self_test_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        daemon_module,
        "_self_test",
        lambda: (_ for _ in ()).throw(CheckUnavailableError("simulated self-test failure")),
    )

    daemon_module._serve(tmp_path)

    assert "FAILED: self-test failed: simulated self-test failure" in capsys.readouterr().out
    assert not daemon_module._socket_path(tmp_path).exists()


def test_serve_prints_failed_when_bind_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A read-only parent directory makes bind() itself raise PermissionError while creating the socket's
    # special file -- real self-test/session construction still runs for real here, since only this later
    # bind step is meant to fail. _serve() now writes its own pidfile before ever attempting to bind (see
    # ADR-0041), so that file is pre-created here first: overwriting an *existing* file's own content
    # doesn't need directory write permission the way creating the new socket special file does. The
    # directory being read-only also means this same pidfile can't actually be removed afterward --
    # covered separately (without restricting directory permissions) below.
    monkeypatch.setattr(daemon_module, "_IDLE_TIMEOUT_SECONDS", 0.2)
    socket_path = daemon_module._socket_path(tmp_path)
    pid_path = daemon_module._pid_path(tmp_path)
    socket_path.parent.mkdir(parents=True)
    pid_path.touch()

    with restricted_permissions(socket_path.parent, 0o555, restore=0o755):
        daemon_module._serve(tmp_path)

    assert "BIND_FAILED: could not bind" in capsys.readouterr().out


def test_serve_removes_its_own_pidfile_when_bind_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A directory already occupying the socket's own path makes bind() fail without needing to restrict
    # the parent directory's own permissions -- unlike the read-only-directory case above, this leaves the
    # pidfile this daemon just wrote for itself actually removable, so its own bind-failure cleanup can be
    # verified directly: the failed bind must not leave this daemon's own pid claiming a dead socket.
    monkeypatch.setattr(daemon_module, "_IDLE_TIMEOUT_SECONDS", 0.2)
    socket_path = daemon_module._socket_path(tmp_path)
    pid_path = daemon_module._pid_path(tmp_path)
    socket_path.mkdir(parents=True)

    daemon_module._serve(tmp_path)

    assert "BIND_FAILED: could not bind" in capsys.readouterr().out
    assert not pid_path.exists()


class TestRealDaemonEndToEnd:
    """Spawns a real daemon subprocess against the real, installed `ty` binary -- see ADR-0041.

    Isolated to `tmp_path` (never the real repo's own `.cache/`), and explicitly torn down via
    `shutdown_if_running()` rather than left running until its own idle timeout.
    """

    __slots__ = ()

    @pytest.fixture(autouse=True)
    def _isolate_and_teardown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.chdir(tmp_path)
        original_session = session_module._session
        original_probe_failed = session_module._daemon_probe_failed
        session_module._session = None
        session_module._daemon_probe_failed = False
        yield
        # A test that established its own session (session_module._session) never closes it, since that's
        # this feature's whole point -- closing it here first matters because _accept_loop's own graceful
        # shutdown (ADR-0041) joins every still-in-flight connection's own worker thread before the daemon's
        # process actually ends: a leftover, still-open-but-idle connection's own thread wouldn't return on
        # its own until its full per-connection timeout, so shutdown_if_running() below would otherwise wait
        # out that same budget instead of the daemon actually exiting promptly.
        leftover_session = session_module.peek_session()
        if leftover_session is not None:
            leftover_session.close()
            session_module._session = None
        # shutdown_if_running() is best-effort by design (never raises) --
        # a couple of unconditional retries absorb a rare, transient
        # connection hiccup rather than leaving this test's own daemon to
        # idle out on its own.
        for _ in range(3):
            shutdown_if_running(tmp_path)
            time.sleep(0.2)
        session_module._session = original_session
        session_module._daemon_probe_failed = original_probe_failed

    def test_connect_spawns_once_and_reuses_the_running_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn_calls: list[Path] = []
        original_spawn = daemon_module._spawn_daemon

        def _counting_spawn(root: Path) -> None:
            spawn_calls.append(root)
            original_spawn(root)

        monkeypatch.setattr(daemon_module, "_spawn_daemon", _counting_spawn)
        # Each connection closes before the next "hook invocation" connects, matching real usage -- one
        # connection per hook invocation, never held open across separate commits.
        first = connect(tmp_path)
        first.close()
        connect(tmp_path).close()

        assert len(spawn_calls) == 1

    def test_daemon_flags_a_caller_after_a_later_run_only_touches_the_callee(self, tmp_path: Path) -> None:
        # The exact gap issue #109 exists to close: a redundant conversion at a call site whose
        # parameter's own type lives in a different file, where the commit that widens that
        # parameter's type never passes the call site itself to this hook.
        callee = tmp_path / "callee.py"
        caller = tmp_path / "caller.py"
        callee.write_text("def takes(x: int) -> None:\n    print(x)\n")
        caller.write_text("from callee import takes\n\n\ndef use(y: str) -> None:\n    takes(int(y))\n")

        first_check = RedundantTypeConversionCheck(level=ConfidenceLevel.PERMISSIVE)
        caller_source = caller.read_text()
        # "Commit 1": caller.py is examined while callee.py still requires int -- int(y) is necessary.
        assert first_check.check(caller, ast.parse(caller_source), caller_source) == []

        # "Commit 2": only callee.py changes (widened to also accept str) and only *it* is examined --
        # caller.py is never passed to this run at all.
        callee.write_text("def takes(x: int | str) -> None:\n    print(x)\n")
        second_check = RedundantTypeConversionCheck(level=ConfidenceLevel.PERMISSIVE)
        callee_source = callee.read_text()
        assert second_check.check(callee, ast.parse(callee_source), callee_source) == []

        third_check = RedundantTypeConversionCheck(level=ConfidenceLevel.PERMISSIVE)
        extra_files = third_check.drain_cross_file_candidates([callee])
        assert caller.resolve() in extra_files

        redundant_now = third_check.check(caller, ast.parse(caller_source), caller_source)
        assert len(redundant_now) == 1
        assert redundant_now[0].line == 5

    def test_shutdown_if_running_actually_ends_the_daemon_process(self, tmp_path: Path) -> None:
        connect(tmp_path).close()

        shutdown_if_running(tmp_path)

        assert try_connect_existing(tmp_path) is None
