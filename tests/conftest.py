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

from pre_commit_hooks.ast_checks import _config
from pre_commit_hooks.ast_checks.redundant_type_conversion import daemon as tri006_daemon

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_from_this_repositorys_own_configuration(request: pytest.FixtureRequest) -> Iterator[None]:
    """This repository configures itself in its own `pyproject.toml`,
    `fix = true` included (ADR-0045). Tests run with the working directory
    at the repository root, so without this every `main()` call under test
    would discover that table and start rewriting files the test never
    asked it to. Equivalent to passing `--isolated`.

    A test that exercises discovery itself opts out with
    `@pytest.mark.uses_project_config`, having pointed the working
    directory at its own temporary project first.

    Deliberately not the shared `monkeypatch` fixture: requesting it here
    would set it up before every module-level autouse fixture in the suite
    and so tear it down after them, inverting an ordering other modules
    already depend on (see `test_main.py`'s own SIGTERM-handler fixture).
    """
    if "uses_project_config" in request.keywords:
        yield
        return
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(_config, "discover", lambda _start: None)
        yield


@pytest.fixture(scope="session", autouse=True)
def _no_leftover_tri006_daemon_in_this_repo() -> Iterator[None]:
    yield
    tri006_daemon.shutdown_if_running(_REPO_ROOT)
