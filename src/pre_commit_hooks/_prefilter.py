"""Batch file pre-filtering using git grep for performance.

This module provides fast file filtering using git grep to eliminate files
that don't need processing. Falls back to Python substring search if git
is unavailable.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

__all__ = ["git_grep_filter", "batch_filter_files"]


logger = logging.getLogger("linter")


def git_grep_filter(
    filepaths: Sequence[str], pattern: str, fixed_string: bool = False
) -> list[str]:
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

    try:
        # Build git grep command
        cmd = ["git", "grep", "--files-with-matches", "--null"]
        if fixed_string:
            cmd.append("--fixed-strings")
        cmd.extend(["-e", pattern, "--"])
        cmd.extend(filepaths)

        git_grep_result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=30
        )

        # Tests mock subprocess to force fallback path; this success path
        # is exercised in real-world usage and benchmarks
        if git_grep_result.returncode == 0:  # pragma: no cover
            # Parse null-separated output
            # Git grep returns paths relative to repo root, but we need to preserve
            # the format of input paths (absolute vs relative)
            git_matches = {f for f in git_grep_result.stdout.split("\0") if f}

            # Build mapping: resolved path -> original input path
            input_map = {Path(fp).resolve(): fp for fp in filepaths}

            # Map git results back to original input paths
            matches = []
            for git_path in git_matches:
                resolved = Path(git_path).resolve()
                if resolved in input_map:
                    matches.append(input_map[resolved])

            return matches
        # Tests mock subprocess to force fallback path; this "no matches"
        # path is exercised in real-world usage
        elif git_grep_result.returncode == 1:  # pragma: no cover
            # No matches found (not an error)
            return []
        else:
            # Error occurred, fall back to Python
            return _python_fallback_filter(filepaths, pattern)

    except (
        subprocess.SubprocessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ) as error:
        logger.error(repr(error))
        # git not available or timeout, fall back
        return _python_fallback_filter(filepaths, pattern)


def _python_fallback_filter(filepaths: Sequence[str], pattern: str) -> list[str]:
    matches = []
    for filepath in filepaths:
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
                if pattern in content:
                    matches.append(filepath)
        except (OSError, UnicodeDecodeError) as error:
            logger.error("File: %s, error: %s", filepath, repr(error))
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
