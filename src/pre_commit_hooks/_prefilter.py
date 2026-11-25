from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pre_commit_hooks._git import run_git

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["batch_filter_files"]


def batch_filter_files(filepaths: Sequence[str], patterns: list[str]) -> list[str]:
    if not patterns:
        return list(filepaths)

    unreadable = {fp for fp in filepaths if not os.access(fp, os.R_OK)}
    searchable = [fp for fp in filepaths if fp not in unreadable]
    if not searchable:
        return sorted(unreadable)

    args = ["--no-color", "--files-with-matches", "--null", "--untracked", "--no-exclude-standard", "--fixed-strings"]
    for pattern in patterns:
        args.extend(("-e", pattern))
    args.append("--")
    args.extend(searchable)

    stdout = run_git("grep", args, ok_codes=(0, 1))
    if not stdout:
        return sorted(unreadable)

    git_matches = {Path(name).resolve() for name in stdout.split("\0") if name}
    input_map = {Path(fp).resolve(): fp for fp in searchable}
    return sorted(unreadable | {fp for path, fp in input_map.items() if path in git_matches})
