from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

try:
    import fcntl
except ImportError:  # pragma: win32 cover
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = ["locked", "locking_is_available"]


def locking_is_available() -> bool:
    return fcntl is not None


@contextlib.contextmanager
def locked(lock_path: Path, *, timeout_seconds: float, poll_interval_seconds: float) -> Iterator[None]:
    """Hold an exclusive advisory lock on `lock_path` for the duration of the `with` block.

    Polls with a non-blocking lock attempt instead of a single blocking
    `LOCK_EX`, so a peer that's still holding the lock past `timeout_seconds`
    raises `TimeoutError` rather than hanging this process indefinitely. A
    crashed process releases its flock automatically when its file
    descriptor closes (the OS does this even on SIGKILL), so this is only
    ever reached by a peer that's still genuinely running.
    """
    assert fcntl is not None  # callers must check their own platform availability first
    with lock_path.open("a", encoding="utf-8") as lock_fp:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    msg = f"Timed out after {timeout_seconds}s waiting for lock on {lock_path}"
                    raise TimeoutError(msg) from None
                time.sleep(poll_interval_seconds)
        try:
            yield
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
