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
    return _git_grep_any(filepaths, (pattern,), fixed_string=fixed_string)


def _git_grep_any(filepaths: Sequence[str], patterns: Sequence[str], *, fixed_string: bool) -> list[str]:
    if not filepaths:
        return []

    unreadable = [fp for fp in filepaths if not os.access(fp, os.R_OK)]

    try:
        cmd = ["git", "grep", "--no-color", "--files-with-matches", "--null", "--untracked", "--no-exclude-standard"]
        if fixed_string:
            cmd.append("--fixed-strings")
        for pattern in patterns:
            cmd.extend(["-e", pattern])
        cmd.append("--")
        cmd.extend(filepaths)

        git_grep_result = subprocess.run(  # noqa: S603 -- Git operands follow --; no shell.
            cmd, capture_output=True, text=True, errors="surrogateescape", check=False, timeout=30
        )

        if git_grep_result.returncode == 0 and not git_grep_result.stderr:
            git_matches = {Path(f).resolve() for f in git_grep_result.stdout.split("\0") if f}

            input_map = {Path(fp).resolve(): fp for fp in filepaths}
            matches = [fp for resolved, fp in input_map.items() if resolved in git_matches]

            return matches + unreadable
        if git_grep_result.returncode == 1 and not git_grep_result.stderr:
            return unreadable
        return _python_fallback_filter(filepaths, patterns)

    except (
        subprocess.SubprocessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        logger.debug("git grep failed", exc_info=True)
        return _python_fallback_filter(filepaths, patterns)


def _python_fallback_filter(filepaths: Sequence[str], patterns: Sequence[str]) -> list[str]:
    matches = []
    for filepath in filepaths:
        try:
            with Path(filepath).open(encoding="utf-8") as f:
                content = f.read()
                if any(pattern in content for pattern in patterns):
                    matches.append(filepath)
        except OSError, UnicodeDecodeError:
            logger.debug("File: %s", filepath, exc_info=True)
            matches.append(filepath)
    return matches


def batch_filter_files(filepaths: Sequence[str], patterns: list[str]) -> list[str]:
    if not patterns:
        return list(filepaths)

    return sorted(set(_git_grep_any(filepaths, patterns, fixed_string=True)))
