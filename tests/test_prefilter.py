from __future__ import annotations

import errno
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from pre_commit_hooks._git import GitError
from pre_commit_hooks._prefilter import batch_filter_files
from tests._helpers import restricted_permissions


@pytest.fixture
def sample_files(git_worktree: Path) -> list[str]:
    sources = {
        "tracked.py": "def get_name():\n    return 'foo'\n",
        ":(glob)x.py": "data = {'key': 'value'}\n",
        "ignored.py": "data = load()\n",
        "other.py": "def foo():\n    pass\n",
    }
    (git_worktree / ".gitignore").write_text("ignored.py\n")
    for name, source in sources.items():
        (git_worktree / name).write_text(source)
    subprocess.run(["git", "add", "tracked.py"], check=True)  # noqa: S607 -- Git is required on PATH.
    return [str(git_worktree / name) for name in sources]


@pytest.mark.parametrize(
    ("patterns", "expected_names"),
    [
        (["def get_"], ["tracked.py"]),
        (["data"], [":(glob)x.py", "ignored.py"]),
        (["data", "def get_"], [":(glob)x.py", "ignored.py", "tracked.py"]),
        (["absent"], []),
        (["d.ta"], []),
        ([], ["tracked.py", ":(glob)x.py", "ignored.py", "other.py"]),
    ],
    ids=["tracked", "literal-path-and-ignored-file", "any-pattern", "no-match", "literal-pattern", "no-patterns"],
)
def test_batch_filter_files(sample_files: list[str], patterns: list[str], expected_names: list[str]) -> None:
    assert [Path(name).name for name in batch_filter_files(sample_files, patterns)] == expected_names


def test_batch_filter_files_deduplicates_path_aliases(sample_files: list[str]) -> None:
    tracked = sample_files[0]
    assert batch_filter_files([tracked, "tracked.py", tracked], ["def get_"]) == [tracked]


def test_batch_filter_files_empty_input() -> None:
    assert batch_filter_files([], ["data"]) == []


def test_batch_filter_files_keeps_unreadable_inputs(git_worktree: Path) -> None:
    unreadable = git_worktree / "unreadable.py"
    unreadable.write_text("data = 1\n")
    subprocess.run(["git", "add", "unreadable.py"], check=True)  # noqa: S607 -- Git is required on PATH.
    readable = git_worktree / "readable.py"
    readable.write_text("data = 2\n")
    vanished = git_worktree / "vanished.py"

    with restricted_permissions(unreadable, 0o000, restore=0o644):
        assert batch_filter_files([str(vanished), str(unreadable), str(readable)], ["data"]) == [
            str(readable),
            str(unreadable),
            str(vanished),
        ]


def test_git_grep_handles_binary_files(git_worktree: Path) -> None:
    binary_file = git_worktree / "binary.pyc"
    binary_file.write_bytes(b"\x00\x01\x02\x03data\x04\x05")
    text_file = git_worktree / "text.py"
    text_file.write_text("data = 123")

    assert batch_filter_files([str(binary_file), str(text_file)], ["data"]) == [str(binary_file), str(text_file)]


@pytest.mark.parametrize(
    ("exit_status", "stderr", "diagnostic"),
    [
        (0, "cannot open source", "cannot open source"),
        (1, "permission denied", "permission denied"),
        (128, "", "exit status 128"),
    ],
    ids=["match-with-stderr", "no-match-with-stderr", "unexpected-status-without-stderr"],
)
def test_git_grep_rejects_failed_search(
    sample_files: list[str], exit_status: int, stderr: str, diagnostic: str
) -> None:
    with (
        mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], exit_status, b"", stderr.encode())),
        pytest.raises(GitError, match=diagnostic),
    ):
        batch_filter_files(sample_files, ["data"])


@pytest.mark.parametrize(
    ("error", "diagnostic"),
    [
        (FileNotFoundError(), "Ensure Git is installed and available on PATH"),
        (subprocess.TimeoutExpired("git grep", 30), "git grep timed out after 30 seconds"),
        (OSError(errno.E2BIG, "Argument list too long"), "Split the file list into smaller runs"),
        (PermissionError(errno.EACCES, "Permission denied"), "git grep could not start.*Permission denied"),
    ],
    ids=["missing-git", "timeout", "argument-list-too-long", "permission-denied"],
)
def test_git_grep_reports_subprocess_errors(sample_files: list[str], error: Exception, diagnostic: str) -> None:
    with mock.patch("subprocess.run", side_effect=error), pytest.raises(GitError, match=diagnostic):
        batch_filter_files(sample_files, ["data"])
