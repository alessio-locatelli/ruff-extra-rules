"""Tests for the ast_checks._base shared utilities."""

from __future__ import annotations

import stat
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks._base import atomic_write_text, byte_col_to_char_col

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ("    data = calc()", b"calc"),
        ('    x = "café"; return data', b"data"),
        ("    x = '😀😀'; data = 1", b"data"),
    ],
    ids=["ascii", "two-byte-char", "four-byte-char"],
)
def test_byte_col_to_char_col(line: str, needle: bytes) -> None:
    byte_offset = line.encode("utf-8").index(needle)
    char_offset = line.index(needle.decode())
    assert byte_col_to_char_col(line, byte_offset) == char_offset


def _setup_plain(tmp_path: Path) -> Path:
    target = tmp_path / "mod.py"
    target.write_text("old\n")
    return target


def _verify_plain(target: Path) -> None:
    assert target.read_text() == "new\n"


def _setup_executable(tmp_path: Path) -> Path:
    target = tmp_path / "script.py"
    target.write_text("old\n")
    target.chmod(0o755)
    return target


def _verify_permission_preserved(target: Path) -> None:
    # mkstemp creates its temp file mode 0600; the executable bit on a
    # script being fixed must survive the rename, not silently regress.
    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def _setup_symlink(tmp_path: Path) -> Path:
    real_target = tmp_path / "real.py"
    real_target.write_text("old\n")
    link = tmp_path / "link.py"
    link.symlink_to(real_target)
    return link


def _verify_symlink_updated_in_place(target: Path) -> None:
    # replace() acts on the directory entry it's given; writing through the
    # symlink path directly would turn the tracked symlink into a plain
    # file instead of updating what it points to.
    real_target = target.parent / "real.py"
    assert target.is_symlink()
    assert target.resolve() == real_target
    assert real_target.read_text() == "new\n"


def _setup_directory(tmp_path: Path) -> Path:
    target = tmp_path / "baddir.py"
    target.mkdir()
    return target


@pytest.mark.parametrize(
    ("setup", "verify", "raises_error"),
    [
        (_setup_plain, _verify_plain, False),
        (_setup_executable, _verify_permission_preserved, False),
        (_setup_symlink, _verify_symlink_updated_in_place, False),
        (_setup_directory, None, True),
    ],
    ids=["plain-file", "preserves-permissions", "updates-symlink-target", "target-is-directory-raises"],
)
def test_atomic_write_text(
    tmp_path: Path,
    setup: Callable[[Path], Path],
    verify: Callable[[Path], None] | None,
    *,
    raises_error: bool,
) -> None:
    target = setup(tmp_path)

    # A directory in place of the target makes the rename step fail without
    # ever touching the temp file's own write, exercising the cleanup path.
    ctx = pytest.raises(IsADirectoryError) if raises_error else nullcontext()
    with ctx:
        atomic_write_text(target, "new\n", "utf-8")

    if verify is not None:
        verify(target)
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []
