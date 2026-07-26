"""Minimal, standard-library-only JSON-RPC/LSP client over stdio.

Shared transport for driving an external language server (e.g. `ty server`)
as one long-lived child process across a whole hook invocation — the same
kind of cross-cutting, dependency-free infrastructure `_cache.py` and
`_prefilter.py` already are, just for a different external tool. See
`docs/audits/type-checker-selection-for-redundant-type-conversion.md` for
why a hand-rolled client, rather than a third-party access-layer library
(`lsp-client`, `multilspy`), is used: `docs/adding-a-check.md` requires
every check to stay standard-library only.

This module only speaks generic JSON-RPC/LSP wire protocol (framing,
request/response/notification plumbing). It has no `ty`-specific
knowledge — that lives in `ast_checks.redundant_type_conversion`.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import IO

__all__ = ["LSPClient", "LSPError", "LSPTimeoutError", "byte_col_to_utf16_col"]

logger = logging.getLogger("lsp")

DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0


class LSPError(Exception):
    """Raised when the server returns a JSON-RPC error response, or the
    connection to it is unexpectedly lost (EOF, broken pipe, malformed
    frame).
    """


class LSPTimeoutError(LSPError):
    """Raised when a request gets no response within its own timeout."""


def byte_col_to_utf16_col(line: str, byte_col: int) -> int:
    """Convert a UTF-8 byte offset within `line` (as `ast.col_offset`
    reports) to a UTF-16 code-unit offset — the position encoding LSP's
    wire format uses by default (`positionEncoding: "utf-16"`; confirmed in
    `ty server`'s own `initialize` response, since this client declares no
    `general.positionEncodings` capability to negotiate a different one).

    Distinct from `ast_checks._base.byte_col_to_char_col`: that converts to
    a Python `str` *character* offset, which only coincides with a UTF-16
    code-unit offset for text confined to the Basic Multilingual Plane — a
    character outside it (e.g. most emoji) is one Python `str` character
    but two UTF-16 code units (a surrogate pair). A conversion column that
    lands one UTF-16 unit short of where it should be can hover/edit the
    wrong sub-expression on such a line.
    """
    prefix = line.encode("utf-8")[:byte_col].decode("utf-8")
    return len(prefix.encode("utf-16-le")) // 2


def _read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    """Reads one `Content-Length`-framed JSON-RPC message from `stream`.

    Returns `None` on a clean EOF before any header bytes arrive (the
    server process exited). Header lines beyond `Content-Length` (e.g. an
    optional `Content-Type`) are read and ignored, matching the LSP spec's
    own base protocol.
    """
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        chunk = stream.read(1)
        if not chunk:
            if header:
                msg = f"LSP connection closed mid-header: {header!r}"
                raise LSPError(msg)
            return None
        header += chunk

    content_length: int | None = None
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
    if content_length is None:
        msg = f"LSP message missing Content-Length header: {header!r}"
        raise LSPError(msg)

    body = b""
    while len(body) < content_length:
        chunk = stream.read(content_length - len(body))
        if not chunk:
            msg = f"LSP connection closed mid-message (expected {content_length} bytes, got {len(body)})"
            raise LSPError(msg)
        body += chunk

    return json.loads(body)


def _message(*, method: str, params: dict[str, Any] | None, msg_id: int | None = None) -> dict[str, Any]:
    """Builds one JSON-RPC message, omitting the `params` key entirely
    when `params` is `None` rather than sending an empty object.

    Some methods' params are spec'd as taking no arguments at all (e.g.
    LSP's own `shutdown`) — `ty server` (confirmed empirically) rejects
    an empty object for one of these with a JSON-RPC parse error
    ("invalid type: map, expected unit"), so `params={}` is not
    equivalent to omitting the field for every server, even though both
    are valid JSON-RPC on the wire.
    """
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        message["id"] = msg_id
    if params is not None:
        message["params"] = params
    return message


class LSPClient:
    """Drives one child language-server process over Content-Length-framed
    JSON-RPC (LSP's own base protocol) via stdio.

    A background thread reads every incoming message and routes it either
    to the pending request it answers or to an internal notifications
    queue (drained only by `close()`'s own shutdown sequence — this client
    only ever needs to *send* notifications and *pull* responses/
    diagnostics, never react to a server-pushed notification). A
    server-to-client *request* (message carrying both `id` and `method`,
    e.g. `window/workDoneProgress/create`) is treated the same as a
    notification — left unanswered. `ty server` has not been observed to
    block waiting for a reply to one (see the research audit linked in
    this module's own docstring); full bidirectional request handling is
    unneeded complexity for driving a single, known server this way.

    Not thread-safe for concurrent callers issuing requests at the same
    time — this codebase's own concurrency model is process-based (prek's
    parallel hook execution spawns separate processes, not threads within
    one), so a single foreground caller plus one background reader thread
    is all this needs.
    """

    __slots__ = (
        "_close_called",
        "_connection_lost",
        "_next_id",
        "_pending",
        "_pending_lock",
        "_process",
        "_reader",
        "_stderr_reader",
    )

    def __init__(self, command: Sequence[str], *, cwd: Path) -> None:
        self._process = subprocess.Popen(  # noqa: S603
            list(command),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._next_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        # Set by _read_loop() once the connection is observably gone (EOF or
        # a protocol error) -- distinct from _close_called below, which
        # guards close()'s own idempotency. Conflating the two used to make
        # close() a silent no-op whenever the server had already exited on
        # its own (e.g. after an "exit" notification), skipping the stdin
        # close/wait/kill cleanup entirely and leaking the process's stdin
        # pipe until an unpredictable later GC pass.
        self._connection_lost = False
        self._close_called = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()

    def _drain_stderr(self) -> None:
        """Continuously reads and discards the server's stderr.

        Without this, verbose server logging or a repeated warning can fill
        the OS pipe buffer (64KiB is typical on Linux); once full, the
        server's own next write to stderr blocks, stalling its entire
        request/response loop and surfacing as a mysterious request timeout
        on this client's side rather than as the stderr backpressure that
        actually caused it.
        """
        stderr = self._process.stderr
        assert stderr is not None  # constructed with stderr=PIPE above
        for line in iter(stderr.readline, b""):
            logger.debug("ty server stderr: %s", line.decode("utf-8", errors="replace").rstrip())

    def _read_loop(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None  # constructed with stdout=PIPE above
        error: BaseException | None = None
        try:
            while True:
                message = _read_message(stdout)
                if message is None:
                    break
                msg_id = message.get("id")
                if msg_id is not None and ("result" in message or "error" in message):
                    with self._pending_lock:
                        box = self._pending.get(msg_id)
                    if box is not None:
                        box.put(message)
                # else: a notification, or a server-to-client request -- see
                # this class's own docstring for why both are dropped.
        except Exception as caught:
            error = caught
            logger.debug("LSP reader loop failed", exc_info=True)
        finally:
            self._connection_lost = True
            # Unblock every request still waiting on a response, rather than
            # leaving each to discover the connection is gone only once its
            # own timeout separately elapses.
            failure = {"error": {"message": str(error) if error else "LSP connection closed"}}
            with self._pending_lock:
                boxes = list(self._pending.values())
            for box in boxes:
                box.put(failure)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write(_message(method=method, params=params))

    # ANN401: a JSON-RPC "result" shape genuinely varies by method (hover vs. diagnostic pull, ...).
    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    ) -> Any:  # noqa: ANN401
        self._next_id += 1
        msg_id = self._next_id
        box: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[msg_id] = box
        try:
            self._write(_message(method=method, params=params, msg_id=msg_id))
            try:
                message = box.get(timeout=timeout)
            except queue.Empty:
                msg = f"LSP request {method!r} (id={msg_id}) timed out after {timeout}s"
                raise LSPTimeoutError(msg) from None
        finally:
            with self._pending_lock:
                self._pending.pop(msg_id, None)

        if "error" in message:
            msg = f"LSP request {method!r} (id={msg_id}) returned an error: {message['error']}"
            raise LSPError(msg)
        return message.get("result")

    def _write(self, payload: dict[str, Any]) -> None:
        stdin = self._process.stdin
        assert stdin is not None  # constructed with stdin=PIPE above
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            stdin.write(header)
            stdin.write(body)
            stdin.flush()
        except (BrokenPipeError, OSError) as error:
            msg = f"failed to write to LSP server process: {error!r}"
            raise LSPError(msg) from error

    def close(self, *, timeout: float = 2.0) -> None:
        """Best-effort clean shutdown: the LSP `shutdown`/`exit` handshake,
        then closing stdin, then waiting for the process to exit on its
        own before killing it. Never raises — this is cleanup, called from
        places (e.g. `atexit`) where a failure here has nothing useful left
        to do but be logged.
        """
        if self._close_called:
            return
        self._close_called = True
        try:
            self.request("shutdown", timeout=timeout)
            self.notify("exit")
        except LSPError:
            logger.debug("LSP shutdown handshake failed; killing the process instead", exc_info=True)

        stdin = self._process.stdin
        assert stdin is not None  # constructed with stdin=PIPE above
        try:
            stdin.close()
        except OSError:
            logger.debug("Failed to close LSP server stdin", exc_info=True)
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=timeout)
