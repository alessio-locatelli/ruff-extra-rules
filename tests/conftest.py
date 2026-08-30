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
