"""Tests for misplaced-comment (STYLE-001)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks.misplaced_comment import MisplacedCommentCheck

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "misplaced_comments"


def test_check_id_and_error_code() -> None:
    check = MisplacedCommentCheck()
    assert check.check_id == "misplaced-comment"
    assert check.error_code == "STYLE-001"


def test_prefilter_pattern_is_hash() -> None:
    assert MisplacedCommentCheck().get_prefilter_pattern() == ["#"]


def test_detects_trailing_comment_on_closing_paren() -> None:
    source = "result = func(\n    arg\n)  # Comment here\n"
    tree = ast.parse(source)

    violations = MisplacedCommentCheck().check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert violations[0].error_code == "STYLE-001"
    assert violations[0].line == 3
    assert violations[0].fixable is True


def test_no_violation_for_correct_code() -> None:
    source = "result = func(\n    arg  # Comment inline on expression\n)\n"
    tree = ast.parse(source)

    violations = MisplacedCommentCheck().check(Path("test.py"), tree, source)

    assert violations == []


def test_fixes_trailing_comment_inline_placement(tmp_path: Path) -> None:
    test_file = tmp_path / "test.py"
    source = "result = x(\n    arg\n)  # Short comment\n"
    test_file.write_text(source)
    tree = ast.parse(source)
    check = MisplacedCommentCheck()
    violations = check.check(test_file, tree, source)

    assert check.fix(test_file, violations, source, tree) is True
    assert test_file.read_text() == "result = x(\n    arg  # Short comment\n)\n"


def test_fixes_trailing_comment_preceding_placement(tmp_path: Path) -> None:
    test_file = tmp_path / "test.py"
    comment = "# This comment is deliberately long enough to force preceding placement"
    source = (
        "result = some_function_with_very_long_name(\n"
        "    argument_one,\n"
        "    argument_two,\n"
        f")  {comment}\n"
    )
    test_file.write_text(source)
    tree = ast.parse(source)
    check = MisplacedCommentCheck()
    violations = check.check(test_file, tree, source)

    assert check.fix(test_file, violations, source, tree) is True
    assert test_file.read_text() == (
        "result = some_function_with_very_long_name(\n"
        "    argument_one,\n"
        f"    {comment}\n"
        "    argument_two,\n"
        ")\n"
    )


def test_check_returns_no_violations_when_nothing_to_fix(tmp_path: Path) -> None:
    test_file = tmp_path / "test.py"
    source = "result = func(arg)\n"
    test_file.write_text(source)
    tree = ast.parse(source)
    check = MisplacedCommentCheck()

    assert check.check(test_file, tree, source) == []
    assert check.fix(test_file, [], source, tree) is False
    assert test_file.read_text() == source


def test_inline_ignore_suppresses_violation() -> None:
    source = "result = func(\n    arg\n)  # Comment  # pytriage: ignore=STYLE-001\n"
    tree = ast.parse(source)

    violations = MisplacedCommentCheck().check(Path("test.py"), tree, source)

    assert violations == []


def test_inline_ignore_is_respected_by_fix(tmp_path: Path) -> None:
    test_file = tmp_path / "test.py"
    source = "result = func(\n    arg\n)  # Comment  # pytriage: ignore=STYLE-001\n"
    test_file.write_text(source)
    tree = ast.parse(source)
    check = MisplacedCommentCheck()

    # No violations reported, so the orchestrator would never call fix() with
    # anything to do here — but fix() must independently honor the ignore
    # comment too, since it re-scans the source rather than trusting violations.
    assert check.fix(test_file, [], source, tree) is False
    assert test_file.read_text() == source


def test_non_comment_tokens_between_bracket_and_comment_not_flagged() -> None:
    """`[1, 2][0]  # c`: tokens between the first `]` and the comment aren't COMMENT."""
    source = "items = [1, 2][0]  # not a bracket-only line\n"
    tree = ast.parse(source)

    assert MisplacedCommentCheck().check(Path("test.py"), tree, source) == []


def test_dedupes_multiple_closing_brackets_on_one_line() -> None:
    """`))  # c` visits the scanner once per bracket token but is one violation."""
    source = "foo(\n    bar(x\n))  # dedup comment\n"
    tree = ast.parse(source)

    violations = MisplacedCommentCheck().check(Path("test.py"), tree, source)

    assert len(violations) == 1


@pytest.mark.parametrize(
    "fixture_name",
    [
        "bracket_comments",
        "trailing_on_paren",
        "trailing_on_bracket",
        "trailing_on_brace",
    ],
    ids=["mixed-brackets", "paren", "bracket", "brace"],
)
def test_fixes_match_golden_fixtures(fixture_name: str, tmp_path: Path) -> None:
    bad_fixture = FIXTURES_DIR / "bad" / f"{fixture_name}.py"
    good_fixture = FIXTURES_DIR / "good" / f"{fixture_name}.py"

    test_file = tmp_path / "test.py"
    source = bad_fixture.read_text()
    test_file.write_text(source)
    tree = ast.parse(source)
    check = MisplacedCommentCheck()
    violations = check.check(test_file, tree, source)

    assert check.fix(test_file, violations, source, tree) is True
    assert test_file.read_text() == good_fixture.read_text()


@pytest.mark.parametrize(
    "fixture_name",
    ["inline_comment", "preceding_comment"],
    ids=["inline", "preceding"],
)
def test_correctly_placed_comments_not_flagged(fixture_name: str) -> None:
    source = (FIXTURES_DIR / "good" / f"{fixture_name}.py").read_text()
    tree = ast.parse(source)

    assert MisplacedCommentCheck().check(Path("test.py"), tree, source) == []


def test_preserves_linter_pragma_comments(tmp_path: Path) -> None:
    bad_fixture = FIXTURES_DIR / "bad" / "ignore_comments.py"
    good_fixture = FIXTURES_DIR / "good" / "ignore_comments.py"

    test_file = tmp_path / "test.py"
    source = bad_fixture.read_text()
    test_file.write_text(source)
    tree = ast.parse(source)
    check = MisplacedCommentCheck()
    violations = check.check(test_file, tree, source)

    assert violations == []
    assert check.fix(test_file, violations, source, tree) is False
    assert test_file.read_text() == good_fixture.read_text()
