"""A persistent, per-repository `ty server` daemon for TR6's cross-file
reanalysis -- see ADR-0041 and issue #123's own spike. Not a general-purpose
LSP proxy: this tunnels exactly the primitives `TySession` already exposes
over a Unix domain socket, so a short-lived hook invocation can share one
long-lived `ty server` process across separate, later commits.
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pre_commit_hooks._filelock import locked, locking_is_available
from pre_commit_hooks._lsp import LSPError, read_framed_message, write_framed_message
from pre_commit_hooks.ast_checks._base import CheckUnavailableError

from .session import PersistentSession, TySession, _run_self_test

if TYPE_CHECKING:
    from typing import IO

logger = logging.getLogger("ast_checks")

_SOCKET_RELATIVE_PATH = Path(".cache/pre_commit_hooks/tri006-daemon.sock")
_PID_RELATIVE_PATH = Path(".cache/pre_commit_hooks/tri006-daemon.pid")
_SPAWN_LOCK_TIMEOUT_SECONDS = 15.0
_SPAWN_LOCK_POLL_INTERVAL_SECONDS = 0.05
_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS = 15.0
_BUSY_DAEMON_RETRY_INTERVAL_SECONDS = 0.5
_SPAWN_WAIT_TIMEOUT_SECONDS = 30.0  # covers ty server's own cold start plus the self-test
_KILL_WAIT_TIMEOUT_SECONDS = 5.0
_CONNECT_TIMEOUT_SECONDS = 5.0
_IDLE_TIMEOUT_SECONDS = 15 * 60
_CLIENT_REQUEST_TIMEOUT_SECONDS = 60.0


class _VersionMismatchError(Exception):
    """Raised internally on both ends of a version-mismatched handshake: server-side (`_handle_connection`),
    it signals the accept loop to end this daemon's own process instead of continuing to serve stale
    results; client-side (`_try_connect`), it signals that the daemon just answered was already exiting for
    this same reason, so callers must not wait for it the way they would for a merely busy one.
    """


class _ShutdownRequestedError(Exception):
    """Raised internally when a client explicitly asks this daemon to stop (`shutdown_if_running()`) --
    signals the accept loop to end this daemon's own process, the same way a version mismatch does.
    """


def _socket_path(root: Path) -> Path:
    return root / _SOCKET_RELATIVE_PATH


def socket_exists_for(root: Path) -> bool:
    """Whether a daemon *might* already be running for `root`, checked with a plain `Path.exists()` -- no
    connection attempt.

    Used by `RedundantTypeConversionCheck.get_prefilter_pattern()` (ADR-0041) to decide whether this run's
    own prefilter needs to widen to "every file": once a daemon exists, any file can be a dependency of an
    already-tracked one, not just files with a redundant-conversion candidate of their own. A stale socket
    left by a crashed daemon makes this return `True` too -- momentarily widening the prefilter for no real
    benefit, but self-correcting the moment something spawns a fresh daemon and cleans the stale file up;
    never a correctness problem, only a possible one-run performance blip.
    """
    return _socket_path(root).exists()


def _pid_path(root: Path) -> Path:
    return root / _PID_RELATIVE_PATH


def _read_recorded_pid(root: Path) -> int | None:
    try:
        return int(_pid_path(root).read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None


def _daemon_process_is_alive(root: Path) -> bool:
    """Whether the process recorded in this repository's own pidfile is still running -- distinct from
    `socket_exists_for()`/`_try_connect()`, which can't tell "no daemon" apart from "a live daemon that's
    simply too busy serving another client to answer a handshake within `_CONNECT_TIMEOUT_SECONDS`" (this
    daemon serves one client at a time, see ADR-0041). `connect()` uses this to avoid spawning a second,
    competing daemon in that case -- both would otherwise unlink the same socket path out from under each
    other on their own startup/shutdown, leaving one or both unreachable.

    A PID no longer belonging to this daemon (reused by an unrelated process after a crash) is a real, if
    rare, inherent risk of any PID-based liveness check -- not specific to this one, and not defended
    against further here, matching this decision's own "coarse over fine-grained" bias elsewhere.
    """
    pid = _read_recorded_pid(root)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by another user -- conservatively assume alive
    return True


def _cleanup_if_still_owned(root: Path) -> None:
    """Removes this daemon's own socket and pidfile from `root` -- but only if the pidfile still names this
    exact process. A replacement daemon spawned right after this one shuts down for a version mismatch (see
    `connect()`/ADR-0041) can already be bound and serving by the time this runs: unlink() always acts on
    whatever currently occupies a path, not on whichever process created it, so an unconditional unlink here
    would delete the replacement's live socket/pidfile instead of this daemon's own already-defunct ones.
    """
    if _read_recorded_pid(root) not in (os.getpid(), None):
        return  # a newer daemon already replaced us here -- its files, not ours; leave them alone
    _cleanup_confirmed_dead_daemon(root)


def _cleanup_confirmed_dead_daemon(root: Path) -> None:
    """Removes a daemon's leftover socket and pidfile at `root` -- callers must have already confirmed no
    live process owns them first, since this runs from processes other than the daemon itself (unlike
    `_cleanup_if_still_owned()`, called by a daemon on its own exit) and so has no pidfile identity of its
    own to compare against.

    Best-effort: an `OSError` removing either file (already gone, or an unwritable directory) leaves this
    repository exactly where it started -- this cleanup is a bonus for a repository that has one and never
    something either caller depends on succeeding.
    """
    with contextlib.suppress(OSError):
        _socket_path(root).unlink()
    with contextlib.suppress(OSError):
        _pid_path(root).unlink()


def _ty_version() -> str:
    """The locally resolved `ty --version`, normalized to a plain `OSError` on any failure -- callers
    (`connect()`'s fallback-to-local path, `_serve()`'s own startup) don't need to distinguish "ty missing"
    from "ty present but exited non-zero"; either way there's no version to compare against.
    """
    try:
        completed_process = subprocess.run(["ty", "--version"], capture_output=True, text=True, check=True)  # noqa: S607
    except (OSError, subprocess.CalledProcessError) as error:
        msg = f"could not determine the local `ty --version`: {error!r}"
        raise OSError(msg) from error
    return completed_process.stdout.strip()


# ---- client side ----


class RemoteTySession:
    """Talks to a `daemon` process over its Unix socket, implementing the same surface `TySession` does for
    `analysis.decide_candidates()`, plus `drain_cross_file_candidates()` -- meaningful here specifically
    because the daemon's own session was constructed with `keep_open=True`.
    """

    __slots__ = ("_rfile", "_sock", "_wfile")

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._rfile = sock.makefile("rb")
        self._wfile = sock.makefile("wb")

    def _call(self, op: str, **params: Any) -> Any:  # noqa: ANN401 -- a daemon response's own "result" shape varies by op
        try:
            write_framed_message(self._wfile, {"op": op, **params})
            response = read_framed_message(self._rfile)
        except OSError as error:
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
        raw_diagnostics = self._call("open_or_update", filepath=str(filepath), content=content)  # pytriage: TR6
        return frozenset(tuple(item) for item in raw_diagnostics)

    def hover(self, filepath: Path, line0: int, char_utf16: int) -> str | None:
        return self._call("hover", filepath=str(filepath), line0=line0, char_utf16=char_utf16)  # pytriage: TR6

    def finalize(self, filepath: Path, source: str) -> None:
        with contextlib.suppress(LSPError):
            # Mirrors TySession.finalize()'s own "never raises" contract
            # (this runs from decide_candidates()'s finally block) -- a
            # lost daemon connection here was already reported by an
            # earlier call in the same candidate loop.
            self._call("finalize", filepath=str(filepath), source=source)  # pytriage: TR6

    def notify_changed_on_disk(self, filepath: Path, source: str) -> None:
        with contextlib.suppress(LSPError):
            # Mirrors TySession.notify_changed_on_disk()'s own "never
            # raises" contract -- called from the candidate-less fast path
            # (session.notify_disk_change_if_session_active()), which must
            # not fail a file's whole check over a daemon-connectivity hiccup.
            self._call("notify_changed_on_disk", filepath=str(filepath), source=source)  # pytriage: TR6

    def drain_cross_file_candidates(self, already_processed: list[Path]) -> list[Path]:
        raw_paths = self._call(
            "drain_cross_file_candidates",
            exclude=[str(path) for path in already_processed],  # pytriage: TR6
        )
        return [Path(path) for path in raw_paths]

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._rfile.close()
        with contextlib.suppress(OSError):
            self._wfile.close()
        with contextlib.suppress(OSError):
            self._sock.close()


def connect(root: Path) -> RemoteTySession:
    """A session backed by a persistent per-repository daemon, spawning one if none is currently reachable.

    Raises:
        CheckUnavailableError: a freshly spawned daemon's own `ty` failed its compatibility self-test --
            not fallback-worthy, since a local session would fail identically.
        OSError: the daemon couldn't be reached or spawned for an operational reason (no socket support,
            spawn timeout, ...) -- the caller should fall back to a private, non-persistent `TySession`.
    """
    socket_path = _socket_path(root)
    client_ty_version = _ty_version()

    sock, already_confirmed_departing = _try_connect_or_departing(socket_path, client_ty_version)
    if sock is not None:
        return RemoteTySession(sock)

    if not locking_is_available():
        # No fcntl on this platform (see docs/adr/0020): spawning is guarded by an exclusive lock this
        # process can't take, so there's no safe way to decide "should I spawn" here -- an OSError makes
        # the caller fall back to a private, non-persistent session instead, exactly like any other
        # operational reason this daemon can't be used.
        msg = f"file locking is unavailable on this platform (os.name={os.name!r}); cannot safely spawn a ty daemon"
        raise OSError(msg)

    lock_path = socket_path.with_suffix(".spawn.lock")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with locked(
        lock_path, timeout_seconds=_SPAWN_LOCK_TIMEOUT_SECONDS, poll_interval_seconds=_SPAWN_LOCK_POLL_INTERVAL_SECONDS
    ):
        # A peer may have already spawned one while this process waited for the lock.
        sock, daemon_is_departing = _try_connect_or_departing(socket_path, client_ty_version)
        if sock is not None:
            return RemoteTySession(sock)
        # A departing daemon can vanish (socket and pidfile both) between the pre-lock attempt above and
        # this one, at which point this attempt alone can no longer observe the mismatch that already
        # confirmed it -- once seen, never unseen, so it must not be treated as merely busy either way.
        daemon_is_departing = daemon_is_departing or already_confirmed_departing

        if not daemon_is_departing and _daemon_process_is_alive(root):
            # A live daemon exists but was too busy serving another client to answer the handshake
            # within _CONNECT_TIMEOUT_SECONDS -- it serves one client at a time (see ADR-0041).
            # Spawning a second daemon here would unlink its socket out from under it (and it could
            # later do the same to the new one on its own exit), leaving one or both unreachable.
            # Waiting for it to free up instead of racing to replace it avoids that entirely.
            sock, daemon_is_departing = _wait_for_busy_daemon(socket_path, client_ty_version)
            if sock is not None:
                return RemoteTySession(sock)
            if not daemon_is_departing:
                msg = f"ty daemon for {root} is running but stayed busy for over {_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS}s"
                raise OSError(msg)
            # else: the busy daemon turned out to be departing partway through the wait -- fall through
            # and spawn its replacement below instead of reporting a misleading "stayed busy" failure.

        _spawn_daemon(root)

        sock, _departing = _try_connect_or_departing(socket_path, client_ty_version)
        if sock is None:
            msg = f"ty daemon for {root} did not become reachable at {socket_path}"
            raise OSError(msg)
        return RemoteTySession(sock)


def _try_connect_or_departing(socket_path: Path, client_ty_version: str) -> tuple[socket.socket | None, bool]:
    """`_try_connect`, but converts its `_VersionMismatchError` into a plain `(None, True)` result instead of
    propagating it -- a daemon that just explicitly rejected the handshake is already shutting itself down,
    not merely busy, and callers (`connect()`, `_wait_for_busy_daemon()`, `try_connect_existing()`) must not
    mistake the two: `_daemon_process_is_alive()` would still see its still-exiting PID and wait out the full
    busy-daemon retry budget for an answer that was never coming, delaying every hook invocation until that
    daemon happens to have fully exited (see ADR-0041) instead of spawning a fresh one immediately.
    """
    try:
        return _try_connect(socket_path, client_ty_version), False
    except _VersionMismatchError:
        return None, True


def _wait_for_busy_daemon(socket_path: Path, client_ty_version: str) -> tuple[socket.socket | None, bool]:
    """Retries `_try_connect` with a short, fixed backoff, bounded by `_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS`,
    for a daemon already confirmed alive (`_daemon_process_is_alive`) but currently too busy to answer.

    Also reports back (the same `(sock, departing)` shape as `_try_connect_or_departing()`) if the daemon
    it was waiting for turns out to be departing rather than merely busy, bailing out early rather than
    waiting out the rest of the retry budget: `connect()` needs to know this happened, not just that no
    socket came back, or it would report a misleading "stayed busy" failure and skip spawning the
    replacement this daemon's own departure calls for.
    """
    deadline = time.monotonic() + _BUSY_DAEMON_RETRY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_BUSY_DAEMON_RETRY_INTERVAL_SECONDS)
        sock, daemon_is_departing = _try_connect_or_departing(socket_path, client_ty_version)
        if sock is not None:
            return sock, False
        if daemon_is_departing:
            return None, True
    return None, False


def try_connect_existing(root: Path) -> RemoteTySession | None:
    """Non-spawning: connects only if a daemon is already running and reachable for `root`.

    Used by `session.notify_disk_change_if_session_active()` for the cheap "tell it about a disk change"
    path -- never spawns one itself, and never raises, since a mere notification about a candidate-less file
    must not fail this check for an unrelated daemon-connectivity reason. Checks `socket_exists_for()` (a
    plain `Path.exists()`) before ever resolving `ty --version`: a repository that has never had a daemon
    must not pay a real subprocess spawn on every single candidate-less file just to learn that, again.

    Waits out the same busy-daemon budget `connect()` does (`_wait_for_busy_daemon()`) for a daemon
    confirmed alive but too busy to answer, rather than treating it as unreachable: `notify_disk_change_
    if_session_active()` caches whatever this returns for the rest of the run (session.py's own `_session`
    singleton), so giving up here on the first overlapping hook invocation would silently drop a disk-change
    notification for the entire run -- exactly the gap ADR-0041 exists to close.
    """
    if not socket_exists_for(root):
        return None
    try:
        client_ty_version = _ty_version()
    except OSError:
        return None
    socket_path = _socket_path(root)
    sock, departing = _try_connect_or_departing(socket_path, client_ty_version)
    if sock is not None:
        return RemoteTySession(sock)
    if not departing and _daemon_process_is_alive(root):
        sock, _departing = _wait_for_busy_daemon(socket_path, client_ty_version)
        return RemoteTySession(sock) if sock is not None else None
    if not departing:
        # A crashed daemon's own socket, confirmed by its pidfile's own process being gone -- not a
        # departing-but-still-alive one (that daemon's own belated cleanup, ADR-0041, already handles
        # its own files). Left alone, this repeatedly widened get_prefilter_pattern()'s own "every
        # file" fallback (ADR-0041) indefinitely, in a repository where no future candidate-containing
        # commit ever runs connect() to notice and replace it.
        _cleanup_confirmed_dead_daemon(root)
    return None


def shutdown_if_running(root: Path) -> None:
    """Best-effort explicit teardown: asks a daemon already running for `root` to stop, if one is reachable.

    Never raises. Not part of this check's own normal operation (a daemon otherwise stops on its own, via
    the idle timeout or a version mismatch) -- this is the deliberate "teardown" ADR-0041 calls for, used
    directly by tests that spawn a real daemon and need it gone before the test ends, rather than waiting
    out its idle timeout.
    """
    session = try_connect_existing(root)
    if session is None:
        return
    with contextlib.suppress(LSPError):
        session._call("shutdown")  # noqa: SLF001 -- same module, not a real encapsulation boundary
    session.close()


def _try_connect(socket_path: Path, client_ty_version: str) -> socket.socket | None:
    """Connects and performs the version handshake against a daemon already listening at `socket_path`.

    Returns `None` when nothing usable answered at all -- no socket, connection refused, a timed-out or
    dropped connection, or a bare EOF -- genuinely ambiguous with "busy," left for the caller's own
    liveness/busy-wait logic (`_daemon_process_is_alive()`, `_wait_for_busy_daemon()`) to resolve.

    Raises:
        _VersionMismatchError: the daemon explicitly rejected the handshake and is already shutting itself
            down as a result -- unambiguous, so callers must use `_try_connect_or_departing()` instead of
            calling this directly whenever that distinction matters (see its own docstring).
    """
    if not socket_path.exists():
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_CONNECT_TIMEOUT_SECONDS)
        sock.connect(str(socket_path))
        rfile = sock.makefile("rb")
        write_framed_message(sock.makefile("wb"), {"op": "handshake", "ty_version": client_ty_version})
        response = read_framed_message(rfile)
    except OSError:
        logger.debug("Connecting to the ty daemon at %s failed", socket_path, exc_info=True)
        sock.close()
        return None
    if response is not None and "error" in response:
        sock.close()
        raise _VersionMismatchError
    if response is None:
        sock.close()
        return None
    sock.settimeout(None)  # steady-state calls rely on TySession's own internal ty-request timeouts instead
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
        # A daemon that didn't make it to READY here must not be left running: it could still
        # finish initializing and bind the socket later, after this caller has already fallen back
        # to a local session, leaving an unowned background process (and possibly its own `ty
        # server` child, if it got as far as its own self-test) behind indefinitely.
        _kill_spawn_attempt(process)
        raise


def _await_ready(process: subprocess.Popen[bytes], root: Path) -> None:
    stdout = process.stdout
    assert stdout is not None  # constructed with stdout=PIPE above
    line = _readline_with_timeout(stdout, _SPAWN_WAIT_TIMEOUT_SECONDS)
    if line is None:
        msg = f"ty daemon for {root} did not start within {_SPAWN_WAIT_TIMEOUT_SECONDS}s"
        raise OSError(msg)

    text = line.decode("utf-8", errors="replace").strip()
    if text.startswith("BIND_FAILED:"):
        # An operational failure unrelated to ty itself (e.g. an unwritable cache directory, or a
        # socket path over the platform's AF_UNIX length limit) -- OSError, so the caller falls back
        # to a local session instead of disabling this check entirely.
        msg = text.removeprefix("BIND_FAILED:").strip()
        raise OSError(msg)
    if text.startswith("FAILED:"):
        raise CheckUnavailableError(text.removeprefix("FAILED:").strip())
    if text != "READY":
        msg = f"ty daemon for {root} sent an unexpected startup line: {text!r}"
        raise OSError(msg)
    # The daemon has already redirected its own stdio away from this pipe
    # and detached (start_new_session=True) -- nothing further to do with
    # `process` itself.


def _kill_spawn_attempt(process: subprocess.Popen[bytes]) -> None:
    """Best-effort: ends the whole process group of a spawn attempt that's failing or timing out.
    `start_new_session=True` made `process` its own group leader, so killing that group also reaches
    any `ty server` child it may have already spawned as part of its own self-test. Never raises.
    """
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


# ---- server side ----


def _self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="ruff-extra-rules-tri006-daemon-selftest-") as scratch_dir:
        scratch_root = Path(scratch_dir)
        self_test_session = TySession(root=scratch_root)
        try:
            _run_self_test(self_test_session, scratch_root)
        finally:
            self_test_session.close()


def _detach_stdio() -> None:
    """Severs this process's own stdio from whatever spawned it, so the spawning client's read of this
    process's stdout sees a clean EOF right after the one status line it already printed, and this daemon's
    own later `ty server` stderr output (drained by `TySession`'s own background thread) never risks blocking
    on a pipe nobody is reading from anymore.

    Exercised by the real, subprocess-spawning tests in `TestRealDaemonEndToEnd` (a successful spawn is only
    possible once this runs without crashing) rather than by line coverage: calling it in-process would sever
    the calling test process's own stdio for the rest of that process's life, which for the real test suite
    is the whole remaining pytest run.
    """
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
        if op == "notify_changed_on_disk":
            session.notify_changed_on_disk(Path(message["filepath"]), message["source"])
            return {"result": None}
        if op == "drain_cross_file_candidates":
            already_processed = [Path(path) for path in message["exclude"]]
            drained = session.drain_cross_file_candidates(already_processed)
            return {"result": [str(path) for path in drained]}  # pytriage: TR6 -- Path isn't JSON-serializable
    except LSPError as error:
        return {"error": str(error)}  # pytriage: TR6 -- LSPError isn't JSON-serializable
    return {"error": f"unknown op: {op!r}"}


def _handle_connection(conn: socket.socket, session: PersistentSession, ty_version: str) -> None:
    # Closed explicitly (not left for GC) so a write that failed mid-flight -- e.g. the broken-pipe case
    # _accept_loop's own except clause already handles -- doesn't leave buffered data behind for a later
    # finalizer to retry flushing on its own, which Python can only report as an unraisable warning by then.
    with conn.makefile("rb") as rfile, conn.makefile("wb") as wfile:
        handshake = read_framed_message(rfile)
        if handshake is None:
            return
        if handshake.get("op") != "handshake" or handshake.get("ty_version") != ty_version:
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
            write_framed_message(wfile, _dispatch(message, session))


def _accept_loop(sock: socket.socket, session: PersistentSession, ty_version: str) -> None:
    while True:
        try:
            conn, _peer = sock.accept()
        except TimeoutError:
            return  # idle timeout elapsed with no client -- exit; the next client respawns a fresh daemon
        # Bounds every read/write against this one connection: this daemon serves one client at a time
        # (see ADR-0041), so a peer that connects and then stalls mid-request -- never sending a complete
        # message -- would otherwise block here forever, never returning to accept() for the next, healthy
        # client. Distinct from `_IDLE_TIMEOUT_SECONDS` above, which only bounds waiting for a connection
        # to arrive at all, not a connection that already arrived and went silent.
        conn.settimeout(_CLIENT_REQUEST_TIMEOUT_SECONDS)
        try:
            with conn:
                _handle_connection(conn, session, ty_version)
        except _VersionMismatchError, _ShutdownRequestedError:
            return
        except TimeoutError:
            logger.debug("ty daemon dropped a client connection that stalled mid-request")
        except LSPError, OSError, ValueError:
            # A truncated/malformed frame (LSPError, ValueError -- the latter covers a malformed JSON
            # body), a reset connection, or a broken pipe replying to a client that already disconnected
            # (OSError) -- an ordinary client-side failure, not this daemon's own. Dropping just this one
            # connection and returning to accept() must not cost every other tracked file's own cross-file
            # state (ADR-0041) the way an uncaught exception escaping this whole loop would.
            logger.debug("ty daemon dropped a client connection due to a connection-level error", exc_info=True)


def _serve(root: Path) -> None:
    """Runs this daemon process to completion: self-test, bind, serve, exit.

    Startup protocol read by `_spawn_daemon()`/`_await_ready()`: exactly one line on stdout -- `READY`
    (the socket is now bound and accepting), `FAILED: <reason>` (ty itself is missing or failed its
    compatibility self-test -- not fallback-worthy, see `CheckUnavailableError`), or `BIND_FAILED:
    <reason>` (an operational failure unrelated to ty, e.g. binding the socket itself -- OSError,
    fallback-worthy, and unlike the other two this process has already run its self-test successfully
    by the time it can happen). `_detach_stdio()` severs this process's own stdio right after `READY`,
    so nothing further is ever written to that same pipe.
    """
    socket_path = _socket_path(root)
    pid_path = _pid_path(root)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        socket_path.unlink()  # a stale socket left behind by a crashed prior daemon

    try:
        ty_version = _ty_version()  # pytriage: TR5 -- captured once, up front, to compare against every later handshake
        _self_test()
        session = TySession(root=root, keep_open=True)
    except (OSError, CheckUnavailableError) as error:
        print(f"FAILED: {error}", flush=True)
        return

    # Claims this repository's daemon identity before ever touching the socket path, not after binding it:
    # a still-departing predecessor's own cleanup (_cleanup_if_still_owned) decides whether it still owns
    # the socket by reading this same pidfile, so writing it only after bind() would leave a window where
    # this process already owns the socket but the pidfile still names the predecessor -- exactly the
    # window in which its belated cleanup would delete this process's own, already-live socket.
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(socket_path))
    except OSError as error:
        session.close()
        with contextlib.suppress(OSError):
            pid_path.unlink()  # never bound -- don't leave this process's own pid claiming a dead socket
        print(f"BIND_FAILED: could not bind {socket_path}: {error!r}", flush=True)
        return
    sock.listen(1)
    sock.settimeout(_IDLE_TIMEOUT_SECONDS)

    print("READY", flush=True)
    _detach_stdio()

    try:
        _accept_loop(sock, session, ty_version)
    finally:
        session.close()
        sock.close()
        _cleanup_if_still_owned(root)
