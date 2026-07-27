"""Test fixture: lazy singleton accessors (should be skipped)."""

import threading


class Session:
    def __init__(self, root: str) -> None:
        self.root = root


_session: Session | None = None
_session_lock = threading.Lock()


def get_session() -> Session:
    """Process-wide Session singleton, created lazily on first use."""
    global _session
    with _session_lock:
        if _session is None:
            _session = Session(root=".")
        return _session


def get_session_guard_clause() -> Session:
    global _session
    with _session_lock:
        if _session is not None:
            return _session
        _session = Session(root=".")
        return _session
