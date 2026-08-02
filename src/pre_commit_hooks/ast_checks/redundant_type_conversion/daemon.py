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
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from pre_commit_hooks._filelock import locked, locking_is_available
from pre_commit_hooks._lsp import LSPError, read_framed_message, write_framed_message
from pre_commit_hooks.ast_checks._base import CheckUnavailableError

from .session import PersistentSession, Redundancy, TySession, _run_self_test_in_temporary_directory

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import IO

logger = logging.getLogger("ast_checks")

_SOCKET_RELATIVE_PATH = Path(".cache/pre_commit_hooks/tri006-daemon.sock")
_PID_RELATIVE_PATH = Path(".cache/pre_commit_hooks/tri006-daemon.pid")
# pre-commit/prek run this hook's own file-batches across several parallel worker processes (up to CPU
# count), each independently trying to connect at roughly the same time -- a backlog of 1 made connect()
# fail immediately with EAGAIN once even two of them overlapped, indistinguishable from "no daemon" to the
# caller.
_LISTEN_BACKLOG = 64
_SPAWN_LOCK_TIMEOUT_SECONDS = 15.0
_SPAWN_LOCK_POLL_INTERVAL_SECONDS = 0.05
_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS = 15.0
_BUSY_DAEMON_RETRY_INTERVAL_SECONDS = 0.5
_SPAWN_WAIT_TIMEOUT_SECONDS = 30.0  # covers ty server's own cold start plus the self-test
_KILL_WAIT_TIMEOUT_SECONDS = 5.0
# How often _accept_loop's own accept() wakes up to notice a shutdown/version-mismatch signaled by one of
# its own worker threads, and to re-check whether _IDLE_TIMEOUT_SECONDS has genuinely elapsed -- both would
# otherwise only be noticed at the next new connection, up to _IDLE_TIMEOUT_SECONDS away.
_ACCEPT_POLL_INTERVAL_SECONDS = 0.1
# shutdown_if_running()'s own bound on waiting for the daemon's socket file to actually disappear after
# asking it to stop: the accept loop notices shutdown_requested (and _cleanup_if_still_owned() removes the
# socket) up to _ACCEPT_POLL_INTERVAL_SECONDS after the "shutting_down" reply this call already waited
# for, not synchronously with it now that connections are handled concurrently (see ADR-0041).
_SHUTDOWN_CONFIRM_TIMEOUT_SECONDS = 5.0
_SHUTDOWN_CONFIRM_POLL_INTERVAL_SECONDS = 0.02
_CONNECT_TIMEOUT_SECONDS = 5.0
_IDLE_TIMEOUT_SECONDS = 15 * 60
_CLIENT_REQUEST_TIMEOUT_SECONDS = 60.0
_STEADY_STATE_CALL_TIMEOUT_SECONDS = 60.0  # comfortably above the daemon's own ~20s internal ty-request budget
_PROTOCOL_VERSION = "4"

type RPCParameter = str | int | list[str] | list[Redundancy]


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


def repository_root(root: Path) -> Path:
    resolved_root = root.resolve()
    for parent in (resolved_root, *resolved_root.parents):
        if not (parent / ".git").exists():
            continue
        return parent
    return resolved_root


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
    return _socket_path(repository_root(root)).exists()


def _pid_path(root: Path) -> Path:
    return root / _PID_RELATIVE_PATH


def _read_recorded_pid(root: Path) -> int | None:
    try:
        return int(_pid_path(root).read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None


def _daemon_process_is_alive(root: Path) -> bool:
    """Whether the process recorded in this repository's own pidfile is still running -- distinct from
    `socket_exists_for()`/`_try_connect()`, which can't tell "no daemon" apart from "a live daemon that
    briefly couldn't accept and hand off this one connection within `_CONNECT_TIMEOUT_SECONDS`" (see
    ADR-0041; connections are handled concurrently, so this is rare, but not impossible under a large
    enough burst). `connect()` uses this to avoid spawning a second, competing daemon in that case -- both
    would otherwise unlink the same socket path out from under each other on their own startup/shutdown,
    leaving one or both unreachable.

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


def _daemon_identity() -> str:
    return f"{_ty_version()}|tri006-protocol-{_PROTOCOL_VERSION}"


# ---- client side ----


class RemoteTySession:
    """Talks to a `daemon` process over its Unix socket."""

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
            # ValueError (including json.JSONDecodeError, a subclass) means a malformed response body --
            # as much a lost, unusable connection as an OSError, so it's reported the same way rather than
            # escaping as a raw exception type this module's own callers (analysis.py) don't catch.
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
            # Mirrors TySession.finalize()'s own "never raises" contract
            # (this runs from decide_candidates()'s finally block) -- a
            # lost daemon connection here was already reported by an
            # earlier call in the same candidate loop.
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
    """A session backed by a persistent per-repository daemon, spawning one if none is currently reachable.

    Raises:
        CheckUnavailableError: a freshly spawned daemon's own `ty` failed its compatibility self-test --
            not fallback-worthy, since a local session would fail identically.
        OSError: the daemon couldn't be reached or spawned for an operational reason (no socket support,
            spawn timeout, ...) -- the caller should fall back to a private, non-persistent `TySession`.
    """
    root = repository_root(root)
    socket_path = _socket_path(root)
    daemon_identity = _daemon_identity()

    sock, already_confirmed_departing = _try_connect_or_departing(socket_path, daemon_identity)
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
        sock, daemon_is_departing = _try_connect_or_departing(socket_path, daemon_identity)
        if sock is not None:
            return RemoteTySession(sock)
        # A departing daemon can vanish (socket and pidfile both) between the pre-lock attempt above and
        # this one, at which point this attempt alone can no longer observe the mismatch that already
        # confirmed it -- once seen, never unseen, so it must not be treated as merely busy either way.
        daemon_is_departing = daemon_is_departing or already_confirmed_departing

        if not daemon_is_departing and _daemon_process_is_alive(root):
            # A live daemon exists but couldn't accept and hand off this one connection within
            # _CONNECT_TIMEOUT_SECONDS -- connections are handled concurrently (see ADR-0041), so this is
            # rare, but a large enough simultaneous burst can still exceed it. Spawning a second daemon
            # here would unlink its socket out from under it (and it could later do the same to the new
            # one on its own exit), leaving one or both unreachable. Waiting for it to catch up instead of
            # racing to replace it avoids that entirely.
            sock, daemon_is_departing = _wait_for_busy_daemon(socket_path, daemon_identity)
            if sock is not None:
                return RemoteTySession(sock)
            if not daemon_is_departing:
                msg = f"ty daemon for {root} is running but stayed busy for over {_BUSY_DAEMON_RETRY_TIMEOUT_SECONDS}s"
                raise OSError(msg)
            # else: the busy daemon turned out to be departing partway through the wait -- fall through
            # and spawn its replacement below instead of reporting a misleading "stayed busy" failure.

        _spawn_daemon(root)

        sock, _departing = _try_connect_or_departing(socket_path, daemon_identity)
        if sock is None:
            msg = f"ty daemon for {root} did not become reachable at {socket_path}"
            raise OSError(msg)
        return RemoteTySession(sock)


def _try_connect_or_departing(socket_path: Path, daemon_identity: str) -> tuple[socket.socket | None, bool]:
    """`_try_connect`, but converts its `_VersionMismatchError` into a plain `(None, True)` result instead of
    propagating it -- a daemon that just explicitly rejected the handshake is already shutting itself down,
    not merely busy, and callers (`connect()`, `_wait_for_busy_daemon()`, `try_connect_existing()`) must not
    mistake the two: `_daemon_process_is_alive()` would still see its still-exiting PID and wait out the full
    busy-daemon retry budget for an answer that was never coming, delaying every hook invocation until that
    daemon happens to have fully exited (see ADR-0041) instead of spawning a fresh one immediately.
    """
    try:
        return _try_connect(socket_path, daemon_identity), False
    except _VersionMismatchError:
        return None, True


def _wait_for_busy_daemon(socket_path: Path, daemon_identity: str) -> tuple[socket.socket | None, bool]:
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
        sock, daemon_is_departing = _try_connect_or_departing(socket_path, daemon_identity)
        if sock is not None:
            return sock, False
        if daemon_is_departing:
            return None, True
    return None, False


def probe_existing(root: Path) -> ExistingDaemonProbe:
    """Non-spawning: connects only if a daemon is already running and reachable for `root`.

    Used by `session.record_direct_input_if_session_active()` for direct inputs -- never spawns one itself,
    and never raises, since recording a candidate-less file
    must not fail this check for an unrelated daemon-connectivity reason. Checks `socket_exists_for()` (a
    plain `Path.exists()`) before ever resolving `ty --version`: a repository that has never had a daemon
    must not pay a real subprocess spawn on every single candidate-less file just to learn that, again.

    Waits out the same busy-daemon budget `connect()` does (`_wait_for_busy_daemon()`) for a daemon
    confirmed alive but too busy to answer, rather than treating it as unreachable: `record_direct_input_
    if_session_active()` caches whatever this returns for the rest of the run (session.py's own `_session`
    singleton), so giving up here on the first overlapping hook invocation would silently drop a direct-input
    recording for the entire run -- exactly the gap ADR-0041 exists to close.
    """
    root = repository_root(root)
    if not _socket_path(root).exists():
        return ExistingDaemonProbe(None)
    try:
        daemon_identity = _daemon_identity()
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
        # A crashed daemon's own socket, confirmed by its pidfile's own process being gone -- not a
        # departing-but-still-alive one (that daemon's own belated cleanup, ADR-0041, already handles
        # its own files). Left alone, this repeatedly widened get_prefilter_pattern()'s own "every
        # file" fallback (ADR-0041) indefinitely, in a repository where no future candidate-containing
        # commit ever runs connect() to notice and replace it.
        _cleanup_confirmed_dead_daemon(root)
    return ExistingDaemonProbe(None)


def try_connect_existing(root: Path) -> RemoteTySession | None:
    return probe_existing(root).session


def shutdown_if_running(root: Path) -> None:
    """Best-effort explicit teardown: asks a daemon already running for `root` to stop, if one is
    reachable, and waits (bounded) for its own socket file to actually disappear before returning.

    Never raises. Not part of this check's own normal operation (a daemon otherwise stops on its own, via
    the idle timeout or a version mismatch) -- this is the deliberate "teardown" ADR-0041 calls for, used
    directly by tests that spawn a real daemon and need it gone before the test ends, rather than waiting
    out its idle timeout.

    The "shutting_down" reply this waits for below only confirms the daemon's own accept loop has been
    told to stop, not that it already has: connections are handled concurrently (ADR-0041), so the thread
    that answered this request signals the accept loop rather than exiting it directly, and that loop
    notices asynchronously, up to `_ACCEPT_POLL_INTERVAL_SECONDS` later. Waiting here for
    `_cleanup_if_still_owned()`'s own socket removal -- run just before the daemon's process actually ends
    -- is what gives a caller an accurate "it's gone" rather than one that's merely about to be.
    """
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
        wfile = sock.makefile("wb")
        try:
            write_framed_message(wfile, {"op": "handshake", "ty_version": daemon_identity})
        finally:
            wfile.close()
        response = read_framed_message(rfile)
    except OSError, LSPError, ValueError:
        # LSPError (a truncated/malformed frame -- a daemon that died mid-handshake, or a stale socket
        # answering with garbage) and ValueError (including json.JSONDecodeError, a subclass) are as
        # unusable a connection as an OSError -- genuinely ambiguous with "busy" the same way, not a
        # confirmed rejection, so this must not escape uncaught and be recorded as a rule failure instead
        # of falling back to a local session the way every other operational connection failure here does.
        logger.debug("Connecting to the ty daemon at %s failed", socket_path, exc_info=True)
        sock.close()
        return None
    if response is not None and "error" in response:
        sock.close()
        raise _VersionMismatchError
    if response is None:
        sock.close()
        return None
    # Bounds every later call too, not just this handshake: a daemon that's genuinely wedged (not merely
    # slow on one ty request, which its own internal timeouts already resolve into a normal error response)
    # would otherwise hang this client forever, with nothing to ever fall back to a local session.
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


def _self_test(session: TySession, _root: Path) -> None:
    _run_self_test_in_temporary_directory(session, _root)


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
        # A malformed request (a missing or wrong-typed field) -- client and daemon are always the same,
        # handshake-matched code, so this should never happen in practice, but it must not escape as a raw
        # exception either way: that would propagate past _accept_loop's own except clause and end this
        # whole daemon's process over a single bad request, discarding every file it had been tracking.
        return {"error": f"malformed request for op {op!r}: {error!r}"}
    return {"error": f"unknown op: {op!r}"}


def _handle_connection(
    conn: socket.socket, session: PersistentSession, daemon_identity: str, session_lock: threading.Lock
) -> None:
    # Closed explicitly (not left for GC) so a write that failed mid-flight -- e.g. the broken-pipe case
    # _serve_connection's own except clause already handles -- doesn't leave buffered data behind for a
    # later finalizer to retry flushing on its own, which Python can only report as an unraisable warning
    # by then.
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
    """Runs on its own thread, one per accepted connection (see `_accept_loop`): bounds this one
    connection's own read/write against `_CLIENT_REQUEST_TIMEOUT_SECONDS` so a peer that connects and
    stalls mid-request -- never sending a complete message -- leaks neither this thread nor its socket
    forever, without blocking any other, concurrently accepted connection the way a stall used to when
    everything ran on the single accept-loop thread.
    """
    conn.settimeout(_CLIENT_REQUEST_TIMEOUT_SECONDS)
    try:
        with conn:
            _handle_connection(conn, session, daemon_identity, session_lock)
    except _VersionMismatchError, _ShutdownRequestedError:
        # Ends this whole daemon's process (see their own docstrings), not just this one connection:
        # signals the accept loop via shutdown_requested rather than returning directly, since this runs
        # on its own thread now, not the accept loop's own.
        shutdown_requested.set()
    except TimeoutError:
        logger.debug("ty daemon dropped a client connection that stalled mid-request")
    except LSPError, OSError, ValueError:
        # A truncated/malformed frame (LSPError, ValueError -- the latter covers a malformed JSON
        # body), a reset connection, or a broken pipe replying to a client that already disconnected
        # (OSError) -- an ordinary client-side failure, not this daemon's own. Dropping just this one
        # connection must not cost every other tracked file's own cross-file state (ADR-0041) the way an
        # uncaught exception escaping this whole daemon's process would.
        logger.debug("ty daemon dropped a client connection due to a connection-level error", exc_info=True)


def _accept_loop(sock: socket.socket, session: PersistentSession, daemon_identity: str) -> None:
    """Accepts connections concurrently, one worker thread each (`_serve_connection`), rather than serving
    them one at a time: pre-commit/prek run this hook's own file-batches across several parallel worker
    processes, each opening its own connection, so serving them serially here would force work that used
    to be parallel (each worker's own file batch) through one connection at a time instead (see ADR-0041).
    Only the actual calls into `session` are still serialized (`session_lock`, held in `_handle_connection`),
    matching `_lsp.LSPClient`'s own not-thread-safe-for-concurrent-request()/notify() contract.

    `sock`'s own accept() timeout is kept short (`_ACCEPT_POLL_INTERVAL_SECONDS`, or `_IDLE_TIMEOUT_SECONDS`
    itself if that's shorter) so this loop notices a shutdown/version-mismatch signaled by one of its own
    worker threads promptly, rather than only at the next new connection -- up to the real, much longer
    `_IDLE_TIMEOUT_SECONDS` away. Idle time is tracked across these short polls via a wall-clock deadline,
    reset on every accepted connection, so genuinely idle-timing-out still takes the full
    `_IDLE_TIMEOUT_SECONDS`, not just one short poll's worth.
    """
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
                # Idle timeout elapsed with no client and nothing in flight -- exit; the next client
                # respawns a fresh daemon.
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

    # A worker signaled shutdown_requested (version mismatch or an explicit shutdown request) -- let every
    # already-accepted, already-handshake-matched connection finish its own in-flight work before this
    # daemon's own process actually ends, rather than severing them mid-request.
    for worker in workers:
        worker.join()


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
        daemon_identity = _daemon_identity()  # pytriage: TR5
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
    sock.listen(_LISTEN_BACKLOG)

    print("READY", flush=True)
    _detach_stdio()

    try:
        _accept_loop(sock, session, daemon_identity)
    finally:
        session.close()
        sock.close()
        _cleanup_if_still_owned(root)
