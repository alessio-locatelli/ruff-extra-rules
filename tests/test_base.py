from __future__ import annotations

import ast
import stat
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks._base import (
    FixValidationError,
    atomic_write_text,
    byte_col_to_char_col,
    fast_get_source_segment,
    is_fix_errored,
    is_fix_failed,
    is_fix_rejected,
    mark_fix_errored,
    mark_fix_failed,
    mark_fix_rejected,
    split_lines_like_ast,
)
from tests.factories import ViolationFactory

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


@pytest.mark.parametrize(
    "source",
    [
        "x = compute(1, 2)\n",
        "café = compute(x)  # café\n",
        "x = (\n    1 +\n    2\n)\n",
        "x = [1, 2, 3][0]\n",
        "x = 1",
        # A raw form-feed byte is legal intra-line whitespace to Python's
        # own tokenizer (end_lineno stays equal to lineno), but
        # str.splitlines() treats it as a line boundary and would
        # truncate the fast path's result if used directly — the reason
        # fast_get_source_segment requires split_lines_like_ast's lines,
        # not source.splitlines()'s.
        'x = requests.get("\x0curl", timeout=1)\n',
    ],
    ids=[
        "single-line",
        "unicode-before-node",
        "multiline-parenthesized",
        "single-line-subscript",
        "no-trailing-newline",
        "form-feed-inside-single-line-node",
    ],
)
def test_fast_get_source_segment_matches_ast_get_source_segment(source: str) -> None:
    tree = ast.parse(source)
    assign = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign))

    fast_result = fast_get_source_segment(source, split_lines_like_ast(source), assign.value)

    assert fast_result == ast.get_source_segment(source, assign.value)


def test_fast_get_source_segment_returns_none_without_end_position() -> None:
    source = "x = 1\n"
    tree = ast.parse(source)
    assign = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign))
    assign.value.end_lineno = None

    assert fast_get_source_segment(source, split_lines_like_ast(source), assign.value) is None


@pytest.mark.parametrize(
    "source",
    [
        "x = 1\ny = 2\n",
        "x = 1\r\ny = 2\r\n",
        "x = 1\ry = 2\r",
        "x = 1",
        'x = "a\x0cb"\ny = 2\n',
        "",
    ],
    ids=["lf", "crlf", "cr", "no-trailing-newline", "form-feed-is-not-a-boundary", "empty"],
)
def test_split_lines_like_ast_matches_ast_own_line_numbering(source: str) -> None:
    # ast._splitlines_no_ff is the private stdlib function
    # split_lines_like_ast deliberately reimplements rather than depends
    # on directly; this proves the two stay in agreement.
    assert split_lines_like_ast(source) == ast._splitlines_no_ff(source)  # type: ignore[attr-defined]


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


@pytest.mark.parametrize(
    "content",
    [
        "def broken(:\n",
        # Valid per the grammar alone (ast.parse accepts it) but invalid at
        # compile time — a fix producing this must be rejected too, not
        # just fixes with a plain grammar error.
        "return 1\n",
    ],
    ids=["grammar-error", "compile-time-only-error"],
)
def test_atomic_write_text_rejects_invalid_syntax(tmp_path: Path, content: str) -> None:
    # A bad fix must never reach disk: validation runs before the temp file
    # is even created, so the target keeps its prior content untouched.
    target = tmp_path / "mod.py"
    target.write_text("old = 1\n")

    with pytest.raises(FixValidationError):
        atomic_write_text(target, content, "utf-8")

    assert target.read_text() == "old = 1\n"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_fix_validation_error_exposes_path_and_syntax_error(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"

    with pytest.raises(FixValidationError) as exc_info:
        atomic_write_text(target, "def broken(:\n", "utf-8")

    assert exc_info.value.path == target
    assert isinstance(exc_info.value.syntax_error, SyntaxError)


@pytest.mark.parametrize(
    "fix_data",
    [None, {"other_key": 1}],
    ids=["no-fix-data", "existing-fix-data"],
)
def test_mark_fix_rejected(fix_data: dict[str, int] | None) -> None:
    violation = ViolationFactory.build(fix_data=fix_data)
    assert not is_fix_rejected(violation)

    mark_fix_rejected(violation)

    assert is_fix_rejected(violation)
    assert violation.fix_data is not None
    if fix_data is not None:
        assert violation.fix_data["other_key"] == 1


def test_is_fix_rejected_false_when_only_marked_fixed() -> None:
    violation = ViolationFactory.build(fix_data={"fixed": True})
    assert not is_fix_rejected(violation)


@pytest.mark.parametrize(
    "fix_data",
    [None, {"other_key": 1}],
    ids=["no-fix-data", "existing-fix-data"],
)
def test_mark_fix_errored(fix_data: dict[str, int] | None) -> None:
    violation = ViolationFactory.build(fix_data=fix_data)
    assert not is_fix_errored(violation)

    mark_fix_errored(violation)

    assert is_fix_errored(violation)
    assert violation.fix_data is not None
    if fix_data is not None:
        assert violation.fix_data["other_key"] == 1


def test_is_fix_errored_false_when_only_marked_rejected() -> None:
    # mark_fix_rejected() and mark_fix_errored() record distinct outcomes
    # (fix() ran but produced invalid syntax, vs. fix() itself raised) —
    # neither must be conflated with the other.
    violation = ViolationFactory.build(fix_data={"fix_rejected": True})
    assert not is_fix_errored(violation)


@pytest.mark.parametrize(
    "fix_data",
    [None, {"other_key": 1}],
    ids=["no-fix-data", "existing-fix-data"],
)
def test_mark_fix_failed(fix_data: dict[str, int] | None) -> None:
    violation = ViolationFactory.build(fix_data=fix_data)
    assert not is_fix_failed(violation)

    mark_fix_failed(violation)

    assert is_fix_failed(violation)
    assert violation.fix_data is not None
    if fix_data is not None:
        assert violation.fix_data["other_key"] == 1


def test_is_fix_failed_false_when_only_marked_errored() -> None:
    # mark_fix_errored() (fix() itself raised — a bug) and mark_fix_failed()
    # (fix() caught its own OSError and returned False — an environmental
    # failure) record distinct outcomes; neither must be conflated with the
    # other.
    violation = ViolationFactory.build(fix_data={"fix_errored": True})
    assert not is_fix_failed(violation)
