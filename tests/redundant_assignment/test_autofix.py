from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
    _can_safely_inline,
    _cleanup_blank_lines_around_removals,
    apply_fixes,
)
from tests.factories import ViolationFactory

# ---------------------------------------------------------------------------
# fix() / apply_fixes(): file-mutation regression tests
# ---------------------------------------------------------------------------


def test_fix_method_with_fixable_violations(tmp_path: Path) -> None:
    source = """def func_scope():
    x = "foo"
    func(x=x)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert len(violations) >= 1
    assert any(v.fixable for v in violations)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()

    # The assignment should be removed and the usage inlined.
    assert "x = " not in fixed_content
    assert 'func(x="foo")' in fixed_content


def test_fix_two_assignments_used_on_the_same_line(tmp_path: Path) -> None:
    # Regression: two independently-fixable assignments whose single uses
    # land on the same line must both be inlined, even when the
    # replacement text is a different length than the variable it
    # replaces (which shifts the column of whichever use is processed
    # second).
    source = """def f():
    x = 1
    y = 22
    return y + x
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "x = " not in fixed_content
    assert "y = " not in fixed_content
    assert "return 22 + 1" in fixed_content


def test_fix_chained_assignment_where_use_line_is_another_assign_line(
    tmp_path: Path,
) -> None:
    # Regression: x's only use is on the same line as y's assignment (`y
    # = x`). Applying y's fix first blanks that whole line, so x's own fix
    # must skip cleanly instead of crashing when its use is gone.
    source = """def f():
    x = 1
    y = x
    return y
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "y = " not in fixed_content
    assert "return x" in fixed_content


def test_autofix_skips_violation_without_fix_data(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violation = ViolationFactory.build(
        check_id="redundant-assignment", error_code="TRI005", fixable=True, fix_data=None
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_skips_violation_with_invalid_fix_data(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    # fix_data is missing 'use_line'.
    violation = ViolationFactory.build(
        check_id="redundant-assignment", error_code="TRI005", fixable=True, fix_data={"other_key": "value"}
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_skips_multiline_rhs() -> None:
    # RHS with newline should not be inlined.
    source_lines = ["result = func(x)\n"]
    assert _can_safely_inline("result", "func(\n    arg\n)", 0, source_lines) is False


def test_autofix_skips_line_length_violation() -> None:
    # Current line is 80 chars, adding 20 more would exceed 88.
    source_lines = ["x = " + "a" * 80 + "\n"]
    assert _can_safely_inline("x", "a" * 20, 0, source_lines) is False


def test_autofix_skips_invalid_line_indices() -> None:
    source_lines = ["line1\n", "line2\n"]
    assert _can_safely_inline("x", "value", -1, source_lines) is False  # negative index
    assert _can_safely_inline("x", "value", 10, source_lines) is False  # out of bounds


def test_autofix_with_invalid_assignment_line(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violation = ViolationFactory.build(
        check_id="redundant-assignment",
        error_code="TRI005",
        fixable=True,
        fix_data={
            "pattern": "IMMEDIATE_SINGLE_USE",
            "assign_line": 100,  # Invalid line number
            "var_name": "x",
            "rhs_source": "1",
            "use_line": 2,
            "use_col": 6,
        },
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_with_invalid_usage_line(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violation = ViolationFactory.build(
        check_id="redundant-assignment",
        error_code="TRI005",
        fixable=True,
        fix_data={
            "pattern": "IMMEDIATE_SINGLE_USE",
            "assign_line": 1,
            "var_name": "x",
            "rhs_source": "1",
            "use_line": 100,  # Invalid line number
            "use_col": 6,
        },
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_with_multiple_uses(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    # RedundantAssignmentCheck.check() leaves use_line/use_col unset
    # whenever a lifecycle doesn't have exactly one use.
    violation = ViolationFactory.build(
        check_id="redundant-assignment",
        error_code="TRI005",
        fixable=True,
        fix_data={
            "pattern": "SINGLE_USE",
            "assign_line": 1,
            "var_name": "x",
            "rhs_source": "1",
            "use_line": None,
            "use_col": None,
        },
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_with_unsafe_inlining(tmp_path: Path) -> None:
    # Line is already 60 chars; adding a 40-char value would exceed 88.
    source = "x = " + "a" * 40 + "\nresult = some_long_function_name(x, param1, param2)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violation = ViolationFactory.build(
        check_id="redundant-assignment",
        error_code="TRI005",
        fixable=True,
        fix_data={
            "pattern": "IMMEDIATE_SINGLE_USE",
            "assign_line": 1,
            "var_name": "x",
            "rhs_source": "a" * 40,
            "use_line": 2,
            "use_col": 41,  # Position of 'x' in the usage line
        },
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_fix_method_with_no_fixable_violations() -> None:
    source = """
x = "foo"
func(x=x)
"""
    violation = ViolationFactory.build(
        check_id="redundant-assignment", error_code="TRI005", fixable=False, fix_data=None
    )
    assert apply_fixes(Path("test.py"), [violation], source) is False


def test_autofix_simple_constant(tmp_path: Path) -> None:
    source = """def f():
    y = 42
    result = y + 10
    return result
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations
    assert check.fix(filepath, fixable_violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "y = 42" not in fixed_content
    assert "result = 42 + 10" in fixed_content


def test_autofix_simple_attribute(tmp_path: Path) -> None:
    source = """def f():
    v = obj.attr
    use(v)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations
    assert check.fix(filepath, fixable_violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "v = obj.attr" not in fixed_content
    assert "use(obj.attr)" in fixed_content


def test_autofix_word_boundaries(tmp_path: Path) -> None:
    source = """def f():
    x = 5
    result = max(x, 10)
    return result
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations
    assert check.fix(filepath, fixable_violations, source, tree) is True

    fixed_content = filepath.read_text()
    # Should replace 'x' but not affect 'max'.
    assert "result = max(5, 10)" in fixed_content
    assert "max" in fixed_content


def test_autofix_handles_word_boundaries(tmp_path: Path) -> None:
    source = """
def func(index):
    x = 5
    return max(x, index)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)

    # Should only replace the standalone 'x', not 'max' or 'index'.
    assert "max(5, index)" in filepath.read_text()


def test_autofix_respects_line_length(tmp_path: Path) -> None:
    source = """
def func():
    some_result = "data"
    return some_result
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violations = RedundantAssignmentCheck().check(filepath, ast.parse(source), source)

    # Long variable names (>10 chars) are excluded from autofix as a
    # conservative proxy for lines that would grow too long when inlined.
    assert violations
    assert all(not v.fixable for v in violations)


def test_zero_arg_call_immediate_single_use_is_fixable(tmp_path: Path) -> None:
    # Regression test (issue #22): IMMEDIATE_SINGLE_USE never allowed a
    # Call RHS, even trivial zero-arg ones, so idiomatic test code like
    # `check = ForbidVarsCheck(); check.check(...)` was never auto-fixed.
    # A zero-arg call has no operands whose evaluation order inlining
    # could disturb, so it's safe to allow as a narrow carve-out.
    source = """def test_something():
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    check_violations = [v for v in violations if "'check'" in v.message]
    assert check_violations
    assert all(v.fixable for v in check_violations)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "check = " not in fixed_content
    assert "ForbidVarsCheck().check(" in fixed_content


def test_augmented_assignment_use_not_flagged_for_zero_arg_call(
    tmp_path: Path,
) -> None:
    # Regression test: the issue #22 zero-arg-call carve-out for
    # IMMEDIATE_SINGLE_USE must not make `x = Box(); x += 1` fixable —
    # inlining would produce invalid syntax (`Box() += 1`).
    source = """def f():
    x = Box()
    x += 1
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert all("'x'" not in v.message for v in violations)

    # Even if something slipped through and marked it fixable, fix() must
    # never corrupt the file.
    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


@pytest.mark.parametrize(
    ("source", "message_filter"),
    [
        (
            # A use inside a loop body isn't "the same execution point" as
            # the assignment — `value = make(); for _ in r: sink(value)`
            # runs make() once, but inlining would make it run once per
            # iteration.
            """def f(r):
    value = make()
    for _ in r:
        sink(value)
""",
            "'value'",
        ),
        (
            # A use inside a lambda body executes later (whenever the
            # lambda is called, if ever) — not once at the assignment
            # point. `x = make(); return lambda: x` must not become
            # `return lambda: make()`, which defers (and can repeat) the
            # call.
            """def f():
    x = make()
    return lambda: x
""",
            "'x'",
        ),
        (
            # An `await` is a suspension point where other code can run
            # and change state, so a use after one within the same
            # statement must be treated like a preceding call. `x =
            # make(); return sink(await future, x)` must not become
            # `sink(await future, make())`, which runs make() after the
            # await instead of before it.
            """async def f(future):
    x = make()
    return sink(await future, x)
""",
            "'x'",
        ),
        (
            # The pre-existing SINGLE_USE call allowance (args<=2) has the
            # exact same loop-repetition risk as the zero-arg carve-out.
            # `value = make(); other(); for _ in r: sink(value)` must not
            # become `for _ in r: sink(make())`, which runs make() N times
            # instead of once.
            """def f(r):
    value = make()
    other()
    for _ in r:
        sink(value)
""",
            "'value'",
        ),
        (
            # A dict literal's own AST field order (all keys, then all
            # values) doesn't match Python's real per-pair evaluation
            # order, so a naive evaluation-order walk would wrongly call
            # `x` in `{"a": side_effect(), x: 1}` safe — it isn't, since
            # "a": side_effect() runs as a pair before x is reached.
            """def f():
    x = make()
    d = {"a": side_effect(), x: 1}
""",
            "'x'",
        ),
        (
            # Binary/boolean/unary/compare operators can invoke arbitrary
            # user code via dunder overloads (__add__, __eq__, __bool__,
            # ...), so a sibling operator expression must count as a
            # preceding effect too. `x = make(); sink(a + b, x)` must not
            # become `sink(a + b, make())`.
            """def f():
    x = make()
    sink(a + b, x)
""",
            "'x'",
        ),
        (
            # A ternary's body/orelse are each conditional — never both
            # run, never unconditionally. `x = make(); sink(x if flag else
            # 0)` must not become `sink(make() if flag else 0)`.
            """def f():
    x = make()
    sink(x if flag else 0)
""",
            "'x'",
        ),
        (
            # `and`/`or` short-circuit, so a non-first operand may never
            # evaluate. `x = make(); sink(flag and x)` must not become
            # `sink(flag and make())`.
            """def f():
    x = make()
    sink(flag and x)
""",
            "'x'",
        ),
        (
            # Python evaluates an assignment's RHS *before* its target,
            # the opposite of ast.Assign's own field order. `x = make();
            # x.attr = side_effect()` must not become `make().attr =
            # side_effect()`.
            """def f():
    x = make()
    x.attr = side_effect()
""",
            "'x'",
        ),
    ],
    ids=[
        "loop-body",
        "lambda-body",
        "after-await",
        "single-use-call-in-loop-body",
        "dict-value-after-earlier-pair",
        "after-operator-sibling",
        "ternary-branch",
        "short-circuited-boolop",
        "assign-target-base",
    ],
)
def test_zero_arg_call_use_not_fixable(tmp_path: Path, source: str, message_filter: str) -> None:
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    matching = [v for v in violations if message_filter in v.message]
    assert matching
    assert all(not v.fixable for v in matching)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_single_use_call_in_loop_body_not_fixable(tmp_path: Path) -> None:
    source = """def f(r):
    value = make()
    other()
    for _ in r:
        sink(value)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    value_violations = [v for v in violations if "'value'" in v.message]
    assert value_violations
    assert all(not v.fixable for v in value_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_autofix_preserves_blank_lines_across_file(tmp_path: Path) -> None:
    # Regression: autofix used to delete blank lines across the entire
    # file, not just around the removed assignment.
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violations = check.check(filepath, tree, source)

    # This source always yields a fixable violation for `x`.
    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)
    fixed_content = filepath.read_text()

    assert "class FirstClass:\n    def method_one(self):\n        pass\n\n\nclass SecondClass:" in fixed_content
    assert (
        "class SecondClass:\n    def method_two(self):\n        pass\n\n\ndef function_with_redundant_var():"
        in fixed_content
    )
    assert "def another_function():\n    pass\n\n\nclass ThirdClass:" in fixed_content

    # Verify the fixed code is still valid Python; raises on failure.
    ast.parse(fixed_content)


def test_autofix_cleans_up_excessive_blank_lines(tmp_path: Path) -> None:
    source = """def function_with_redundant():


    x = 42


    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violations = check.check(filepath, tree, source)

    # This source always yields a fixable violation for `x`.
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

    # Verify the fixed code is still valid Python; raises on failure.
    ast.parse(fixed_content)


def test_cleanup_blank_lines_only_excess_below() -> None:
    # Branch coverage: blank_above <= 1 but blank_below > 1 (total >= 3).
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {0})
    assert lines[2] == ""
    assert lines[3] == "code\n"


def test_cleanup_blank_lines_only_excess_above() -> None:
    # Branch coverage: blank_above > 1 but blank_below <= 1 (total >= 3).
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {2})
    assert lines[0] == ""
    assert lines[3] == "code\n"


def test_fix_inlines_use_on_line_with_non_ascii_text(tmp_path: Path) -> None:
    # Regression: ast.col_offset is a UTF-8 byte offset, not a character
    # offset. A non-ASCII character earlier on the use's line must not
    # throw off the position used to locate the variable for inlining.
    source = """def process():
    data = calc()
    x = "café"; return data
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert any(v.fixable for v in violations)
    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "data" not in fixed_content
    assert "return calc()" in fixed_content
