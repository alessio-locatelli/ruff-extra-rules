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


@pytest.mark.parametrize(
    ("source", "line", "fixable"),
    [
        ("result = func(\n    arg\n)  # Comment here\n", 3, True),
        # `))  # c` visits the scanner once per bracket token but is one violation.
        ("foo(\n    bar(x\n))  # dedup comment\n", 3, True),
    ],
    ids=["closing-paren", "dedups-multiple-closing-brackets"],
)
def test_check_detects_trailing_comment(source: str, line: int, *, fixable: bool) -> None:
    violations = MisplacedCommentCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 1
    assert violations[0].error_code == "STYLE-001"
    assert violations[0].line == line
    assert violations[0].fixable is fixable


@pytest.mark.parametrize(
    "source",
    [
        "result = func(\n    arg  # Comment inline on expression\n)\n",
        "result = func(\n    arg\n)  # Comment  # pytriage: ignore=STYLE-001\n",
        # `[1, 2][0]  # c`: tokens between the first `]` and the comment aren't COMMENT.
        "items = [1, 2][0]  # not a bracket-only line\n",
    ],
    ids=["correctly-placed", "inline-ignore", "tokens-between-bracket-and-comment"],
)
def test_check_returns_no_violations(source: str) -> None:
    assert MisplacedCommentCheck().check(Path("test.py"), ast.parse(source), source) == []


@pytest.mark.parametrize(
    ("source", "fixed_source"),
    [
        (
            "result = x(\n    arg\n)  # Short comment\n",
            "result = x(\n    arg  # Short comment\n)\n",
        ),
        (
            "result = some_function_with_very_long_name(\n"
            "    argument_one,\n"
            "    argument_two,\n"
            ")  # This comment is deliberately long enough to force preceding placement\n",
            "result = some_function_with_very_long_name(\n"
            "    argument_one,\n"
            "    # This comment is deliberately long enough to force preceding placement\n"
            "    argument_two,\n"
            ")\n",
        ),
    ],
    ids=["inline-placement", "preceding-placement"],
)
def test_fix_moves_trailing_comment(source: str, fixed_source: str, tmp_path: Path) -> None:
    test_file = tmp_path / "test.py"
    test_file.write_text(source)
    tree = ast.parse(source)
    check = MisplacedCommentCheck()
    violations = check.check(test_file, tree, source)

    assert check.fix(test_file, violations, source, tree) is True
    assert test_file.read_text() == fixed_source


@pytest.mark.parametrize(
    "source",
    [
        "result = func(arg)\n",
        # No violations reported, so the orchestrator would never call fix()
        # with anything to do here — but fix() must independently honor the
        # ignore comment too, since it re-scans the source rather than
        # trusting its input.
        "result = func(\n    arg\n)  # Comment  # pytriage: ignore=STYLE-001\n",
    ],
    ids=["nothing-to-fix", "ignore-comment-respected"],
)
def test_fix_is_noop_when_nothing_to_fix(source: str, tmp_path: Path) -> None:
    test_file = tmp_path / "test.py"
    test_file.write_text(source)

    assert MisplacedCommentCheck().fix(test_file, [], source, ast.parse(source)) is False
    assert test_file.read_text() == source


@pytest.mark.parametrize(
    "fixture_name",
    ["bracket_comments", "trailing_on_paren", "trailing_on_bracket", "trailing_on_brace"],
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

    assert MisplacedCommentCheck().check(Path("test.py"), ast.parse(source), source) == []


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
