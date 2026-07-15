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


def test_fix_collapses_header_blank_lines(tmp_path: Path) -> None:
    """Autofix on a bad fixture should match its known-good counterpart."""
    bad_source = (FIXTURES_DIR / "bad" / "header_spacing.py").read_text()
    good_source = (FIXTURES_DIR / "good" / "header_spacing.py").read_text()

    test_file = tmp_path / "module.py"
    test_file.write_text(bad_source)

    tree = ast.parse(bad_source)
    check = ExcessiveBlankLinesCheck()
    violations = check.check(test_file, tree, bad_source)
    assert check.fix(test_file, violations, bad_source, tree)

    assert test_file.read_text() == good_source
