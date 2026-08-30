from __future__ import annotations

import io
import sys
import textwrap
import time
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks._lsp import LSPClient, LSPError, LSPTimeoutError, byte_col_to_utf16_col, read_framed_message

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

_FAKE_SERVER_SCRIPT = textwrap.dedent(
    r"""
    import json
    import sys
    import time


    def read_message():
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            chunk = sys.stdin.buffer.read(1)
            if not chunk:
                return None
            header += chunk
        length = None
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        body = sys.stdin.buffer.read(length)
        return json.loads(body)


    def write_message(payload):
        body = json.dumps(payload).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        sys.stdout.buffer.flush()


    while True:
        message = read_message()
        if message is None:
            break
        method = message.get("method")
        if method == "echo":
            write_message({"jsonrpc": "2.0", "id": message["id"], "result": message["params"]})
        elif method == "boom":
            write_message(
                {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "simulated failure"}}
            )
        elif method == "never_respond":
            pass
        elif method == "delayed_echo":
            time.sleep(0.3)
            write_message({"jsonrpc": "2.0", "id": message["id"], "result": message["params"]})
        elif method == "shutdown":
            write_message({"jsonrpc": "2.0", "id": message["id"], "result": None})
        elif method == "hanging_shutdown":
            pass
        elif method == "spin_forever":
            while True:
                time.sleep(1)
        elif method == "send_garbage":
            sys.stdout.buffer.write(b"Not-A-Valid-LSP-Header\r\n\r\n")
            sys.stdout.buffer.flush()
        elif method == "spam_stderr_then_respond":
            sys.stderr.write("x" * 200_000)
            sys.stderr.flush()
            write_message({"jsonrpc": "2.0", "id": message["id"], "result": message["params"]})
        elif method == "die_immediately":
            break
        elif method == "send_notification":
            write_message({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": message["params"]})
            write_message({"jsonrpc": "2.0", "id": message["id"], "result": None})
        elif method == "send_server_request":
            write_message({"jsonrpc": "2.0", "id": 999, "method": "client/registerCapability", "params": {}})
            write_message({"jsonrpc": "2.0", "id": message["id"], "result": None})
        elif method == "exit":
            break
    """
)


def _spawn_fake_server(cwd: Path, *, on_notification: Callable[[str, dict[str, Any]], None] | None = None) -> LSPClient:
    return LSPClient([sys.executable, "-c", _FAKE_SERVER_SCRIPT], cwd=cwd, on_notification=on_notification)


def test_request_returns_result(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client:
        response = client.request("echo", {"hello": "world"})
        assert response == {"hello": "world"}


def test_a_large_stderr_write_does_not_deadlock_the_server(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client:
        response = client.request("spam_stderr_then_respond", {"hello": "world"}, timeout=5.0)
        assert response == {"hello": "world"}


def test_request_raises_lsp_error_on_error_response(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client, pytest.raises(LSPError, match="simulated failure"):
        client.request("boom", {})


def test_request_times_out_when_server_never_responds(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client, pytest.raises(LSPTimeoutError, match="never_respond"):
        client.request("never_respond", {}, timeout=0.2)


def test_pending_request_is_cleaned_up_after_timeout(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client:
        with pytest.raises(LSPTimeoutError):
            client.request("never_respond", {}, timeout=0.2)
        assert client._pending == {}
        assert client.request("echo", {"ok": True}) == {"ok": True}


def test_notify_does_not_wait_for_a_response(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client:
        client.notify("exit", {})
        client._process.wait(timeout=5)
        assert client._process.returncode == 0


def test_close_performs_clean_shutdown_handshake(tmp_path: Path) -> None:
    client = _spawn_fake_server(tmp_path)
    client.close()
    assert client._process.returncode == 0
    assert client._close_called is True


def test_close_kills_the_process_when_shutdown_handshake_hangs(tmp_path: Path) -> None:
    client = _spawn_fake_server(tmp_path)
    client._next_id += 1
    msg_id = client._next_id
    client._write({"jsonrpc": "2.0", "id": msg_id, "method": "hanging_shutdown", "params": {}})

    client.close(timeout=0.3)

    assert client._close_called is True
    assert client._process.poll() is not None


def test_close_kills_the_process_when_it_never_exits_on_its_own(tmp_path: Path) -> None:
    client = _spawn_fake_server(tmp_path)
    client._next_id += 1
    msg_id = client._next_id
    client._write({"jsonrpc": "2.0", "id": msg_id, "method": "spin_forever", "params": {}})
    time.sleep(0.1)

    client.close(timeout=0.3)

    assert client._close_called is True
    assert client._process.poll() is not None


def test_close_is_idempotent(tmp_path: Path) -> None:
    client = _spawn_fake_server(tmp_path)
    client.close()
    client.close()
    assert client._close_called is True


def test_close_still_cleans_up_after_the_server_already_exited_on_its_own(tmp_path: Path) -> None:
    client = _spawn_fake_server(tmp_path)
    client.notify("exit", {})
    client._process.wait(timeout=5)
    time.sleep(0.2)

    client.close()

    assert client._process.stdin is not None
    assert client._process.stdin.closed is True


def test_request_after_server_exits_raises_instead_of_hanging(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client:
        client.notify("exit", {})
        client._process.wait(timeout=5)

        with pytest.raises(LSPError):
            client.request("echo", {}, timeout=2.0)


def test_request_raises_lsp_error_when_server_exits_without_responding(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client, pytest.raises(LSPError, match="LSP connection closed"):
        client.request("die_immediately", timeout=5.0)


def test_reader_loop_marks_connection_lost_on_a_malformed_frame(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client:
        client.notify("send_garbage")
        client._reader.join(timeout=5)
        assert client._reader.is_alive() is False
        assert client._connection_lost is True


def test_on_notification_receives_a_server_notification(tmp_path: Path) -> None:
    received: list[tuple[str, dict[str, Any]]] = []
    with _spawn_fake_server(
        tmp_path, on_notification=lambda method, params: received.append((method, params))
    ) as client:
        client.request("send_notification", {"uri": "file:///a.py", "items": []})

    assert received == [("textDocument/publishDiagnostics", {"uri": "file:///a.py", "items": []})]


def test_missing_on_notification_silently_drops_a_server_notification(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client:
        assert client.request("send_notification", {"uri": "file:///a.py", "items": []}) is None


def test_on_notification_is_never_called_for_a_server_to_client_request(tmp_path: Path) -> None:
    received: list[tuple[str, dict[str, Any]]] = []
    with _spawn_fake_server(
        tmp_path, on_notification=lambda method, params: received.append((method, params))
    ) as client:
        client.request("send_server_request", {})

    assert received == []


def test_on_notification_callback_raising_does_not_kill_the_reader_thread(tmp_path: Path) -> None:
    def _boom(_method: str, _params: dict[str, Any]) -> None:
        raise ValueError("boom")

    with _spawn_fake_server(tmp_path, on_notification=_boom) as client:
        client.request("send_notification", {"uri": "file:///a.py", "items": []})
        assert client.request("echo", {"ok": True}) == {"ok": True}


def test_late_response_after_a_timeout_is_silently_dropped(tmp_path: Path) -> None:
    with _spawn_fake_server(tmp_path) as client:
        with pytest.raises(LSPTimeoutError):
            client.request("delayed_echo", {"value": 1}, timeout=0.05)

        time.sleep(0.5)

        assert client.request("echo", {"ok": True}) == {"ok": True}


def test_read_message_returns_none_on_clean_eof() -> None:
    assert read_framed_message(io.BytesIO(b"")) is None


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (b"Content-Length: 10\r\n", "mid-header"),
        (b"Content-Length: 10\r\n\r\n{}", "mid-message"),
        (b"X-Custom: 1\r\n\r\n", "Content-Length"),
    ],
    ids=["truncated-header", "truncated-body", "missing-content-length-header"],
)
def test_read_message_raises_on_a_malformed_frame(raw: bytes, match: str) -> None:
    with pytest.raises(LSPError, match=match):
        read_framed_message(io.BytesIO(raw))


def test_read_message_ignores_unrelated_headers() -> None:
    body = b'{"jsonrpc": "2.0", "id": 1, "result": null}'
    raw = f"Content-Type: application/vscode-jsonrpc\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    assert read_framed_message(io.BytesIO(raw)) == {"jsonrpc": "2.0", "id": 1, "result": None}


@pytest.mark.parametrize(
    ("line", "byte_col", "expected"),
    [
        ("abc", 0, 0),
        ("abc", 3, 3),
        ("café", len(b"caf"), 3),
        ("café !", len("café".encode()), 4),
        ("\U0001f600x", len("\U0001f600".encode()), 2),
    ],
    ids=["ascii-start", "ascii-end", "bmp-accent-before", "bmp-accent-mid", "astral-surrogate-pair"],
)
def test_byte_col_to_utf16_col(line: str, byte_col: int, expected: int) -> None:
    assert byte_col_to_utf16_col(line, byte_col) == expected
