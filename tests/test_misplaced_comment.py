from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._base import FixOutcome
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks.misplaced_comment import MisplacedCommentCheck

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "misplaced_comments"
MODULE_TRY_ASSIGN_SOURCE = (
    "try:\n"
    "    value = make_value(\n"
    "        argument\n"
    "    )  #: Description\n"
    "except ImportError:\n"
    "    value = make_placeholder()\n"
)
MODULE_TRY_ANN_ASSIGN_SOURCE = (
    "try:\n"
    "    value: object = make_value(\n"
    "        argument\n"
    "    )  #: Description\n"
    "except ImportError:\n"
    "    value = make_placeholder()\n"
)
ASYNC_FUNCTION_LOCAL_SOURCE = "async def make_value():\n    process(\n        argument\n    )  #: Description\n"
CLASS_ATTRIBUTE_SOURCE = (
    "class Serializer:\n"
    "    pipeline = SerializerPipeline(\n"
    '        name="optional",\n'
    "        is_binary=True,\n"
    "    )  #: Complete optional serializer\n"
)
INSTANCE_ATTRIBUTE_SOURCE = (
    "class Serializer:\n"
    "    def __init__(self) -> None:\n"
    "        self.pipeline = SerializerPipeline(\n"
    '            name="optional",\n'
    "            is_binary=True,\n"
    "        )  #: Complete optional serializer\n"
)


def _write_module_source(tmp_path: Path, source: str) -> Path:
    filepath = tmp_path / "module.py"
    filepath.write_bytes(source.encode())
    return filepath


def test_check_id_and_error_code() -> None:
    check = MisplacedCommentCheck()
    assert check.check_id == "misplaced-comment"
    assert check.error_code == "TR7"


def test_prefilter_pattern_is_hash() -> None:
    assert MisplacedCommentCheck().get_prefilter_pattern() == ["#"]


@pytest.mark.parametrize(
    ("source", "line", "fixable"),
    [
        ("result = func(\n    arg\n)  # Comment here\n", 3, True),
        ("foo(\n    bar(x\n))  # dedup comment\n", 3, True),
    ],
    ids=["closing-paren", "dedups-multiple-closing-brackets"],
)
def test_check_detects_trailing_comment(source: str, line: int, *, fixable: bool) -> None:
    violations = MisplacedCommentCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 1
    assert violations[0].error_code == "TR7"
    assert violations[0].line == line
    assert violations[0].fixable is fixable


@pytest.mark.parametrize(
    "source",
    [
        "result = func(\n    arg  # Comment inline on expression\n)\n",
        "result = func(\n    arg\n)  # Comment  # pytriage: TR7\n",
        "items = [1, 2][0]  # not a bracket-only line\n",
        "# fmt: off\nresult = func(\n    arg\n)  # Comment here\n# fmt: on\n",
    ],
    ids=["correctly-placed", "inline-ignore", "tokens-between-bracket-and-comment", "fmt-off-suppressed"],
)
def test_check_returns_no_violations(source: str) -> None:
    assert MisplacedCommentCheck().check(Path("test.py"), ast.parse(source), source) == []


def test_check_records_a_pytriage_usage() -> None:
    source = "result = func(\n    arg\n)  # Comment  # pytriage: TR7\n"

    check_result = MisplacedCommentCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [(usage.check_id, usage.error_code, usage.line) for usage in check_result.suppression_usages] == [
        ("misplaced-comment", "TR7", 3)
    ]


@pytest.mark.parametrize(
    ("second_fragment", "expected_lines"),
    [
        ("second = func(\n    arg\n)  # pytriage: TR7\n", [3, 6]),
        ("# fmt: off\nsecond = func(\n    arg\n)  # comment\n# fmt: on\n", [3]),
    ],
    ids=["pytriage", "format-suppressed"],
)
def test_check_records_suppression_usage_for_each_reportable_candidate(
    second_fragment: str, expected_lines: list[int]
) -> None:
    source = f"first = func(\n    arg\n)  # pytriage: TR7\n{second_fragment}"

    check_result = MisplacedCommentCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [usage.line for usage in check_result.suppression_usages] == expected_lines


@pytest.mark.parametrize(
    ("source", "expected_source", "expected_exit_code"),
    [
        (
            "value = make_value(\n    argument\n)  #: Description\n",
            "value = make_value(\n    argument\n)  #: Description\n",
            0,
        ),
        (
            "value: object = make_value(\n    argument\n)  #: Description\n",
            "value: object = make_value(\n    argument\n)  #: Description\n",
            0,
        ),
        (
            MODULE_TRY_ASSIGN_SOURCE,
            MODULE_TRY_ASSIGN_SOURCE,
            0,
        ),
        (
            MODULE_TRY_ANN_ASSIGN_SOURCE,
            MODULE_TRY_ANN_ASSIGN_SOURCE,
            0,
        ),
        (
            "def make_value():\n    process(\n        argument\n    )  #: Description\n",
            "def make_value():\n    process(\n        argument\n    )  #: Description\n",
            0,
        ),
        (
            ASYNC_FUNCTION_LOCAL_SOURCE,
            ASYNC_FUNCTION_LOCAL_SOURCE,
            0,
        ),
        (
            CLASS_ATTRIBUTE_SOURCE,
            CLASS_ATTRIBUTE_SOURCE,
            0,
        ),
        (
            INSTANCE_ATTRIBUTE_SOURCE,
            INSTANCE_ATTRIBUTE_SOURCE,
            0,
        ),
    ],
    ids=[
        "module-level-assignment",
        "module-level-annotated-assignment",
        "module-level-try-assignment",
        "module-level-try-annotated-assignment",
        "function-local-marker",
        "async-function-local-marker",
        "class-attribute",
        "instance-attribute",
    ],
)
def test_cli_fix_handles_sphinx_attribute_comments(
    source: str,
    expected_source: str,
    expected_exit_code: int,
    tmp_path: Path,
) -> None:
    filepath = _write_module_source(tmp_path, source)

    assert main(["--select", "misplaced-comment", "--fix", str(filepath)]) == expected_exit_code
    assert filepath.read_bytes() == expected_source.encode()


@pytest.mark.parametrize(
    ("source", "fixed_source"),
    [
        (
            "result = x(\n    arg\n)  # Short comment\n",
            "result = x(\n    arg  # Short comment\n)\n",
        ),
        (
            (
                "result = some_function_with_very_long_name(\n"
                "    argument_one,\n"
                "    argument_two,\n"
                ")  # This comment is deliberately long enough to force preceding placement\n"
            ),
            (
                "result = some_function_with_very_long_name(\n"
                "    argument_one,\n"
                "    # This comment is deliberately long enough to force preceding placement\n"
                "    argument_two,\n"
                ")\n"
            ),
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

    assert FixOutcome.APPLIED in check.fix(test_file, violations, source, tree).outcomes
    assert test_file.read_text() == fixed_source


@pytest.mark.parametrize(
    "source",
    [
        "result = func(arg)\n",
        "result = func(\n    arg\n)  # Comment  # pytriage: TR7\n",
        "# fmt: off\nresult = func(\n    arg\n)  # Comment here\n# fmt: on\n",
    ],
    ids=["nothing-to-fix", "ignore-comment-respected", "fmt-off-respected"],
)
def test_fix_is_noop_when_nothing_to_fix(source: str, tmp_path: Path) -> None:
    test_file = tmp_path / "test.py"
    test_file.write_text(source)

    assert FixOutcome.APPLIED not in MisplacedCommentCheck().fix(test_file, [], source, ast.parse(source)).outcomes
    assert test_file.read_text() == source


@pytest.mark.parametrize(
    ("source", "fixed_source"),
    [
        (
            "foo(\r\n    bar,\r\n)  # comment\r\n",
            "foo(\r\n    bar,  # comment\r\n)\r\n",
        ),
        (
            (
                "result = some_function_with_very_long_name(\r\n"
                "    argument_one,\r\n"
                "    argument_two,\r\n"
                ")  # This comment is deliberately long enough to force preceding placement\r\n"
            ),
            (
                "result = some_function_with_very_long_name(\r\n"
                "    argument_one,\r\n"
                "    # This comment is deliberately long enough to force preceding placement\r\n"
                "    argument_two,\r\n"
                ")\r\n"
            ),
        ),
    ],
    ids=["inline-placement", "preceding-placement"],
)
def test_fix_preserves_crlf_on_touched_lines(source: str, fixed_source: str, tmp_path: Path) -> None:
    test_file = tmp_path / "test.py"
    test_file.write_bytes(source.encode())
    tree = ast.parse(source)
    check = MisplacedCommentCheck()
    violations = check.check(test_file, tree, source)

    assert FixOutcome.APPLIED in check.fix(test_file, violations, source, tree).outcomes
    assert test_file.read_bytes() == fixed_source.encode()


def test_check_and_fix_detect_comment_on_cr_only_source(tmp_path: Path) -> None:
    source = "foo(\r    bar,\r)  # comment\r"
    test_file = tmp_path / "test.py"
    test_file.write_bytes(source.encode())
    tree = ast.parse(source)
    check = MisplacedCommentCheck()
    violations = check.check(test_file, tree, source)

    assert len(violations) == 1
    assert FixOutcome.APPLIED in check.fix(test_file, violations, source, tree).outcomes
    assert test_file.read_bytes() == b"foo(\r    bar,  # comment\r)\r"


def test_fix_write_failure_reports_failed_outcome(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = "result = func(\n    arg\n)  # Comment here\n"

    filepath = tmp_path / "missing_dir" / "test.py"

    tree = ast.parse(source)
    check = MisplacedCommentCheck()
    violations = check.check(filepath, tree, source)

    with caplog.at_level("DEBUG"):
        fix_result = check.fix(filepath, violations, source, tree)
    assert FixOutcome.APPLIED not in fix_result.outcomes
    assert fix_result.outcomes == (FixOutcome.FAILED,) * len(violations)
    assert all(record.levelname == "DEBUG" for record in caplog.records)


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

    assert FixOutcome.APPLIED in check.fix(test_file, violations, source, tree).outcomes
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
    assert FixOutcome.APPLIED not in check.fix(test_file, violations, source, tree).outcomes
    assert test_file.read_text() == good_fixture.read_text()
