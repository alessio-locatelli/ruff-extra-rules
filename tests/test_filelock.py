from __future__ import annotations

import fcntl
import time
from typing import TYPE_CHECKING

import pytest

import pre_commit_hooks._filelock as filelock_module
from pre_commit_hooks._filelock import locked, locking_is_available

if TYPE_CHECKING:
    from pathlib import Path


def test_locking_is_available_reflects_whether_fcntl_is_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert locking_is_available() is True

    monkeypatch.setattr(filelock_module, "fcntl", None)
    assert locking_is_available() is False


def test_locked_allows_sequential_acquisition(tmp_path: Path) -> None:
    lock_path = tmp_path / "some.lock"

    with locked(lock_path, timeout_seconds=1.0, poll_interval_seconds=0.01):
        pass
    with locked(lock_path, timeout_seconds=1.0, poll_interval_seconds=0.01):
        pass


def test_locked_raises_timeout_error_when_already_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "some.lock"

    with lock_path.open("a", encoding="utf-8") as blocker_fp:
        fcntl.flock(blocker_fp, fcntl.LOCK_EX)

        start = time.monotonic()
        with (
            pytest.raises(TimeoutError, match="Timed out"),
            locked(lock_path, timeout_seconds=0.2, poll_interval_seconds=0.01),
        ):
            pass  # pragma: no cover -- locked() raises during __enter__, so this body is never reached
        elapsed = time.monotonic() - start

    assert elapsed < 2.0


def test_locked_releases_lock_on_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "some.lock"

    with locked(lock_path, timeout_seconds=1.0, poll_interval_seconds=0.01):
        pass

    # A fresh acquisition attempt must succeed immediately -- the previous
    # `with` block's own exit must have actually released the flock, not
    # just returned without unlocking.
    with lock_path.open("a", encoding="utf-8") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fp, fcntl.LOCK_UN)


def test_locked_releases_lock_even_when_body_raises(tmp_path: Path) -> None:
    lock_path = tmp_path / "some.lock"

    with pytest.raises(ValueError, match="boom"), locked(lock_path, timeout_seconds=1.0, poll_interval_seconds=0.01):
        raise ValueError("boom")

    with lock_path.open("a", encoding="utf-8") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fp, fcntl.LOCK_UN)
