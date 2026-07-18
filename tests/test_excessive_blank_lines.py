"""Tests for excessive_blank_lines hook (TRI002)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._base import Violation
from pre_commit_hooks.ast_checks.excessive_blank_lines import ExcessiveBlankLinesCheck

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "excessive_blank_lines"


def _check(source: str) -> list[str]:
    violations = ExcessiveBlankLinesCheck().check(
        Path("test.py"), ast.parse(source), source
    )
    return [v.message for v in violations]


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "bad").glob("*.py")),
    ids=lambda p: p.name,
)
def test_bad_fixtures_are_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text())


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "good").glob("*.py")),
    ids=lambda p: p.name,
)
def test_good_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text()) == []


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "ignore").glob("*.py")),
    ids=lambda p: p.name,
)
def test_ignore_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text()) == []


def test_raw_prefixed_docstring_header_is_detected() -> None:
    """Regression: raw/byte-prefixed docstrings must be detected via the AST.

    A raw-text quote-prefix scan misses the r/b prefix entirely and would
    treat the whole file as one giant docstring.
    """
    source = 'r"""Raw docstring."""\n\n\n\nimport os\n'
    assert _check(source)


def test_comment_only_file_has_no_violations() -> None:
    """A file with only comments (no code at all) has no first code line, so
    the header-end scan runs off the end of the file.
    """
    source = "# just a comment\n\n# another comment\n"
    assert _check(source) == []


def test_empty_file_has_no_violations() -> None:
    assert _check("") == []


def test_leading_blank_lines_before_first_code_with_no_header() -> None:
    """A file with no docstring/comment header, just leading blank lines
    before the first code line, has no non-blank header line to find, so the
    whole leading run is treated as the gap before the first code line.
    """
    assert _check("\n\n\nimport os\n") == [
        "Excessive blank lines (3) should be collapsed to 1. Add "
        "'# pytriage: ignore=TRI002' to the line following the blank run "
        "to suppress."
    ]


def test_inline_ignore_suppresses_violation() -> None:
    """The blank run's own line is blank, so the ignore comment goes on the
    first code line after it instead."""
    source = '"""Docstring."""\n\n\n\ndef foo():  # pytriage: ignore=TRI002\n    pass\n'
    assert _check(source) == []


def test_inline_ignore_is_respected_by_fix(tmp_path: Path) -> None:
    source = '"""Docstring."""\n\n\n\ndef foo():  # pytriage: ignore=TRI002\n    pass\n'
    test_file = tmp_path / "module.py"
    test_file.write_text(source)
    check = ExcessiveBlankLinesCheck()

    # A stale violation (as if collected before the ignore comment was
    # added) must not cause fix() to collapse the blank run anyway — fix()
    # independently re-checks ignored_lines rather than trusting its input.
    stale_violation = Violation(
        check_id=check.check_id,
        error_code=check.error_code,
        line=2,
        col=0,
        message="stale",
        fixable=True,
    )
    assert check.fix(test_file, [stale_violation], source, ast.parse(source)) is False
    assert test_file.read_text() == source


def test_fix_with_stale_violation_and_no_current_violation_returns_false(
    tmp_path: Path,
) -> None:
    """A caller-supplied violations list can be stale (e.g. a previous fix in
    the same run already collapsed the blank run) — fix() must recheck the
    current source rather than trusting it.
    """
    source = '"""Docstring."""\n\ndef foo():\n    pass\n'
    test_file = tmp_path / "module.py"
    test_file.write_text(source)
    check = ExcessiveBlankLinesCheck()

    stale_violation = Violation(
        check_id=check.check_id,
        error_code=check.error_code,
        line=2,
        col=0,
        message="stale",
        fixable=True,
    )
    assert check.fix(test_file, [stale_violation], source, ast.parse(source)) is False
    assert test_file.read_text() == source


def test_fix_file_content_empty_source_returns_unchanged() -> None:
    from pre_commit_hooks.ast_checks.excessive_blank_lines import fix_file_content

    assert fix_file_content("", ast.parse("")) == ""


def test_fix_with_no_violations_returns_false(tmp_path: Path) -> None:
    source = "x = 1\n"
    test_file = tmp_path / "module.py"
    test_file.write_text(source)

    check = ExcessiveBlankLinesCheck()
    assert check.fix(test_file, [], source, ast.parse(source)) is False


def test_fix_leading_blank_lines_before_first_code_with_no_header(
    tmp_path: Path,
) -> None:
    source = "\n\n\nimport os\n"
    tree = ast.parse(source)
    test_file = tmp_path / "module.py"
    test_file.write_text(source)

    check = ExcessiveBlankLinesCheck()
    violations = check.check(test_file, tree, source)
    assert check.fix(test_file, violations, source, tree)
    assert test_file.read_text() == "\nimport os\n"


def test_fix_write_failure_returns_false(tmp_path: Path) -> None:
    bad_source = (FIXTURES_DIR / "bad" / "header_spacing.py").read_text()

    # Point at a path inside a directory that doesn't exist so write_text()
    # raises OSError.
    test_file = tmp_path / "missing_dir" / "module.py"

    tree = ast.parse(bad_source)
    check = ExcessiveBlankLinesCheck()
    violations = check.check(test_file, tree, bad_source)
    assert check.fix(test_file, violations, bad_source, tree) is False


def test_fix_collapses_header_blank_lines(tmp_path: Path) -> None:
    bad_source = (FIXTURES_DIR / "bad" / "header_spacing.py").read_text()
    good_source = (FIXTURES_DIR / "good" / "header_spacing.py").read_text()

    test_file = tmp_path / "module.py"
    test_file.write_text(bad_source)

    tree = ast.parse(bad_source)
    check = ExcessiveBlankLinesCheck()
    violations = check.check(test_file, tree, bad_source)
    assert check.fix(test_file, violations, bad_source, tree)

    assert test_file.read_text() == good_source
