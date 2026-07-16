"""Tests for excessive_blank_lines hook (TRI002)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks.excessive_blank_lines import ExcessiveBlankLinesCheck

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "excessive_blank_lines"


def _check(source: str) -> list[str]:
    tree = ast.parse(source)
    violations = ExcessiveBlankLinesCheck().check(Path("test.py"), tree, source)
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
    source = "\n\n\nimport os\n"
    assert _check(source) == ["Excessive blank lines (3) should be collapsed to 1"]


def test_fix_file_content_empty_source_returns_unchanged() -> None:
    from pre_commit_hooks.ast_checks.excessive_blank_lines import fix_file_content

    assert fix_file_content("", ast.parse("")) == ""


def test_fix_with_no_violations_returns_false(tmp_path: Path) -> None:
    source = "x = 1\n"
    tree = ast.parse(source)
    test_file = tmp_path / "module.py"
    test_file.write_text(source)

    check = ExcessiveBlankLinesCheck()
    assert check.fix(test_file, [], source, tree) is False


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
