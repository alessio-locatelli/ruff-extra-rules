from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks._base import FixOutcome
from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
    _can_safely_inline,
    _cleanup_blank_lines_around_removals,
    apply_fixes,
)
from tests.factories import ViolationFactory

if TYPE_CHECKING:
    from collections.abc import Callable

    from pre_commit_hooks.ast_checks._base import Violation

    WriteSourceFn = Callable[[str], Path]
    CheckedFn = Callable[[str], tuple[Path, ast.Module, list[Violation]]]


@pytest.fixture
def check() -> RedundantAssignmentCheck:
    return RedundantAssignmentCheck()


@pytest.fixture
def write_source(tmp_path: Path) -> WriteSourceFn:
    def _write(source: str) -> Path:
        filepath = tmp_path / "source.py"
        filepath.write_text(source)
        return filepath

    return _write


@pytest.fixture
def checked(write_source: WriteSourceFn, check: RedundantAssignmentCheck) -> CheckedFn:
    def _checked(source: str) -> tuple[Path, ast.Module, list[Violation]]:
        filepath = write_source(source)
        tree = ast.parse(source)
        violations = check.check(filepath, tree, source)
        return filepath, tree, violations

    return _checked


def test_fix_method_with_fixable_violations(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    source = """def func_scope():
    x = "foo"
    func(x=x)
"""
    filepath, tree, violations = checked(source)

    assert len(violations) >= 1
    assert any(v.fixable for v in violations)

    assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

    fixed_content = filepath.read_text()
    assert "x = " not in fixed_content
    assert 'func(x="foo")' in fixed_content


def test_fix_two_assignments_used_on_the_same_line(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    source = """def f():
    x = 1
    y = 22
    return y + x
"""
    filepath, tree, violations = checked(source)

    assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

    fixed_content = filepath.read_text()
    assert "x = " not in fixed_content
    assert "y = " not in fixed_content
    assert "return 22 + 1" in fixed_content


def test_fix_chained_assignment_where_use_line_is_another_assign_line(
    checked: CheckedFn, check: RedundantAssignmentCheck
) -> None:
    source = """def f():
    x = 1
    y = x
    return y
"""
    filepath, tree, violations = checked(source)

    assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

    fixed_content = filepath.read_text()
    assert "y = " not in fixed_content
    assert "return x" in fixed_content


def test_fix_write_failure_reports_failed_outcome(
    tmp_path: Path, check: RedundantAssignmentCheck, caplog: pytest.LogCaptureFixture
) -> None:
    source = """def func_scope():
    x = "foo"
    func(x=x)
"""
    filepath = tmp_path / "missing_dir" / "source.py"

    tree = ast.parse(source)
    violations = check.check(filepath, tree, source)

    with caplog.at_level("DEBUG"):
        fix_result = check.fix(filepath, violations, source, tree)
    assert FixOutcome.APPLIED not in fix_result.outcomes
    assert fix_result.outcomes == (FixOutcome.FAILED,) * len(violations)
    assert all(record.levelname == "DEBUG" for record in caplog.records)


@pytest.mark.parametrize(
    ("source", "fix_data"),
    [
        ("x = 1\nprint(x)\n", None),
        ("x = 1\nprint(x)\n", {"other_key": "value"}),
        (
            "x = 1\nprint(x)\n",
            {
                "pattern": "IMMEDIATE_SINGLE_USE",
                "assign_line": 100,
                "var_name": "x",
                "rhs_source": "1",
                "use_line": 2,
                "use_col": 6,
            },
        ),
        (
            "x = 1\nprint(x)\n",
            {
                "pattern": "IMMEDIATE_SINGLE_USE",
                "assign_line": 1,
                "var_name": "x",
                "rhs_source": "1",
                "use_line": 100,
                "use_col": 6,
            },
        ),
        (
            "x = 1\nprint(x)\nprint(x)\n",
            {
                "pattern": "SINGLE_USE",
                "assign_line": 1,
                "var_name": "x",
                "rhs_source": "1",
                "use_line": None,
                "use_col": None,
            },
        ),
        (
            "x = " + "a" * 40 + "\nresult = some_long_function_name(x, param1, param2)\n",
            {
                "pattern": "IMMEDIATE_SINGLE_USE",
                "assign_line": 1,
                "var_name": "x",
                "rhs_source": "a" * 40,
                "use_line": 2,
                "use_col": 33,
            },
        ),
    ],
    ids=[
        "missing-fix-data",
        "fix-data-missing-use-line",
        "invalid-assignment-line",
        "invalid-usage-line",
        "multiple-uses-unset-position",
        "unsafe-line-length",
    ],
)
def test_autofix_declines_fix_for_invalid_or_unsafe_fix_data(
    write_source: WriteSourceFn,
    check: RedundantAssignmentCheck,
    source: str,
    fix_data: dict[str, object] | None,
) -> None:
    violation = ViolationFactory.build(
        check_id="redundant-assignment", error_code="TR5", fixable=True, fix_data=fix_data
    )
    assert FixOutcome.APPLIED not in check.fix(write_source(source), [violation], source, ast.parse(source)).outcomes


def test_autofix_skips_multiline_rhs() -> None:
    assert _can_safely_inline("result", "func(\n    arg\n)", 0, ["result = func(x)\n"]) is False


def test_autofix_skips_line_length_violation() -> None:
    source_lines = ["x = " + "a" * 80 + "\n"]
    assert _can_safely_inline("x", "a" * 20, 0, source_lines) is False


@pytest.mark.parametrize("line_index", [-1, 10], ids=["negative", "out-of-bounds"])
def test_autofix_skips_invalid_line_indices(line_index: int) -> None:
    assert _can_safely_inline("x", "value", line_index, ["line1\n", "line2\n"]) is False


def test_fix_method_with_no_fixable_violations() -> None:
    source = """
x = "foo"
func(x=x)
"""
    violation = ViolationFactory.build(check_id="redundant-assignment", error_code="TR5", fixable=False, fix_data=None)
    assert FixOutcome.APPLIED not in apply_fixes(Path("test.py"), [violation], source).outcomes


@pytest.mark.parametrize(
    ("source", "removed", "kept"),
    [
        (
            """def f():
    y = 42
    result = y + 10
    return result
""",
            "y = 42",
            "result = 42 + 10",
        ),
        (
            """def func(days_with_routes_in_a_row: int) -> int:
    return days_with_routes_in_a_row


def caller() -> int:
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row=days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row = 42",
            "func(days_with_routes_in_a_row=42)",
        ),
        (
            """def func(days_with_routes_in_a_row: int) -> int:
    return days_with_routes_in_a_row


def caller() -> int:
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row = 42",
            "func(42)",
        ),
        (
            """def f():
    v = obj.attr
    use(v)
""",
            "v = obj.attr",
            "use(obj.attr)",
        ),
        (
            """def f():
    n = 5
    return f"Total: {n}"
""",
            "n = ",
            'return f"Total: {5}"',
        ),
        (
            """def f(obj):
    x = obj
    return f"value: {x}"
""",
            "x = obj",
            'return f"value: {obj}"',
        ),
        (
            """def process():
    data = calc()
    return "café", data
""",
            "data",
            'return "café", calc()',
        ),
    ],
    ids=[
        "simple-constant",
        "keyword-argument-echo",
        "positional-argument-echo",
        "simple-attribute",
        "fstring-field-non-string-rhs",
        "fstring-field-name-rhs",
        "non-ascii-text-on-use-line",
    ],
)
def test_autofix_inlines_simple_redundant_assignment(
    checked: CheckedFn, check: RedundantAssignmentCheck, source: str, removed: str, kept: str
) -> None:
    filepath, tree, violations = checked(source)

    assert any(v.fixable for v in violations)
    assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

    fixed_content = filepath.read_text()
    assert removed not in fixed_content
    assert kept in fixed_content


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            """def f():
    x = 5
    return max(x, 10)
""",
            "return max(5, 10)",
        ),
        (
            """
def func(index):
    x = 5
    return max(x, index)
""",
            "max(5, index)",
        ),
    ],
    ids=["against-max", "against-index"],
)
def test_autofix_respects_word_boundaries(
    checked: CheckedFn, check: RedundantAssignmentCheck, source: str, expected: str
) -> None:
    filepath, tree, violations = checked(source)

    assert any(v.fixable for v in violations)
    assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes
    assert expected in filepath.read_text()


def test_autofix_respects_line_length(checked: CheckedFn) -> None:
    source = """
def func():
    x = "hello world"
    print("first", "second", "third", "fourth", "fifth", "sixth", "seventh", x)
"""
    _filepath, _tree, violations = checked(source)

    assert violations
    assert all(not v.fixable for v in violations)


def test_zero_arg_call_immediate_single_use_is_fixable(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    source = """def test_something():
    check = MeaninglessVarsCheck()
    violations = check.check(Path("test.py"), tree, source)
"""
    filepath, tree, violations = checked(source)

    check_violations = [v for v in violations if "'check'" in v.message]
    assert check_violations
    assert all(v.fixable for v in check_violations)

    assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

    fixed_content = filepath.read_text()
    assert "check = " not in fixed_content
    assert "MeaninglessVarsCheck().check(" in fixed_content


def test_augmented_assignment_use_not_flagged_for_zero_arg_call(
    checked: CheckedFn, check: RedundantAssignmentCheck
) -> None:
    source = """def f():
    x = Box()
    x += 1
"""
    filepath, tree, violations = checked(source)

    assert all("'x'" not in v.message for v in violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


@pytest.mark.parametrize(
    ("source", "message_filter"),
    [
        (
            """def f():
    x = make()
    d = {"a": side_effect(), x: 1}
""",
            "'x'",
        ),
        (
            """def f():
    x = make()
    sink(a + b, x)
""",
            "'x'",
        ),
        (
            """def f():
    x = make()
    sink(x if flag else 0)
""",
            "'x'",
        ),
        (
            """def f():
    x = make()
    sink(flag and x)
""",
            "'x'",
        ),
    ],
    ids=[
        "dict-value-after-earlier-pair",
        "after-operator-sibling",
        "ternary-branch",
        "short-circuited-boolop",
    ],
)
def test_zero_arg_call_use_not_fixable(
    checked: CheckedFn, check: RedundantAssignmentCheck, source: str, message_filter: str
) -> None:
    filepath, tree, violations = checked(source)

    matching = [v for v in violations if message_filter in v.message]
    assert matching
    assert all(not v.fixable for v in matching)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


@pytest.mark.parametrize(
    "source",
    [
        """def f(r):
    value = make()
    for _ in r:
        sink(value)
""",
        """def f(r):
    value = make()
    other()
    for _ in r:
        sink(value)
""",
    ],
    ids=["immediate-before-loop", "intervening-statement-before-loop"],
)
def test_single_use_call_in_loop_body_not_reported(checked: CheckedFn, source: str) -> None:
    _filepath, _tree, violations = checked(source)
    assert all("'value'" not in v.message for v in violations)


def test_call_rhs_across_await_in_same_statement_not_reported(checked: CheckedFn) -> None:
    source = """async def f(future):
    x = make()
    return sink(await future, x)
"""
    _filepath, _tree, violations = checked(source)
    assert all("'x'" not in v.message for v in violations)


def test_autofix_preserves_blank_lines_across_file(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    source = """class FirstClass:
    def method_one(self):
        pass


class SecondClass:
    def method_two(self):
        pass


def function_with_redundant_var():
    x = 42
    return x


def another_function():
    pass


class ThirdClass:
    def method_three(self):
        pass
"""
    filepath, tree, violations = checked(source)

    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)
    fixed_content = filepath.read_text()

    assert "class FirstClass:\n    def method_one(self):\n        pass\n\n\nclass SecondClass:" in fixed_content
    assert (
        "class SecondClass:\n    def method_two(self):\n        pass\n\n\ndef function_with_redundant_var():"
        in fixed_content
    )
    assert "def another_function():\n    pass\n\n\nclass ThirdClass:" in fixed_content

    ast.parse(fixed_content)


def test_autofix_cleans_up_excessive_blank_lines(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    source = """def function_with_redundant():


    x = 42


    return x
"""
    filepath, tree, violations = checked(source)

    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)
    fixed_content = filepath.read_text()

    lines = fixed_content.split("\n")
    def_index = next(i for i, line in enumerate(lines) if "def function_with_redundant" in line)
    return_index = next(i for i in range(def_index, len(lines)) if "return" in lines[i])
    blanks_before_return = 0
    j = return_index - 1
    while j >= 0 and lines[j].strip() == "":
        blanks_before_return += 1
        j -= 1

    assert blanks_before_return <= 2

    ast.parse(fixed_content)


def test_cleanup_blank_lines_only_excess_below() -> None:
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {0})
    assert lines[2] == ""
    assert lines[3] == "code\n"


def test_cleanup_blank_lines_only_excess_above() -> None:
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {2})
    assert lines[0] == ""
    assert lines[3] == "code\n"


def test_fix_preserves_trailing_comment_on_string_ending_in_escaped_backslash(checked: CheckedFn) -> None:
    source = 'def get_sep() -> str:\n    sep = "\\\\"  # Windows path separator\n    return sep\n'
    filepath, _tree, violations = checked(source)

    assert violations == []
    assert filepath.read_text() == source


def test_check_reports_assignment_after_multiline_string_with_trailing_comment(checked: CheckedFn) -> None:
    source = 'def f():\n    x = """\nmulti\nline\n"""  # trailing comment\n    y = 5\n    return x, y\n'
    _filepath, _tree, violations = checked(source)

    assert any("'y'" in v.message for v in violations)


def test_autofix_splices_string_literal_into_fstring_field(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    source = """def f():
    org = "requests-cache"
    return f"https://github.com/{org}/requests-cache"
"""
    filepath, tree, violations = checked(source)

    assert any(v.fixable for v in violations)
    assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

    fixed_content = filepath.read_text()
    assert "org = " not in fixed_content
    assert 'return f"https://github.com/requests-cache/requests-cache"' in fixed_content

    ast.parse(fixed_content)


@pytest.mark.parametrize(
    ("source", "message_filter"),
    [
        (
            """def f():
    name = "O'Brien"
    return f"Hello {name}!"
""",
            "'name'",
        ),
        (
            """def f():
    reset = "\\x1b[0m"
    return f"{reset}"
""",
            "'reset'",
        ),
        (
            'def f():\n    label = "\\x00"\n    return f"<{label}>"\n',
            "'label'",
        ),
        (
            'def f():\n    label = "\\ud800"\n    return f"<{label}>"\n',
            "'label'",
        ),
        (
            """def f():
    org = "requests-cache"
    return f"{org!r}"
""",
            "'org'",
        ),
        (
            """def f():
    org = "requests-cache"
    return f"{org.upper()}"
""",
            "'org'",
        ),
    ],
    ids=[
        "unsafe-quote-character",
        "control-character",
        "nul-byte",
        "unpaired-surrogate",
        "conversion",
        "nested-expression",
    ],
)
def test_autofix_declines_fstring_splice(
    checked: CheckedFn, check: RedundantAssignmentCheck, source: str, message_filter: str
) -> None:
    filepath, tree, violations = checked(source)

    matching = [v for v in violations if message_filter in v.message]
    assert matching
    assert all(not v.fixable for v in matching)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_autofix_declines_fstring_splice_when_value_unencodable_in_declared_encoding(
    tmp_path: Path, check: RedundantAssignmentCheck
) -> None:
    source = 'def f():\n    label = "\\xe9"\n    return f"<{label}>"\n'
    filepath = tmp_path / "source.py"
    filepath.write_bytes(source.encode("ascii"))

    tree = ast.parse(source)
    violations = check.check(filepath, tree, source)

    label_violations = [v for v in violations if "'label'" in v.message]
    assert label_violations
    assert all(v.fixable for v in label_violations)

    check.fix(filepath, violations, source, tree, encoding="ascii")
    assert filepath.read_bytes() == source.encode("ascii")


def test_autofix_fstring_splice_declines_when_earlier_fix_lengthens_line(
    checked: CheckedFn, check: RedundantAssignmentCheck
) -> None:
    source = 'def f():\n    o = "cccc"\n    p = "xxxx"\n    return "' + "a" * 48 + '" + f"{o}" + p\n'
    filepath, tree, violations = checked(source)

    o_violations = [v for v in violations if "'o'" in v.message]
    p_violations = [v for v in violations if "'p'" in v.message]
    assert o_violations
    assert all(v.fixable for v in o_violations)
    assert p_violations
    assert all(v.fixable for v in p_violations)

    check.fix(filepath, violations, source, tree)

    fixed_content = filepath.read_text()
    assert '    o = "cccc"' in fixed_content
    assert "{o}" in fixed_content
    assert 'p = "xxxx"' not in fixed_content
