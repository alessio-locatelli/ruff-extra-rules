"""File discovery: turning the CLI's raw filename arguments into a concrete list of files to check."""

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

from ._globs import anchored_patterns, glob_matches, relative_to_anchor

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("ast_checks")

_CURRENT_DIR = PurePosixPath()


class ExcludePattern(NamedTuple):
    """A glob plus the directory it resolves against; see
    `docs/adr/0046-exclude-glob-semantics.md`.
    """

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
        if relative is not None and any(_matches(relative, pattern, anchor) for pattern in patterns):
            return True
    return False


def _matches(relative: PurePosixPath, pattern: str, anchor: Path) -> bool:
    """See `docs/adr/0046-exclude-glob-semantics.md`."""
    if "/" not in pattern:
        return any(glob_matches(pattern, part) for part in relative.parts)
    candidates = [str(relative), *(str(parent) for parent in relative.parents if parent != _CURRENT_DIR)]
    return any(
        glob_matches(anchored, candidate) for anchored in anchored_patterns(pattern, anchor) for candidate in candidates
    )


def expand_directories(filenames: list[str]) -> list[str]:
    """Expand any directory argument into the `.py` files it contains.

    pre-commit/prek's own `types: [python]` hook contract (`.pre-commit-hooks.yaml`)
    always passes individual files, never a directory, so this only matters
    for direct CLI use (e.g. `ruff-extra-rules src/`, the form this
    project's own dev docs use — see AGENTS.md). Without it, a directory
    argument reached `CheckOrchestrator.process_files()` as a single
    unexpanded path: git grep's own directory pathspec support made it
    recurse and report matches for files *inside* that directory, but those
    resolved paths never matched the literal directory path in
    `git_grep_filter`'s own input map, so every result was silently
    discarded as unresolvable — the run reported zero violations, exit code
    0, having actually checked nothing.

    Each directory entry in `filenames` is replaced by the `.py` files found
    under it (see `_list_python_files_in_dir`); a non-directory entry (an
    ordinary file, or a path that doesn't exist at all) is kept as-is so the
    existing unreadable/unprocessable-file reporting still applies to it
    downstream.
    """
    expanded: list[str] = []
    for name in filenames:
        path = Path(name)
        if path.is_dir():
            expanded.extend(_list_python_files_in_dir(path))
        else:
            expanded.append(name)
    return expanded


def _list_python_files_in_dir(directory: Path) -> list[str]:
    """`.py` files under `directory`, as resolved absolute paths regardless
    of whether `directory` itself was given as a relative or absolute
    string — `git ls-files` always reports paths relative to whatever `-C`
    directory it was run against, so returning its output as-is would make
    a directory argument's expansion inconsistent with a plain file
    argument (passed through in whatever form the caller used); resolving
    both branches the same way keeps that consistent (ch. 13: "MUST handle
    relative and absolute paths consistently").

    Prefers `git ls-files --cached --others --exclude-standard` — tracked
    plus untracked-but-not-`.gitignore`d, so a brand-new file that hasn't
    been `git add`ed yet matches `git_grep_filter`'s own treatment of that
    same file when it's named explicitly instead of via its containing
    directory (ADR 0024) — and falls back to a plain recursive glob outside
    a git repo or when git itself is unavailable. A genuinely `.gitignore`d
    file is still excluded (avoids sweeping in `.venv`/build artifacts that
    happen to live under the given directory, ADR 0015), but
    `_warn_about_ignored_python_files` below reports that exclusion instead
    of leaving it silent (ADR 0028, issue #67).
    """
    resolved_dir = directory.resolve()
    try:
        cmd: list[str | Path] = ["git", "-C", directory, "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        # cmd is built entirely from this function's own hardcoded git
        # subcommand/flags plus a directory supplied by this hook's own CLI
        # invocation (never from untrusted external input), so no shell is
        # involved and no argument here can inject another command.
        # errors="surrogateescape": paths are just bytes on Linux, never
        # required to be valid UTF-8 -- the default strict decoding would
        # otherwise raise UnicodeDecodeError for an oddly-encoded filename
        # and force falling all the way back to the untracked, non
        # `.gitignore`-aware rglob below for the *entire* directory, sweeping
        # in `.venv`/build artifacts to dodge one bad filename. surrogateescape
        # is the same handler `os.fsdecode()` already uses for filesystem
        # paths, so it never raises and round-trips back to the exact file
        # when resolved below.
        git_ls_files_result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, errors="surrogateescape", check=False, timeout=30
        )
        if git_ls_files_result.returncode == 0 and not git_ls_files_result.stderr:
            # git ls-files reports the *index*, not the working tree: a
            # tracked file deleted from disk without `git rm` still shows up
            # here even though it no longer exists. A directory scan isn't
            # asking about that specific file by name (unlike an explicit
            # file argument, which git_grep_filter always still surfaces so
            # its own removal is reported) -- it's asking "what's currently
            # under here", so a stale index entry is silently dropped rather
            # than reported as a fake unreadable file (ch. 12: "MUST avoid
            # relying on stale file lists when the user explicitly requests
            # a current filesystem state").
            python_files = sorted(
                str(candidate)
                for f in git_ls_files_result.stdout.split("\0")
                if f.endswith(".py") and (candidate := resolved_dir / f).exists()
            )
            _warn_about_ignored_python_files(directory)
            return python_files
    except subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired:
        # Self-healing: falls back to the equivalent rglob scan below.
        # Debug-only — an ERROR-level .exception() call here would leak a
        # raw traceback onto the user's stderr by default (nothing in this
        # codebase configures logging, so Python's own lastResort handler
        # prints WARNING+ straight to stderr) for a condition nothing
        # actually failed at from the user's perspective.
        logger.debug("git ls-files failed", exc_info=True)

    return sorted(str(p) for p in resolved_dir.rglob("*.py"))


_MAX_REPORTED_IGNORED_PATHS = 20
_MAX_PENDING_IGNORED_STATUS_BYTES = 65_536
_GIT_STATUS_TIMEOUT_SECONDS = 5
_PROCESS_STOP_TIMEOUT_SECONDS = 1
_CAN_STREAM_IGNORED_STATUS = os.name == "posix"

# Well-known packaging/tooling directory names, most of which are named in
# this project's own .gitignore -- every one of these gets created by this
# project's own routine `mypy`/`pytest`/`ruff`/`build`/`uv sync` commands, so
# warning about them unconditionally fired on essentially every
# directory-argument run rather than the occasional case ADR 0028
# anticipated (ADR 0029). `.ruff_cache` isn't itself named in this
# repository's top-level .gitignore -- ruff writes its own nested
# `.ruff_cache/.gitignore` containing `*`, which is what makes `git status`
# collapse it to a single ignored-directory line the same way a
# name-matched pattern would. None of these names are ever used for
# hand-written source, so skipping them costs nothing a directly-ignored
# `.py` file (still always reported below) wouldn't already catch.
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
