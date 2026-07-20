"""File discovery: turning the CLI's raw filename arguments into a concrete list of files to check."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("ast_checks")


def filter_excluded_files(filepaths: list[str], exclude_patterns: list[str]) -> list[str]:
    """Filter out files matching exclude patterns.

    Args:
        filepaths: List of file paths to filter
        exclude_patterns: List of glob patterns to exclude

    Returns:
        Filtered list of file paths
    """
    if not exclude_patterns:
        return filepaths

    filtered = []
    for filepath_str in filepaths:
        filepath = Path(filepath_str)
        excluded = False

        for pattern in exclude_patterns:
            # Check if file matches pattern using glob-style matching
            # Support both relative and absolute patterns
            if filepath.match(pattern):
                excluded = True
                break
            # Also check if any parent directory matches
            if any(part for part in filepath.parts if Path(part).match(pattern)):
                excluded = True
                break

        if not excluded:
            filtered.append(filepath_str)

    return filtered


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

    Args:
        filenames: Raw CLI filename arguments, files and/or directories mixed

    Returns:
        `filenames` with each directory entry replaced by the `.py` files
        found under it (see `_list_python_files_in_dir`); a non-directory
        entry (an ordinary file, or a path that doesn't exist at all) is
        kept as-is so the existing unreadable/unprocessable-file reporting
        still applies to it downstream.
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

    Prefers `git ls-files` (tracked, `.gitignore`-aware — consistent with
    `_prefilter.py`'s own git-grep based discovery, and avoids sweeping in
    `.venv`/build artifacts that happen to live under the given directory)
    and falls back to a plain recursive glob outside a git repo or when git
    itself is unavailable.
    """
    resolved_dir = directory.resolve()
    try:
        cmd = ["git", "-C", str(directory), "ls-files", "-z"]
        # cmd is built entirely from this function's own hardcoded git
        # subcommand/flags plus a directory supplied by this hook's own CLI
        # invocation (never from untrusted external input), so no shell is
        # involved and no argument here can inject another command.
        git_ls_files_result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, check=False, timeout=30
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
            return sorted(
                str(candidate)
                for f in git_ls_files_result.stdout.split("\0")
                if f.endswith(".py") and (candidate := resolved_dir / f).exists()
            )
    except subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired:
        # Self-healing: falls back to the equivalent rglob scan below.
        # Debug-only — an ERROR-level .exception() call here would leak a
        # raw traceback onto the user's stderr by default (nothing in this
        # codebase configures logging, so Python's own lastResort handler
        # prints WARNING+ straight to stderr) for a condition nothing
        # actually failed at from the user's perspective.
        logger.debug("git ls-files failed", exc_info=True)

    return sorted(str(p) for p in resolved_dir.rglob("*.py"))
