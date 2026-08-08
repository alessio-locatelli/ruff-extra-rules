from __future__ import annotations

import shutil
import subprocess
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_benchmark.fixture import BenchmarkFixture

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DIRECTORY = _PROJECT_ROOT / ".cache" / "pre_commit_hooks"
_FIXTURES_DIRECTORY = _PROJECT_ROOT / "benchmarks" / "fixtures"

ExecutionPath = Literal["direct", "prek"]
CacheState = Literal["cold", "warm"]


def _clear_benchmark_cache() -> None:
    try:
        shutil.rmtree(_CACHE_DIRECTORY)
    except FileNotFoundError:
        return
    except OSError as error:
        message = f"Could not remove benchmark cache {_CACHE_DIRECTORY}: {error}"
        raise OSError(message) from error


@pytest.fixture(scope="module", autouse=True)
def _reset_benchmark_cache() -> None:
    _clear_benchmark_cache()


def _command(execution_path: ExecutionPath, fixture_path: Path) -> list[str]:
    if execution_path == "direct":
        return [
            "uv",
            "run",
            "python",
            "-m",
            "pre_commit_hooks.ast_checks",
            "--ignore=redundant-type-conversion",
            str(fixture_path),
        ]
    return ["prek", "run", "local-ruff-extra-rules-default", "--files", str(fixture_path)]


def _run(command: list[str]) -> None:
    subprocess.run(command, cwd=_PROJECT_ROOT, check=True, capture_output=True, text=True)


def _cold_setup() -> None:
    _clear_benchmark_cache()


def _warm_setup(command: list[str]) -> None:
    _cold_setup()
    _run(command)


@pytest.mark.parametrize("fixture_name", ["small", "typical", "large"])
@pytest.mark.parametrize("execution_path", ["direct", "prek"])
@pytest.mark.parametrize("cache_state", ["cold", "warm"])
def test_default_hook_benchmark(
    benchmark: BenchmarkFixture,
    fixture_name: str,
    execution_path: ExecutionPath,
    cache_state: CacheState,
) -> None:
    command = _command(execution_path, _FIXTURES_DIRECTORY / f"{fixture_name}.py")
    benchmark.name = f"{execution_path}-{cache_state}-{fixture_name}"
    setup: Callable[[], None] = _cold_setup if cache_state == "cold" else partial(_warm_setup, command)
    benchmark.pedantic(_run, args=(command,), setup=setup, rounds=5, iterations=1)
