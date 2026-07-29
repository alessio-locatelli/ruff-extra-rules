"""Shared, session-wide test fixtures.

See `docs/adr/0041-persistent-ty-daemon-for-cross-file-reanalysis.md`: a test exercising
`redundant-type-conversion`'s real, unmocked session against this repo's own working directory can spawn a
real, detached daemon process rooted at this repo -- unlike the plain, per-invocation `ty server` process it
replaced, a daemon deliberately outlives the process that spawned it. This is a defensive backstop, not a
substitute for isolating any individual test's own `Path.cwd()` (e.g. via `monkeypatch.chdir`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks.redundant_type_conversion import daemon as tri006_daemon

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _no_leftover_tri006_daemon_in_this_repo() -> Iterator[None]:
    yield
    tri006_daemon.shutdown_if_running(_REPO_ROOT)
