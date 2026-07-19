"""Tests for _prefilter module."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from pre_commit_hooks._prefilter import (
    batch_filter_files,
    git_grep_filter,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def sample_files(tmp_path: Path) -> list[str]:
    files = []

    file1 = tmp_path / "file1.py"
    file1.write_text("def get_name():\n    return 'foo'\n")
    files.append(str(file1))

    file2 = tmp_path / "file2.py"
    file2.write_text("data = {'key': 'value'}\n")
    files.append(str(file2))

    file3 = tmp_path / "file3.py"
    file3.write_text("data = load()\nresult = process(data)\n")
    files.append(str(file3))

    file4 = tmp_path / "file4.py"
    file4.write_text("def foo():\n    pass\n")
    files.append(str(file4))

    return files


@pytest.mark.parametrize(
    ("pattern", "expected_names"),
    [
        ("def get_", {"file1.py"}),
        ("data", {"file2.py", "file3.py"}),
        ("nonexistent", set()),
    ],
    ids=["single-match", "multiple-matches", "no-matches"],
)
def test_git_grep_filter_fixed_string(sample_files: list[str], pattern: str, expected_names: set[str]) -> None:
    matches = git_grep_filter(sample_files, pattern, fixed_string=True)
    assert {Path(m).name for m in matches} == expected_names


def test_git_grep_filter_empty_input() -> None:
    assert git_grep_filter([], "pattern") == []


def test_git_grep_filter_regex_pattern(sample_files: list[str]) -> None:
    # If files aren't in a git repo, this falls back to Python substring
    # search, which doesn't support regex, so the pattern used here must
    # work either way.
    matches = git_grep_filter(sample_files, "def get_", fixed_string=False)

    assert len(matches) >= 1
    assert any(m.endswith("file1.py") for m in matches)


def test_git_grep_filter_real_success_and_no_match_paths(tmp_path: Path) -> None:
    # Exercises the actual git-grep-succeeded (returncode == 0) and
    # git-grep-found-nothing (returncode == 1) branches without mocking
    # subprocess. The other tests here use files outside any git repo, so
    # `git grep` always errors out and they only ever exercise the Python
    # fallback path.
    git = shutil.which("git")
    assert git is not None

    original_dir = Path.cwd()
    try:
        os.chdir(tmp_path)
        # Args are hardcoded test setup, not untrusted input.
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        file1 = tmp_path / "file1.py"
        file1.write_text("def get_name():\n    return 'foo'\n")
        subprocess.run([git, "add", "file1.py"], check=True, cwd=tmp_path)  # noqa: S603

        matches = git_grep_filter([str(file1)], "def get_", fixed_string=True)
        assert matches == [str(file1)]

        no_matches = git_grep_filter([str(file1)], "totally_absent_pattern", fixed_string=True)
        assert no_matches == []
    finally:
        os.chdir(original_dir)


def test_git_grep_filter_skips_unresolvable_git_paths(tmp_path: Path) -> None:
    # Defensive: if git's null-separated output includes a path that
    # doesn't resolve back to one of the requested filepaths, it's skipped
    # rather than included as a bogus match.
    #
    # The file content deliberately doesn't contain "data" so the assertion
    # can only pass via the mocked git-grep-success branch, not by
    # coincidentally falling through to the Python substring fallback.
    file1 = tmp_path / "file1.py"
    file1.write_text("value = 1\n")

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{file1}\0/does/not/exist/in/input.py\0",
            stderr="",
        )
        matches = git_grep_filter([str(file1)], "data", fixed_string=True)

    assert matches == [str(file1)]


def _fail_not_a_git_repo(mock_run: mock.MagicMock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="", stderr="fatal: not a git repository"
    )


def _fail_timeout(mock_run: mock.MagicMock) -> None:
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="git grep", timeout=30)


def _fail_git_not_found(mock_run: mock.MagicMock) -> None:
    mock_run.side_effect = FileNotFoundError("git not found")


@pytest.mark.parametrize(
    "configure_mock",
    [_fail_not_a_git_repo, _fail_timeout, _fail_git_not_found],
    ids=["not-a-git-repo", "timeout", "git-not-found"],
)
def test_git_grep_filter_falls_back_to_python_search(
    sample_files: list[str], configure_mock: Callable[[mock.MagicMock], None]
) -> None:
    with mock.patch("subprocess.run") as mock_run:
        configure_mock(mock_run)
        matches = git_grep_filter(sample_files, "data", fixed_string=True)

    assert len(matches) >= 1
    assert any(m.endswith("file2.py") for m in matches)


def test_python_fallback_includes_unreadable_files(tmp_path: Path) -> None:
    file1 = tmp_path / "readable.py"
    file1.write_text("data = 123")

    file2 = tmp_path / "unreadable.py"
    file2.write_text("content")
    file2.chmod(0o000)

    try:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            matches = git_grep_filter([str(file1), str(file2)], "data")

            # Unreadable files are kept in; the hook itself surfaces the read error.
            assert str(file2) in matches
    finally:
        file2.chmod(0o644)


def test_git_grep_handles_binary_files(tmp_path: Path) -> None:
    binary_file = tmp_path / "binary.pyc"
    binary_file.write_bytes(b"\x00\x01\x02\x03data\x04\x05")

    text_file = tmp_path / "text.py"
    text_file.write_text("data = 123")

    matches = git_grep_filter([str(binary_file), str(text_file)], "data")

    assert any(m.endswith("text.py") for m in matches)


@pytest.mark.parametrize(
    ("patterns", "expected_names"),
    [
        (["data", "def get_"], {"file1.py", "file2.py", "file3.py"}),
        ([], {"file1.py", "file2.py", "file3.py", "file4.py"}),
        (["nonexistent1", "nonexistent2"], set()),
    ],
    ids=["match-any-pattern", "empty-patterns-returns-all", "no-matches"],
)
def test_batch_filter_files(sample_files: list[str], patterns: list[str], expected_names: set[str]) -> None:
    matches = batch_filter_files(sample_files, patterns)
    assert {Path(m).name for m in matches} == expected_names
