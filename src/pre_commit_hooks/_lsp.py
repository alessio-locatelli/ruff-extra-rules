"""Standard-library-only JSON-RPC/LSP client over stdio. See ADR-0035 and
`docs/audits/type-checker-selection-for-redundant-type-conversion.md`.
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
    from typing import IO, Self

__all__ = ["LSPClient", "LSPError", "LSPTimeoutError", "byte_col_to_utf16_col"]

logger = logging.getLogger("lsp")

DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0


class LSPError(Exception):
    """A JSON-RPC error response, or a lost connection (EOF, broken pipe, malformed frame)."""


class LSPTimeoutError(LSPError):
    """A request got no response within its own timeout."""


def byte_col_to_utf16_col(line: str, byte_col: int) -> int:
    """UTF-8 byte offset (`ast.col_offset`) to a UTF-16 code-unit offset (LSP's own wire encoding).

    Not the same as `ast_checks._base.byte_col_to_char_col`'s character
    offset -- a character outside the Basic Multilingual Plane is one
    `str` character but two UTF-16 code units.
    """
    prefix = line.encode("utf-8")[:byte_col].decode("utf-8")
    return len(prefix.encode("utf-16-le")) // 2


def _read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    """One `Content-Length`-framed JSON-RPC message, or `None` on a clean EOF before any header bytes arrive."""
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
    # Omits "params" entirely when None: ty server rejects an empty object
    # for a no-argument method like "shutdown" with a JSON-RPC parse error.
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        message["id"] = msg_id
    if params is not None:
        message["params"] = params
    return message


class LSPClient:
    """One child language-server process over Content-Length-framed JSON-RPC via stdio.

    A server-to-client request (carries both `id` and `method`) is
    silently left unanswered, same as an ordinary notification -- `ty
    server` has not been observed to block on one. Not thread-safe for
    concurrent `request()`/`notify()` calls.
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
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._next_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        # _close_called alone gates close()'s idempotency; _connection_lost (set by _read_loop) never does.
        self._connection_lost = False
        self._close_called = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _drain_stderr(self) -> None:
        # An unread stderr PIPE fills once the server writes enough to it,
        # then blocks the server's own next write, stalling its whole
        # request/response loop.
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
            failure = {"error": {"message": f"{error}" if error else "LSP connection closed"}}
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
        """Best-effort shutdown handshake, then stdin close, then wait/kill. Never raises."""
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
