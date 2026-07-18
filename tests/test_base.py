"""Tests for the ast_checks._base shared utilities."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks._base import atomic_write_text, byte_col_to_char_col

if TYPE_CHECKING:
    from pathlib import Path


def test_byte_col_to_char_col_ascii_line_is_identity() -> None:
    line = "    data = calc()"
    assert byte_col_to_char_col(line, line.index("calc")) == line.index("calc")


def test_byte_col_to_char_col_converts_past_multibyte_char() -> None:
    line = '    x = "café"; return data'
    byte_offset = line.encode("utf-8").index(b"data")
    assert byte_col_to_char_col(line, byte_offset) == line.index("data")


def test_byte_col_to_char_col_converts_past_four_byte_char() -> None:
    line = "    x = '😀😀'; data = 1"
    byte_offset = line.encode("utf-8").index(b"data")
    assert byte_col_to_char_col(line, byte_offset) == line.index("data")


def test_atomic_write_text_replaces_file_content(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("old\n")

    atomic_write_text(target, "new\n", "utf-8")

    assert target.read_text() == "new\n"
    assert list(tmp_path.glob(".mod.py.*.tmp")) == []


def test_atomic_write_text_preserves_permission_bits(tmp_path: Path) -> None:
    # mkstemp creates its temp file mode 0600; the executable bit on a
    # script being fixed must survive the rename, not silently regress.
    target = tmp_path / "script.py"
    target.write_text("old\n")
    target.chmod(0o755)

    atomic_write_text(target, "new\n", "utf-8")

    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_atomic_write_text_updates_symlink_target_in_place(tmp_path: Path) -> None:
    # replace() acts on the directory entry it's given; writing through the
    # symlink path directly would turn the tracked symlink into a plain
    # file instead of updating what it points to.
    real_target = tmp_path / "real.py"
    real_target.write_text("old\n")
    link = tmp_path / "link.py"
    link.symlink_to(real_target)

    atomic_write_text(link, "new\n", "utf-8")

    assert link.is_symlink()
    assert link.resolve() == real_target
    assert real_target.read_text() == "new\n"


def test_atomic_write_text_cleans_up_temp_file_on_failure(tmp_path: Path) -> None:
    # A directory in place of the target makes the rename step fail without
    # ever touching the temp file's own write, exercising the cleanup path.
    target = tmp_path / "mod.py"
    target.mkdir()

    with pytest.raises(IsADirectoryError):
        atomic_write_text(target, "new\n", "utf-8")

    assert list(tmp_path.glob(".mod.py.*.tmp")) == []
