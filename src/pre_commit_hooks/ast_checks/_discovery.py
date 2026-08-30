from __future__ import annotations

import contextlib
import logging
import os
import select
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from ._globs import anchored_pattern, glob_matches, relative_to_anchor

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("ast_checks")

_CURRENT_DIR = PurePosixPath()


class ExcludePattern(NamedTuple):
    pattern: str
    anchor: Path


def filter_excluded_files(filepaths: list[str], exclude_patterns: Sequence[ExcludePattern]) -> list[str]:
    if not exclude_patterns:
        return filepaths

    patterns_by_anchor: defaultdict[Path, list[str]] = defaultdict(list)
    for pattern, anchor in exclude_patterns:
        patterns_by_anchor[anchor].append(pattern)

    return [
        filepath
        for filepath in filepaths
        if not _is_excluded(PurePosixPath(os.path.abspath(filepath)), patterns_by_anchor)  # noqa: PTH100
    ]


def _is_excluded(absolute: PurePosixPath, patterns_by_anchor: dict[Path, list[str]]) -> bool:
    for anchor, patterns in patterns_by_anchor.items():
        relative = relative_to_anchor(absolute, anchor)
        if relative is not None and any(_matches(absolute, relative, pattern, anchor) for pattern in patterns):
            return True
    return False


def _matches(absolute: PurePosixPath, relative: PurePosixPath, pattern: str, anchor: Path) -> bool:
    if "/" not in pattern:
        return any(glob_matches(pattern, part) for part in relative.parts)
    anchored = anchored_pattern(pattern, anchor)
    directories = (PurePosixPath(anchor) / parent for parent in relative.parents if parent != _CURRENT_DIR)
    return glob_matches(anchored, str(absolute)) or any(
        glob_matches(anchored, str(directory)) for directory in directories
    )


def expand_directories(filenames: list[str]) -> list[str]:
    expanded: list[str] = []
    for name in filenames:
        path = Path(name)
        if path.is_dir():
            expanded.extend(_list_python_files_in_dir(path))
        else:
            expanded.append(name)
    return expanded


def _list_python_files_in_dir(directory: Path) -> list[str]:
    resolved_dir = directory.resolve()
    try:
        cmd: list[str | Path] = ["git", "-C", directory, "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        git_ls_files_result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, errors="surrogateescape", check=False, timeout=30
        )
        if git_ls_files_result.returncode == 0 and not git_ls_files_result.stderr:
            python_files = sorted(
                str(candidate)
                for f in git_ls_files_result.stdout.split("\0")
                if f.endswith(".py") and (candidate := resolved_dir / f).exists()
            )
            _warn_about_ignored_python_files(directory)
            return python_files
    except subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired:
        logger.debug("git ls-files failed", exc_info=True)

    return sorted(str(p) for p in resolved_dir.rglob("*.py"))


_MAX_REPORTED_IGNORED_PATHS = 20
_MAX_PENDING_IGNORED_STATUS_BYTES = 65_536
_GIT_STATUS_TIMEOUT_SECONDS = 5
_PROCESS_STOP_TIMEOUT_SECONDS = 1
_CAN_STREAM_IGNORED_STATUS = os.name == "posix"

_NON_SOURCE_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "develop-eggs",
        "dist",
        "downloads",
        "eggs",
        ".eggs",
        "lib",
        "lib64",
        "parts",
        "sdist",
        "var",
        "wheels",
        ".venv",
        "venv",
        "ENV",
        "env",
        "htmlcov",
        ".vscode",
        ".idea",
    }
)


def _is_known_non_source_directory(entry: str) -> bool:
    name = entry.rstrip("/").rpartition("/")[2]
    return name in _NON_SOURCE_DIRECTORY_NAMES or name.endswith(".egg-info")


def _warn_about_ignored_python_files(directory: Path) -> None:
    if not _CAN_STREAM_IGNORED_STATUS:
        logger.warning(
            "Ignored paths under %s could not be inspected on this platform; ignored Python files may be skipped "
            "during this directory scan.",
            directory,
        )
        return
    try:
        git = shutil.which("git") or "git"
        with subprocess.Popen(  # noqa: S603
            [
                git,
                "-C",
                directory,
                "status",
                "--porcelain=v1",
                "-z",
                "--ignored",
                "--untracked-files=normal",
                "--",
                ".",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            ignored = _read_ignored_status_paths(process)
    except OSError, subprocess.SubprocessError:
        logger.debug("git status --ignored failed", exc_info=True)
        return

    if ignored:
        shown = sorted(ignored)
        logger.warning(
            "at least %d gitignored path(s) under %s were excluded from this directory scan and may contain "
            ".py files these checks never examined; name a file explicitly on the command line to "
            "check it regardless of its ignore status (showing first %d): %s",
            len(ignored),
            directory,
            len(shown),
            ", ".join(shown),
        )


def _read_ignored_status_paths(process: subprocess.Popen[bytes]) -> list[str]:
    stdout = process.stdout
    stderr = process.stderr
    assert stdout is not None
    assert stderr is not None

    deadline = time.monotonic() + _GIT_STATUS_TIMEOUT_SECONDS
    streams = [stderr, stdout]
    pending = b""
    stderr_seen = False
    ignored: list[str] = []

    try:
        while streams:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, _GIT_STATUS_TIMEOUT_SECONDS)
            ready, _, _ = select.select(streams, [], [], max(deadline - time.monotonic(), 0))
            if not ready:
                raise subprocess.TimeoutExpired(process.args, _GIT_STATUS_TIMEOUT_SECONDS)
            for stream in sorted(ready, key=lambda stream: stream is stdout):
                chunk = os.read(stream.fileno(), 65_536)
                if not chunk:
                    streams.remove(stream)
                    continue
                if stream is stderr:
                    stderr_seen = True
                    continue
                if len(pending) + len(chunk) > _MAX_PENDING_IGNORED_STATUS_BYTES:
                    return []
                pending += chunk
                entries = pending.split(b"\0")
                pending = entries.pop()
                for entry in entries:
                    path = _ignored_status_path(entry)
                    if path is None:
                        continue
                    ignored.append(path)
                    if len(ignored) == _MAX_REPORTED_IGNORED_PATHS:
                        if stderr_seen:
                            return []
                        return ignored

        if pending or stderr_seen:
            return []
        if process.wait(timeout=max(deadline - time.monotonic(), 0)) != 0:
            return []
        return ignored
    finally:
        _stop_process(process)


def _ignored_status_path(entry: bytes) -> str | None:
    if not entry.startswith(b"!! "):
        return None
    path = os.fsdecode(entry[len(b"!! ") :])
    if not path.endswith((".py", "/")):
        return None
    if path.endswith("/") and _is_known_non_source_directory(path):
        return None
    return path


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
