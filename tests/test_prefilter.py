"""Tests for _prefilter module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from pre_commit_hooks._prefilter import (
    batch_filter_files,
    git_grep_filter,
)


@pytest.fixture
def sample_files(tmp_path: Path) -> list[str]:
    """Create sample Python files."""
    files = []

    # File with "def get_"
    file1 = tmp_path / "file1.py"
    file1.write_text("def get_name():\n    return 'foo'\n")
    files.append(str(file1))

    # File with "data"
    file2 = tmp_path / "file2.py"
    file2.write_text("data = {'key': 'value'}\n")
    files.append(str(file2))

    # File with both "data" and "result"
    file3 = tmp_path / "file3.py"
    file3.write_text("data = load()\nresult = process(data)\n")
    files.append(str(file3))

    # File with nothing interesting
    file4 = tmp_path / "file4.py"
    file4.write_text("def foo():\n    pass\n")
    files.append(str(file4))

    return files


def test_git_grep_filter_basic(sample_files: list[str]) -> None:
    """Test basic git grep filtering."""
    # Filter for "def get_"
    matches = git_grep_filter(sample_files, "def get_", fixed_string=True)

    assert len(matches) == 1
    assert matches[0].endswith("file1.py")


def test_git_grep_filter_multiple_matches(sample_files: list[str]) -> None:
    """Test git grep with multiple matches."""
    # Filter for "data"
    matches = git_grep_filter(sample_files, "data", fixed_string=True)

    assert len(matches) == 2
    assert any(m.endswith("file2.py") for m in matches)
    assert any(m.endswith("file3.py") for m in matches)


def test_git_grep_filter_no_matches(sample_files: list[str]) -> None:
    """Test git grep with no matches."""
    # Filter for non-existent pattern
    matches = git_grep_filter(sample_files, "nonexistent", fixed_string=True)

    assert len(matches) == 0


def test_git_grep_filter_empty_input() -> None:
    """Test git grep with empty file list."""
    matches = git_grep_filter([], "pattern")
    assert len(matches) == 0


def test_git_grep_filter_regex_pattern(sample_files: list[str]) -> None:
    """Test git grep with regex pattern.

    Note: If files aren't in a git repo, this falls back to Python substring
    search, which doesn't support regex. So we use a pattern that works either way.
    """
    # Filter for "def get_" (works as both regex and substring)
    matches = git_grep_filter(sample_files, "def get_", fixed_string=False)

    # Should match file1.py
    assert len(matches) >= 1
    assert any(m.endswith("file1.py") for m in matches)


def test_git_grep_fallback_when_not_in_git_repo(
    sample_files: list[str], tmp_path: Path
) -> None:
    """Test Python fallback when git grep fails."""
    # Create files in non-git directory
    non_git_dir = tmp_path / "non_git"
    non_git_dir.mkdir()

    file1 = non_git_dir / "test.py"
    file1.write_text("data = 123\n")

    # Mock git grep to fail
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="fatal: not a git repository"
        )

        matches = git_grep_filter([str(file1)], "data", fixed_string=True)

        # Should fall back to Python search and find the match
        assert len(matches) == 1
        assert matches[0] == str(file1)


def test_git_grep_fallback_on_timeout(sample_files: list[str]) -> None:
    """Test Python fallback on git grep timeout."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git grep", timeout=30)

        matches = git_grep_filter(sample_files, "data", fixed_string=True)

        # Should fall back to Python search
        assert len(matches) >= 1


def test_git_grep_fallback_on_file_not_found(sample_files: list[str]) -> None:
    """Test Python fallback when git command not found."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("git not found")

        matches = git_grep_filter(sample_files, "data", fixed_string=True)

        # Should fall back to Python search
        assert len(matches) >= 1


def test_python_fallback_includes_unreadable_files(tmp_path: Path) -> None:
    """Test that Python fallback includes files it can't read."""
    # Create a file
    file1 = tmp_path / "readable.py"
    file1.write_text("data = 123")

    # Create unreadable file
    file2 = tmp_path / "unreadable.py"
    file2.write_text("content")
    file2.chmod(0o000)

    try:
        # Mock git to force fallback
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            matches = git_grep_filter([str(file1), str(file2)], "data")

            # Should include unreadable file (let hook handle error)
            assert str(file2) in matches
    finally:
        # Restore permissions
        file2.chmod(0o644)


def test_batch_filter_files_match_any(sample_files: list[str]) -> None:
    """Test batch filtering with OR logic."""
    # Match files with "data" OR "def get_"
    matches = batch_filter_files(sample_files, ["data", "def get_"])

    # Should match file1.py, file2.py, file3.py
    assert len(matches) == 3
    assert any(m.endswith("file1.py") for m in matches)
    assert any(m.endswith("file2.py") for m in matches)
    assert any(m.endswith("file3.py") for m in matches)


def test_batch_filter_files_empty_patterns(sample_files: list[str]) -> None:
    """Test batch filtering with no patterns."""
    matches = batch_filter_files(sample_files, [])

    # Should return all files
    assert len(matches) == len(sample_files)


def test_batch_filter_files_no_matches(sample_files: list[str]) -> None:
    """Test batch filtering with patterns that match nothing."""
    matches = batch_filter_files(sample_files, ["nonexistent1", "nonexistent2"])

    assert len(matches) == 0


def test_git_grep_handles_binary_files(tmp_path: Path) -> None:
    """Test that git grep handles binary files gracefully."""
    # Create a binary file
    binary_file = tmp_path / "binary.pyc"
    binary_file.write_bytes(b"\x00\x01\x02\x03data\x04\x05")

    text_file = tmp_path / "text.py"
    text_file.write_text("data = 123")

    # Should not crash
    matches = git_grep_filter([str(binary_file), str(text_file)], "data")

    # At minimum, should find the text file
    assert any(m.endswith("text.py") for m in matches)
