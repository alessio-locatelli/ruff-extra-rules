"""Batch file pre-filtering using git grep for performance.

This module provides fast file filtering using git grep to eliminate files
that don't need processing. Falls back to Python substring search if git
is unavailable.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["batch_filter_files", "git_grep_filter"]


logger = logging.getLogger("linter")


def git_grep_filter(filepaths: Sequence[str], pattern: str, *, fixed_string: bool = False) -> list[str]:
    """Uses git grep to quickly filter files containing a pattern. This is much
    faster than parsing every file with Python. Falls back to Python substring
    search if git is unavailable.

    Example:
        >>> # Find files with "def get_"
        >>> candidates = git_grep_filter(all_files, "def get_", fixed_string=True)
        >>> # Process only candidate files
        >>> for filepath in candidates:
        ...     check_file(filepath)
    """
    if not filepaths:
        return []

    # git grep's own pathspec handling gives no reliable signal that a
    # specific input file was skipped rather than genuinely not matching: a
    # file that's vanished since the caller's file list was built (e.g.
    # deleted between pre-commit computing the changed-file list and this
    # hook actually running) exits 1 -- git grep's own "no matches" code --
    # with empty stdout *and* empty stderr, indistinguishable from an
    # ordinary non-match. A permission-denied file only ever surfaces as an
    # "error: failed to stat" line on stderr, without necessarily changing
    # the exit code for the files git *could* read. Trusting "absent from
    # stdout" as proof of "doesn't match" would silently drop such a file
    # from every check's candidate list, with zero trace it was ever
    # skipped. Anything not confirmed-readable up front is therefore always
    # kept as a candidate regardless of what git grep reports for it,
    # deferring the actual diagnosis to the hook's own
    # _check_file/_read_source -- the same "include it, let the hook handle
    # the error" contract _python_fallback_filter already applies to its own
    # read failures below.
    unreadable = [fp for fp in filepaths if not os.access(fp, os.R_OK)]

    try:
        # --untracked --no-exclude-standard: without these, git grep only
        # searches files already in the index. A file passed explicitly on
        # this hook's own CLI (AGENTS.md's documented direct-CLI workflow)
        # that hasn't been `git add`ed yet -- a brand-new file, or one
        # matched by .gitignore -- is otherwise never actually searched:
        # git grep still exits 1 with empty stdout *and* empty stderr, the
        # same signal as "searched and no match", so the file was silently
        # dropped from every check's candidate list with zero trace, giving
        # a false-clean exit 0 for content that was never examined. These
        # flags make git grep search the exact files this function was
        # asked about regardless of their VCS status, matching "an
        # explicitly requested file is always in scope" (ch. 12: "MUST
        # process only the requested scope"). This is unaffected by
        # pre-commit/prek's own normal invocation, which only ever passes
        # already-staged files.
        cmd = ["git", "grep", "--files-with-matches", "--null", "--untracked", "--no-exclude-standard"]
        if fixed_string:
            cmd.append("--fixed-strings")
        cmd.extend(["-e", pattern, "--"])
        cmd.extend(filepaths)

        # cmd is built entirely from this function's own hardcoded git-grep
        # flags plus filepaths supplied by this hook's own CLI invocation
        # (never from untrusted external input), so no shell is involved and
        # no argument here can inject another command. errors="surrogateescape":
        # a matched file's path is just bytes on Linux, never required to be
        # valid UTF-8 -- the default strict decoding would otherwise raise
        # UnicodeDecodeError for an oddly-encoded filename and crash this
        # entire prefilter pass instead of reporting the match. This is the
        # same handler os.fsdecode() already uses for filesystem paths, so it
        # never raises and round-trips back to the exact file when resolved
        # below.
        git_grep_result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, errors="surrogateescape", check=False, timeout=30
        )

        # A 0/1 returncode alone doesn't mean every input file was actually
        # processed cleanly: stderr can carry a per-file error (e.g.
        # "failed to stat") even though git still exits 0 or 1 for the
        # files it *could* read. Only trust stdout when stderr is empty too
        # -- otherwise fall back to the Python path below, which reads each
        # file itself rather than relying on git's account of what it saw.
        if git_grep_result.returncode == 0 and not git_grep_result.stderr:
            # git grep returns paths relative to repo root, but the format
            # of the input paths (absolute vs relative) must be preserved.
            git_matches = {Path(f).resolve() for f in git_grep_result.stdout.split("\0") if f}

            # Iterating a dict (insertion-ordered, following filepaths' own
            # order) rather than the git_matches set itself: string hashing
            # is randomized per process by default (PYTHONHASHSEED), so
            # iterating the set directly would make this function's own
            # return order vary run-to-run for identical input -- ch. 9:
            # "MUST NOT allow hash-table ... order to affect the result".
            input_map = {Path(fp).resolve(): fp for fp in filepaths}
            matches = [fp for resolved, fp in input_map.items() if resolved in git_matches]

            return matches + unreadable
        if git_grep_result.returncode == 1 and not git_grep_result.stderr:
            # No matches found (not an error).
            return unreadable
        return _python_fallback_filter(filepaths, pattern)

    except (
        subprocess.SubprocessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        # Self-healing: falls back to _python_fallback_filter below, which
        # produces the same candidate set (just slower). Debug-only — an
        # ERROR-level .exception() call here would leak a raw traceback onto
        # the user's stderr by default (nothing in this codebase configures
        # logging, so Python's own lastResort handler prints WARNING+
        # straight to stderr) for a condition nothing actually failed at.
        logger.debug("git grep failed", exc_info=True)
        # git not available or timeout, fall back
        return _python_fallback_filter(filepaths, pattern)


def _python_fallback_filter(filepaths: Sequence[str], pattern: str) -> list[str]:
    matches = []
    for filepath in filepaths:
        try:
            with Path(filepath).open(encoding="utf-8") as f:
                content = f.read()
                if pattern in content:
                    matches.append(filepath)
        except OSError, UnicodeDecodeError:
            # Debug-only: the file is kept in as a candidate below, and the
            # hook's own downstream read (_read_source) cleanly reports this
            # same failure to the user — an ERROR-level .exception() call
            # here would just leak a redundant raw traceback onto stderr by
            # default (see git_grep_filter's own except block above).
            logger.debug("File: %s", filepath, exc_info=True)
            # Include file if we can't read it (let hook handle error)
            matches.append(filepath)
    return matches


def batch_filter_files(filepaths: Sequence[str], patterns: list[str]) -> list[str]:
    """
    Example:
        >>> # Find files with "data" OR "result"
        >>> matches = batch_filter_files(files, ["data", "result"])
    """
    if not patterns:
        return list(filepaths)

    # OR: file matches if it contains ANY pattern
    all_matches = set()
    for pattern in patterns:
        matches = git_grep_filter(filepaths, pattern, fixed_string=True)
        all_matches.update(matches)
    return sorted(all_matches)
