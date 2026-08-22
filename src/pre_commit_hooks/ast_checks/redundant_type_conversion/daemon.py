# See ADR-0041.
from __future__ import annotations

import contextlib
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from pre_commit_hooks._filelock import locked, locking_is_available
from pre_commit_hooks._lsp import LSPError, read_framed_message, write_framed_message
from pre_commit_hooks.ast_checks._base import CheckUnavailableError

from .session import PersistentSession, Redundancy, TySession, _cache_context, _run_self_test_in_temporary_directory

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import IO

logger = logging.getLogger("ast_checks")

_SOCKET_RELATIVE_PATH = Path(".cache/pre_commit_hooks/tri006-daemon.sock")
_PID_RELATIVE_PATH = Path(".cache/pre_commit_hooks/tri006-daemon.pid")
_LISTEN_BACKLOG = 64
_SPAWN_LOCK_TIMEOUT_SECONDS = 15.0
_SPAWN_LOCK_POLL_INTERVAL_SECONDS = 0.05
_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS = 15.0
_BUSY_DAEMON_RETRY_INTERVAL_SECONDS = 0.5
_SPAWN_WAIT_TIMEOUT_SECONDS = 30.0
_KILL_WAIT_TIMEOUT_SECONDS = 5.0
_ACCEPT_POLL_INTERVAL_SECONDS = 0.1
_SHUTDOWN_CONFIRM_TIMEOUT_SECONDS = 5.0
_SHUTDOWN_CONFIRM_POLL_INTERVAL_SECONDS = 0.02
_CONNECT_TIMEOUT_SECONDS = 5.0
_IDLE_TIMEOUT_SECONDS = 15 * 60
_CLIENT_REQUEST_TIMEOUT_SECONDS = 60.0
_STEADY_STATE_CALL_TIMEOUT_SECONDS = 60.0
_PROTOCOL_VERSION = "4"

type RPCParameter = str | int | list[str] | list[Redundancy]


class _VersionMismatchError(Exception):
    pass


class _ShutdownRequestedError(Exception):
    pass


def _socket_path(root: Path) -> Path:
    return root / _SOCKET_RELATIVE_PATH


def repository_root(root: Path) -> Path:
    resolved_root = root.resolve()
    for parent in (resolved_root, *resolved_root.parents):
        if not (parent / ".git").exists():
            continue
        return parent
    return resolved_root


def socket_exists_for(root: Path) -> bool:
    return _socket_path(repository_root(root)).exists()


def _pid_path(root: Path) -> Path:
    return root / _PID_RELATIVE_PATH


def _read_recorded_pid(root: Path) -> int | None:
    try:
        return int(_pid_path(root).read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None


def _daemon_process_is_alive(root: Path) -> bool:
    pid = _read_recorded_pid(root)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_if_still_owned(root: Path) -> None:
    if _read_recorded_pid(root) not in (os.getpid(), None):
        return
    _cleanup_confirmed_dead_daemon(root)


def _cleanup_confirmed_dead_daemon(root: Path) -> None:
    with contextlib.suppress(OSError):
        _socket_path(root).unlink()
    with contextlib.suppress(OSError):
        _pid_path(root).unlink()


def _ty_version() -> str:
    try:
        completed_process = subprocess.run(["ty", "--version"], capture_output=True, text=True, check=True)  # noqa: S607
    except (OSError, subprocess.CalledProcessError) as error:
        msg = f"could not determine the local `ty --version`: {error!r}"
        raise OSError(msg) from error
    return completed_process.stdout.strip()


def _daemon_identity(root: Path) -> str:
    return f"{_ty_version()}|tri006-protocol-{_PROTOCOL_VERSION}|context-{_cache_context(root).hex()}"


class RemoteTySession:
    __slots__ = ("_rfile", "_sock", "_wfile")

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._rfile = sock.makefile("rb")
        self._wfile = sock.makefile("wb")

    def _call(self, op: str, **params: RPCParameter) -> list[Any] | str | None:
        try:
            write_framed_message(self._wfile, {"op": op, **params})
            response = read_framed_message(self._rfile)
        except (OSError, ValueError) as error:
            msg = f"ty daemon connection failed during {op!r}: {error!r}"
            raise LSPError(msg) from error
        if response is None:
            msg = f"ty daemon closed the connection during {op!r}"
            raise LSPError(msg)
        if "error" in response:
            msg = f"ty daemon call {op!r} failed: {response['error']}"
            raise LSPError(msg)
        return response.get("result")

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[Any, ...]]:
        raw_diagnostics = self._call("open_or_update", filepath=_canonical_rpc_path(filepath), content=content)
        assert isinstance(raw_diagnostics, list)
        return frozenset(tuple(item) for item in raw_diagnostics)

    def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None:
        hover_text = self._call("hover", filepath=_canonical_rpc_path(filepath), line0=line0, char_utf16=char_utf16)
        assert not isinstance(hover_text, list)
        return hover_text

    @contextlib.contextmanager
    def analysis_transaction(self) -> Iterator[None]:
        self._call("begin_analysis")
        try:
            yield
        finally:
            with contextlib.suppress(LSPError):
                self._call("end_analysis")

    def finalize(self, filepath: Path, source: str) -> None:
        with contextlib.suppress(LSPError):
            self._call("finalize", filepath=_canonical_rpc_path(filepath), source=source)

    def cached_redundancies(self, filepath: Path, source: str, cache_key: str) -> list[Redundancy] | None:
        cached = self._call(
            "cached_redundancies", filepath=_canonical_rpc_path(filepath), source=source, cache_key=cache_key
        )
        if cached is None:
            return None
        assert isinstance(cached, list)
        return [tuple(item) for item in cached]

    def cache_redundancies(self, filepath: Path, source: str, cache_key: str, redundancies: list[Redundancy]) -> None:
        with contextlib.suppress(LSPError):
            self._call(
                "cache_redundancies",
                filepath=_canonical_rpc_path(filepath),
                source=source,
                cache_key=cache_key,
                redundancies=redundancies,
            )

    def record_direct_input(self, filepath: Path, source: str) -> None:
        with contextlib.suppress(LSPError):
            self._call("record_direct_input", filepath=_canonical_rpc_path(filepath), source=source)

    def reconcile_direct_inputs(self) -> list[Path]:
        raw_paths = self._call("reconcile_direct_inputs")
        assert isinstance(raw_paths, list)
        return [Path(path) for path in raw_paths]

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._rfile.close()
        with contextlib.suppress(OSError):
            self._wfile.close()
        with contextlib.suppress(OSError):
            self._sock.close()


def _canonical_rpc_path(filepath: Path) -> str:
    return str(filepath.resolve())


class ExistingDaemonProbe(NamedTuple):
    session: RemoteTySession | None
    terminal_failure: bool = False


def connect(root: Path) -> RemoteTySession:
    root = repository_root(root)
    socket_path = _socket_path(root)
    daemon_identity = _daemon_identity(root)

    sock, already_confirmed_departing = _try_connect_or_departing(socket_path, daemon_identity)
    if sock is not None:
        return RemoteTySession(sock)

    if not locking_is_available():
        msg = f"file locking is unavailable on this platform (os.name={os.name!r}); cannot safely spawn a ty daemon"
        raise OSError(msg)

    lock_path = socket_path.with_suffix(".spawn.lock")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with locked(
        lock_path, timeout_seconds=_SPAWN_LOCK_TIMEOUT_SECONDS, poll_interval_seconds=_SPAWN_LOCK_POLL_INTERVAL_SECONDS
    ):
        sock, daemon_is_departing = _try_connect_or_departing(socket_path, daemon_identity)
        if sock is not None:
            return RemoteTySession(sock)
        daemon_is_departing = daemon_is_departing or already_confirmed_departing

        if not daemon_is_departing and _daemon_process_is_alive(root):
            sock, daemon_is_departing = _wait_for_busy_daemon(socket_path, daemon_identity)
            if sock is not None:
                return RemoteTySession(sock)
            if not daemon_is_departing:
                msg = f"ty daemon for {root} is running but stayed busy for over {_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS}s"
                raise OSError(msg)

        _spawn_daemon(root)

        sock, _departing = _try_connect_or_departing(socket_path, daemon_identity)
        if sock is None:
            msg = f"ty daemon for {root} did not become reachable at {socket_path}"
            raise OSError(msg)
        return RemoteTySession(sock)


def _try_connect_or_departing(socket_path: Path, daemon_identity: str) -> tuple[socket.socket | None, bool]:
    try:
        return _try_connect(socket_path, daemon_identity), False
    except _VersionMismatchError:
        return None, True


def _wait_for_busy_daemon(socket_path: Path, daemon_identity: str) -> tuple[socket.socket | None, bool]:
    deadline = time.monotonic() + _BUSY_DAEMON_RETRY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_BUSY_DAEMON_RETRY_INTERVAL_SECONDS)
        sock, daemon_is_departing = _try_connect_or_departing(socket_path, daemon_identity)
        if sock is not None:
            return sock, False
        if daemon_is_departing:
            return None, True
    return None, False


def probe_existing(root: Path) -> ExistingDaemonProbe:
    root = repository_root(root)
    if not _socket_path(root).exists():
        return ExistingDaemonProbe(None)
    try:
        daemon_identity = _daemon_identity(root)
    except OSError:
        return ExistingDaemonProbe(None, terminal_failure=True)
    socket_path = _socket_path(root)
    sock, departing = _try_connect_or_departing(socket_path, daemon_identity)
    if sock is not None:
        return ExistingDaemonProbe(RemoteTySession(sock))
    if not departing and _daemon_process_is_alive(root):
        sock, _departing = _wait_for_busy_daemon(socket_path, daemon_identity)
        return ExistingDaemonProbe(RemoteTySession(sock) if sock is not None else None)
    if not departing:
        _cleanup_confirmed_dead_daemon(root)
    return ExistingDaemonProbe(None)


def try_connect_existing(root: Path) -> RemoteTySession | None:
    return probe_existing(root).session


def shutdown_if_running(root: Path) -> None:
    root = repository_root(root)
    session = try_connect_existing(root)
    if session is None:
        return
    with contextlib.suppress(LSPError):
        session._call("shutdown")  # noqa: SLF001 -- same module, not a real encapsulation boundary
    session.close()

    deadline = time.monotonic() + _SHUTDOWN_CONFIRM_TIMEOUT_SECONDS
    while socket_exists_for(root) and time.monotonic() < deadline:
        time.sleep(_SHUTDOWN_CONFIRM_POLL_INTERVAL_SECONDS)


def _try_connect(socket_path: Path, daemon_identity: str) -> socket.socket | None:
    if not socket_path.exists():
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_CONNECT_TIMEOUT_SECONDS)
        sock.connect(str(socket_path))
        rfile = sock.makefile("rb")
        wfile = sock.makefile("wb")
        try:
            write_framed_message(wfile, {"op": "handshake", "ty_version": daemon_identity})
        finally:
            wfile.close()
        response = read_framed_message(rfile)
    except OSError, LSPError, ValueError:
        logger.debug("Connecting to the ty daemon at %s failed", socket_path, exc_info=True)
        sock.close()
        return None
    if response is not None and "error" in response:
        sock.close()
        raise _VersionMismatchError
    if response is None:
        sock.close()
        return None
    sock.settimeout(_STEADY_STATE_CALL_TIMEOUT_SECONDS)
    return sock


def _spawn_daemon(root: Path) -> None:
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "pre_commit_hooks.ast_checks.redundant_type_conversion", root],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        _await_ready(process, root)
    except BaseException:
        _kill_spawn_attempt(process)
        raise


def _await_ready(process: subprocess.Popen[bytes], root: Path) -> None:
    stdout = process.stdout
    assert stdout is not None
    line = _readline_with_timeout(stdout, _SPAWN_WAIT_TIMEOUT_SECONDS)
    if line is None:
        msg = f"ty daemon for {root} did not start within {_SPAWN_WAIT_TIMEOUT_SECONDS}s"
        raise OSError(msg)

    text = line.decode("utf-8", errors="replace").strip()
    if text.startswith("BIND_FAILED:"):
        msg = text.removeprefix("BIND_FAILED:").strip()
        raise OSError(msg)
    if text.startswith("FAILED:"):
        raise CheckUnavailableError(text.removeprefix("FAILED:").strip())
    if text != "READY":
        msg = f"ty daemon for {root} sent an unexpected startup line: {text!r}"
        raise OSError(msg)


def _kill_spawn_attempt(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=_KILL_WAIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            process.wait(timeout=_KILL_WAIT_TIMEOUT_SECONDS)


def _readline_with_timeout(stream: IO[bytes], timeout: float) -> bytes | None:
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        return None
    return stream.readline()


def _self_test(session: TySession, _root: Path) -> None:
    _run_self_test_in_temporary_directory(session, _root)


def _detach_stdio() -> None:
    # no cover: start
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull_fd, sys.stdin.fileno())
        os.dup2(devnull_fd, sys.stdout.fileno())
        os.dup2(devnull_fd, sys.stderr.fileno())
    finally:
        os.close(devnull_fd)
    # no cover: stop


def _dispatch(message: dict[str, Any], session: PersistentSession) -> dict[str, Any]:
    op = message.get("op")
    try:
        if op == "open_or_update":
            diagnostics = session.open_or_update(Path(message["filepath"]), message["content"])
            return {"result": list(diagnostics)}  # pytriage: TR6 -- frozenset isn't JSON-serializable
        if op == "hover":
            hover_text = session.hover(Path(message["filepath"]), message["line0"], message["char_utf16"])
            return {"result": hover_text}
        if op == "finalize":
            session.finalize(Path(message["filepath"]), message["source"])
            return {"result": None}
        if op == "cached_redundancies":
            cached = session.cached_redundancies(Path(message["filepath"]), message["source"], message["cache_key"])
            return {"result": cached}
        if op == "cache_redundancies":
            session.cache_redundancies(
                Path(message["filepath"]), message["source"], message["cache_key"], message["redundancies"]
            )
            return {"result": None}
        if op == "record_direct_input":
            session.record_direct_input(Path(message["filepath"]), message["source"])
            return {"result": None}
        if op == "reconcile_direct_inputs":
            drained = session.reconcile_direct_inputs()
            return {"result": [str(path) for path in drained]}  # pytriage: TR6 -- Path isn't JSON-serializable
    except LSPError as error:
        return {"error": str(error)}  # pytriage: TR6 -- LSPError isn't JSON-serializable
    except (KeyError, TypeError) as error:
        return {"error": f"malformed request for op {op!r}: {error!r}"}
    return {"error": f"unknown op: {op!r}"}


def _handle_connection(
    conn: socket.socket, session: PersistentSession, daemon_identity: str, session_lock: threading.Lock
) -> None:
    transaction_active = False
    try:
        with conn.makefile("rb") as rfile, conn.makefile("wb") as wfile:
            handshake = read_framed_message(rfile)
            if handshake is None:
                return
            if handshake.get("op") != "handshake" or handshake.get("ty_version") != daemon_identity:
                write_framed_message(wfile, {"error": "version_mismatch"})
                raise _VersionMismatchError
            write_framed_message(wfile, {"result": "ok"})

            while True:
                message = read_framed_message(rfile)
                if message is None:
                    return
                if message.get("op") == "shutdown":
                    write_framed_message(wfile, {"result": "shutting_down"})
                    raise _ShutdownRequestedError
                if message.get("op") == "begin_analysis":
                    if transaction_active:
                        write_framed_message(wfile, {"error": "analysis transaction already active"})
                        continue
                    session_lock.acquire()
                    transaction_active = True
                    write_framed_message(wfile, {"result": None})
                    continue
                if message.get("op") == "end_analysis" and transaction_active:
                    session_lock.release()
                    transaction_active = False
                    write_framed_message(wfile, {"result": None})
                    continue
                if transaction_active:
                    response = _dispatch(message, session)
                else:
                    with session_lock:
                        response = _dispatch(message, session)
                write_framed_message(wfile, response)
    finally:
        if transaction_active:
            session_lock.release()


def _serve_connection(
    conn: socket.socket,
    session: PersistentSession,
    daemon_identity: str,
    session_lock: threading.Lock,
    shutdown_requested: threading.Event,
) -> None:
    conn.settimeout(_CLIENT_REQUEST_TIMEOUT_SECONDS)
    try:
        with conn:
            _handle_connection(conn, session, daemon_identity, session_lock)
    except _VersionMismatchError, _ShutdownRequestedError:
        shutdown_requested.set()
    except TimeoutError:
        logger.debug("ty daemon dropped a client connection that stalled mid-request")
    except LSPError, OSError, ValueError:
        logger.debug("ty daemon dropped a client connection due to a connection-level error", exc_info=True)


def _accept_loop(sock: socket.socket, session: PersistentSession, daemon_identity: str) -> None:
    session_lock = threading.Lock()
    shutdown_requested = threading.Event()
    workers: list[threading.Thread] = []
    sock.settimeout(min(_ACCEPT_POLL_INTERVAL_SECONDS, _IDLE_TIMEOUT_SECONDS))
    idle_deadline = time.monotonic() + _IDLE_TIMEOUT_SECONDS

    while not shutdown_requested.is_set():
        try:
            conn, _peer = sock.accept()
        except TimeoutError:
            workers = [worker for worker in workers if worker.is_alive()]
            if not workers and time.monotonic() >= idle_deadline:
                return
            continue
        idle_deadline = time.monotonic() + _IDLE_TIMEOUT_SECONDS
        workers = [worker for worker in workers if worker.is_alive()]
        worker = threading.Thread(
            target=_serve_connection,
            args=(conn, session, daemon_identity, session_lock, shutdown_requested),
            daemon=True,
        )
        worker.start()
        workers.append(worker)

    for worker in workers:
        worker.join()


def _serve(root: Path) -> None:
    socket_path = _socket_path(root)
    pid_path = _pid_path(root)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        socket_path.unlink()

    try:
        daemon_identity = _daemon_identity(root)
    except OSError as error:
        print(f"FAILED: {error}", flush=True)
        return

    try:
        session = TySession(root=root, keep_open=True)
    except (OSError, CheckUnavailableError) as error:
        print(f"FAILED: could not start a ty session for {root}: {error}", flush=True)
        return

    try:
        _self_test(session, root)
    except (OSError, CheckUnavailableError) as error:
        session.close()
        print(f"FAILED: self-test failed: {error}", flush=True)
        return

    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(socket_path))
    except OSError as error:
        session.close()
        with contextlib.suppress(OSError):
            pid_path.unlink()
        print(f"BIND_FAILED: could not bind {socket_path}: {error!r}", flush=True)
        return
    sock.listen(_LISTEN_BACKLOG)

    print("READY", flush=True)
    _detach_stdio()

    try:
        _accept_loop(sock, session, daemon_identity)
    finally:
        session.close()
        sock.close()
        _cleanup_if_still_owned(root)
