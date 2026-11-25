from __future__ import annotations

import errno
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_GIT_TIMEOUT_SECONDS = 30


class GitError(RuntimeError):
    pass


def run_git(operation: str, args: Sequence[str], *, cwd: Path | None = None, ok_codes: tuple[int, ...] = (0,)) -> str:
    cmd: list[str | Path] = ["git", "--literal-pathspecs"]
    if cwd is not None:
        cmd.extend(("-C", cwd))
    cmd.extend((operation, *args))
    try:
        git_process = subprocess.run(  # noqa: S603 -- Git arguments are passed without a shell.
            cmd,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        msg = f"git {operation} could not start. Ensure Git is installed and available on PATH."
        raise GitError(msg) from error
    except subprocess.TimeoutExpired as error:
        msg = f"git {operation} timed out after {_GIT_TIMEOUT_SECONDS} seconds"
        raise GitError(msg) from error
    except OSError as error:
        if error.errno == errno.E2BIG:
            msg = f"git {operation} argument list is too long. Split the file list into smaller runs."
            raise GitError(msg) from error
        msg = f"git {operation} could not start ({error})"
        raise GitError(msg) from error

    if git_process.returncode not in ok_codes or git_process.stderr:
        detail = os.fsdecode(git_process.stderr).strip() or f"exit status {git_process.returncode}"
        where = f" in {cwd}" if cwd is not None else ""
        msg = f"git {operation} failed{where} ({detail})"
        raise GitError(msg)
    return os.fsdecode(git_process.stdout)
