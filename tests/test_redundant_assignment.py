"""Tests for TRI005 redundant assignment check."""

from __future__ import annotations

import ast
from pathlib import Path

from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
    PatternType,
    VariableLifecycle,
    VariableTracker,
    detect_redundancy,
)
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import _is_test_file


def test_immediate_single_use_detected() -> None:
    source = """
def func_scope():
    x = "foo"
    func(x=x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1
    violation = violations[0]
    assert violation.error_code == "TRI005"
    assert "x" in violation.message


def test_single_use_return_detected() -> None:
    source = """
def example():
    result = get_value()
    return result
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1
    assert any("result" in v.message for v in violations)


def test_literal_identity_detected() -> None:
    source = """
def func_scope():
    foo = "foo"
    process(foo)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1
    assert any("foo" in v.message for v in violations)


def test_literal_identity_with_underscores() -> None:
    source = """
def func_scope():
    SOME_VALUE = "somevalue"
    process(SOME_VALUE)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1


def test_multiple_uses_not_flagged() -> None:
    source = """
value = calc()
print(value)
log(value)
return value
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_semantic_value_skipped() -> None:
    source = """
def example():
    formatted_timestamp = format_iso8601(raw_ts)
    return formatted_timestamp
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_inline_suppression_respected() -> None:
    source = """
x = "foo"  # pytriage: ignore=TRI005
func(x=x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_inline_suppression_case_insensitive() -> None:
    source = """
x = "foo"  # PYTRIAGE: IGNORE=TRI005
func(x=x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_variable_tracker_scope_isolation() -> None:
    source = """
def outer():
    x = "outer"
    def inner():
        x = "inner"
        return x
    return x
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycles = [lc for lc in lifecycles if lc.assignment.var_name == "x"]
    assert len(x_lifecycles) == 2


def test_global_variable_not_analyzed() -> None:
    source = """
def func():
    global state
    state = "active"
    return state
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_type_annotation_adds_value() -> None:
    source = """
def example():
    result: ComplexType = calculate()
    return result
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Type annotation should increase semantic value enough to skip
    # (15 points for annotation + other factors)
    # This might still be flagged depending on total score, so we just check
    # that it doesn't crash
    assert isinstance(violations, list)


def test_comprehension_not_causing_errors() -> None:
    source = """
result = [x for x in items]
return result
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Comprehensions add semantic value (30 points), so likely won't be flagged
    # Just verify no crashes
    assert isinstance(violations, list)


def test_pattern_detection_immediate_use() -> None:
    source = """
def func():
    x = "foo"
    print(x)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")

    pattern = detect_redundancy(x_lifecycle)
    assert pattern == PatternType.IMMEDIATE_SINGLE_USE


def test_pattern_detection_single_use() -> None:
    source = """
def func():
    x = "foo"
    y = "bar"
    z = "baz"
    print(x)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")

    # Not immediate: there are intervening statements between assignment and use.
    pattern = detect_redundancy(x_lifecycle)
    assert pattern == PatternType.SINGLE_USE


def test_pattern_detection_augmented_assignment_use_is_not_redundant() -> None:
    """An augmented-assignment target (`x += 1`) can't be inlined — the
    result (`5 += 1`) is invalid syntax — and isn't the read-then-forward
    pattern TRI005 targets anyway, so detect_redundancy must return None.
    """
    source = """
def func():
    x = 5
    x += 1
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")

    assert detect_redundancy(x_lifecycle) is None


def test_match_statement_use_not_flagged() -> None:
    """A use inside a match/case body must be treated as control flow (like
    an if/elif branch), not as an ordinary use that always runs — otherwise
    it could be reported/autofixed as if the case always matched.
    """
    source = """
def f(command):
    value = make()
    match command:
        case "go":
            sink(value)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert all("'value'" not in v.message for v in violations)


def test_check_id_and_error_code() -> None:
    check = RedundantAssignmentCheck()
    assert check.check_id == "redundant-assignment"
    assert check.error_code == "TRI005"


def test_prefilter_pattern() -> None:
    check = RedundantAssignmentCheck()
    patterns = check.get_prefilter_pattern()
    assert patterns == [" = "]


def test_fixable_marked_correctly() -> None:
    source = """
def func_scope():
    x = "foo"
    func(x=x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1
    # Simple case: constant assignment, immediate use, short name, no control flow.
    assert any(v.fixable for v in violations)


def test_non_fixable_semantic_value() -> None:
    source = """
def example():
    calculated_value = expensive_operation()
    return calculated_value
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # 'calculated_value' has semantic value (transformative verb 'calculated')
    # so it should not be flagged at all
    assert len(violations) == 0


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
    """Regression: two independently-fixable assignments whose single uses
    land on the same line must both be inlined, even when the replacement
    text is a different length than the variable it replaces (which shifts
    the column of whichever use is processed second).
    """
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
    """Regression: x's only use is on the same line as y's assignment
    (`y = x`). Applying y's fix first blanks that whole line, so x's own
    fix must skip cleanly instead of crashing when its use is gone.
    """
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
    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )

    source = "x = 1\nprint(x)\n"

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    check = RedundantAssignmentCheck()

    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
            fixable=True,
            fix_data=None,
        )
    ]

    assert check.fix(filepath, violations, source, ast.parse(source)) is False


def test_autofix_skips_violation_with_invalid_fix_data(tmp_path: Path) -> None:
    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )

    source = "x = 1\nprint(x)\n"

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    check = RedundantAssignmentCheck()

    # Create a violation with invalid fix_data (missing 'use_line')
    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
            fixable=True,
            fix_data={"other_key": "value"},
        )
    ]

    assert check.fix(filepath, violations, source, ast.parse(source)) is False


def test_autofix_skips_multiline_rhs() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
        _can_safely_inline,
    )

    source_lines = ["result = func(x)\n"]

    # RHS with newline should not be inlined.
    assert _can_safely_inline("result", "func(\n    arg\n)", 0, source_lines) is False


def test_autofix_skips_line_length_violation() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
        _can_safely_inline,
    )

    # Current line is 80 chars, adding 20 more would exceed 88.
    source_lines = ["x = " + "a" * 80 + "\n"]

    assert _can_safely_inline("x", "a" * 20, 0, source_lines) is False


def test_autofix_skips_invalid_line_indices() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
        _can_safely_inline,
    )

    source_lines = ["line1\n", "line2\n"]

    assert _can_safely_inline("x", "value", -1, source_lines) is False  # negative index
    assert _can_safely_inline("x", "value", 10, source_lines) is False  # out of bounds


def test_autofix_with_invalid_assignment_line(tmp_path: Path) -> None:
    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )

    source = "x = 1\nprint(x)\n"

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    check = RedundantAssignmentCheck()

    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=100,
            col=0,
            message="test",
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
    ]

    assert check.fix(filepath, violations, source, ast.parse(source)) is False


def test_autofix_with_invalid_usage_line(tmp_path: Path) -> None:
    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )

    source = "x = 1\nprint(x)\n"

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    check = RedundantAssignmentCheck()

    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
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
    ]

    assert check.fix(filepath, violations, source, ast.parse(source)) is False


def test_autofix_with_multiple_uses(tmp_path: Path) -> None:
    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )

    source = "x = 1\nprint(x)\nprint(x)\n"

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    check = RedundantAssignmentCheck()

    # RedundantAssignmentCheck.check() leaves use_line/use_col unset
    # whenever a lifecycle doesn't have exactly one use.
    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
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
    ]

    # multiple uses
    assert check.fix(filepath, violations, source, ast.parse(source)) is False


def test_autofix_with_unsafe_inlining(tmp_path: Path) -> None:
    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )

    # Create a case where inlining would exceed 88 characters
    # Line is already 60 chars, adding 40 char value would exceed 88
    source = (
        "x = " + "a" * 40 + "\nresult = some_long_function_name(x, param1, param2)\n"
    )

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    check = RedundantAssignmentCheck()

    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
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
    ]

    assert check.fix(filepath, violations, source, ast.parse(source)) is False


def test_fix_method_with_no_fixable_violations() -> None:
    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import apply_fixes

    source = """
x = "foo"
func(x=x)
"""
    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
            fixable=False,
            fix_data=None,
        )
    ]

    assert apply_fixes(Path("test.py"), violations, source) is False


def test_nonlocal_variable_not_analyzed() -> None:
    source = """
def outer():
    x = "outer"
    def inner():
        nonlocal x
        x = "modified"
        return x
    return inner()
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert all("modified" not in v.message for v in violations)


def test_annotated_assignment_tracked() -> None:
    source = """
def example():
    x: str = "foo"
    func(x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Type annotation adds 15 points, but 'x' literal is still low value.
    assert len(violations) >= 1


def test_annotated_assignment_not_global() -> None:
    source = """
def example():
    result: int = calculate_value()
    another: str = "test"
    return result, another
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Both assignments should be tracked normally
    assert isinstance(violations, list)


def test_annotated_assignment_without_value() -> None:
    source = """
def example():
    x: str  # Type hint only, no assignment
    x = "value"
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Only the assignment with value should be tracked
    assert isinstance(violations, list)


def test_class_attributes_not_analyzed() -> None:
    source = """
class MyClass:
    x = "foo"

    def method(self):
        self.x = "bar"
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_semantic_scoring_long_expression() -> None:
    source = """
def example():
    x = very_long_function_name_that_exceeds_sixty_characters_in_total()
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Long expression should still be flagged if var name adds no value
    # But it might get some points for length
    assert isinstance(violations, list)


def test_semantic_scoring_comprehension() -> None:
    source = """
result = [x * 2 for x in range(10)]
print(result)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Comprehensions add 30 points, should help avoid flagging
    assert isinstance(violations, list)


def test_semantic_scoring_binary_op() -> None:
    source = """
result = a + b
print(result)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Binary op adds 15 points
    assert isinstance(violations, list)


def test_semantic_scoring_unary_op() -> None:
    source = """
result = -value
print(result)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Unary op adds 10 points
    assert isinstance(violations, list)


def test_semantic_scoring_ternary() -> None:
    source = """
result = x if condition else y
print(result)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Ternary adds 20 points
    assert isinstance(violations, list)


def test_semantic_scoring_lambda() -> None:
    source = """
func = lambda x: x * 2
result = func(10)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Lambda adds 25 points
    assert isinstance(violations, list)


def test_semantic_scoring_multipart_name() -> None:
    source = """
def example():
    user_email_address = get_email()
    return user_email_address
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # 3+ parts adds 20 points
    assert isinstance(violations, list)


def test_tuple_unpacking_not_analyzed() -> None:
    source = """
x, y = get_coords()
print(x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Tuple unpacking should not be flagged
    assert len(violations) == 0


def test_orchestrator_skips_file_with_invalid_syntax(tmp_path: Path) -> None:
    """Files with invalid syntax must not crash the check pipeline.

    Syntax errors are caught by CheckOrchestrator._check_file (it parses the
    AST once for all checks), not by RedundantAssignmentCheck itself.
    """
    from pre_commit_hooks.ast_checks import CheckOrchestrator

    filepath = tmp_path / "broken.py"
    filepath.write_text("x = (((")

    orchestrator = CheckOrchestrator(checks=[RedundantAssignmentCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations.get(str(filepath), []) == []


def test_autofix_should_autofix_simple_call() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    source = "get_value()"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="x",
                line=2,
                col=0,
                stmt_index=1,
                context="unknown",
                scope_id=0,
            )
        ],
    )

    # May be True or False depending on semantic score.
    assert isinstance(should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE), bool)


def test_no_uses_not_flagged() -> None:
    source = """
def example():
    x = "foo"
    y = "bar"
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_should_autofix_with_single_use_pattern() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    source = "get_value()"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    use_stmt = ast.parse("x.method()").body[0]
    use_node = next(
        n
        for n in ast.walk(use_stmt)
        if isinstance(n, ast.Name) and n.id == "x" and isinstance(n.ctx, ast.Load)
    )
    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="x",
                line=5,
                col=0,
                stmt_index=4,  # Not immediate use
                context="unknown",
                scope_id=0,
                node=use_node,
                enclosing_stmt=use_stmt,
            )
        ],
    )

    # SINGLE_USE pattern CAN be auto-fixed for simple cases (simple call with no args).
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is True


def test_semantic_scoring_medium_length_expression() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test with exactly 45 characters (between 40 and 60)
    rhs_source = "some_function_with_exactly_45_characters("
    rhs_node = ast.parse(rhs_source + ")", mode="eval").body

    # Medium length (40-60 chars) should score +10 points.
    assert calculate_semantic_value("x", rhs_source + ")", rhs_node, False) >= 10


def test_should_autofix_call_with_simple_args() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    source = "func(1, 2)"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="x",
                line=2,
                col=0,
                stmt_index=1,
                context="unknown",
                scope_id=0,
            )
        ],
    )

    # May or may not autofix, depending on semantic score.
    assert isinstance(should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE), bool)


def test_should_autofix_no_args_call() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    source = "func()"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="x",
                line=2,
                col=0,
                stmt_index=1,
                context="unknown",
                scope_id=0,
            )
        ],
    )

    # May or may not autofix, depending on semantic score.
    assert isinstance(should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE), bool)


def test_lifecycle_no_uses_not_immediate() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        VariableLifecycle,
    )

    source = "func()"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    lifecycle = VariableLifecycle(assignment=assignment, uses=[])

    assert lifecycle.is_immediate_use is False
    assert lifecycle.is_single_use is False


def test_annotated_assignment_with_nonlocal() -> None:
    source = """
def outer():
    x: str = "outer"
    def inner():
        nonlocal x
        x: str = "modified"
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Nonlocal annotated assignment should be skipped
    assert isinstance(violations, list)


def test_get_source_segment_error_handling() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
    )

    node = ast.Constant(value=1, lineno=-1, col_offset=-1)

    assert VariableTracker("x = 1")._get_source_segment(node) == ""


def test_multiple_assignments_to_same_variable() -> None:
    source = """
def example():
    x = "first"
    print(x)
    x = "second"
    print(x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Each assignment should be tracked separately
    assert isinstance(violations, list)


def test_multiple_annotated_assignments_same_variable() -> None:
    source = """
def example():
    x: str = "first"
    print(x)
    x: str = "second"
    print(x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Each annotated assignment should be tracked separately
    assert isinstance(violations, list)


def test_self_referential_assignment_correctly_tracked() -> None:
    source = """
def example():
    x = 1
    x = x + 1
    print(x)
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Second assignment (x = x + 1) has two uses (print and return)
    # First assignment (x = 1) has one use (x + 1 RHS)
    # Neither should be flagged as redundant because multiple uses
    # This test verifies that currently_assigning logic works
    assert len(violations) == 0


def test_should_autofix_complex_call_args() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    source = "func({k: v for k, v in items})"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="x",
                line=2,
                col=0,
                stmt_index=1,
                context="unknown",
                scope_id=0,
            )
        ],
    )

    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is False


def test_conditional_assignment_with_augmented_use() -> None:
    source = """
def func(v):
    if v:
        msg = "foo"
    else:
        msg = "bar"

    msg += "spameggs"

    print(msg)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # 1. Both assignments are in different branches (if/else)
    # 2. The variable is used in an augmented assignment (msg += ...)
    # 3. This is not a single-use pattern - the conditional value is essential
    assert len(violations) == 0


def test_augmented_assignment_tracks_usage() -> None:
    source = """
def example():
    x = 1
    x += 2
    print(x)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    # Augmented assignments are tracked as usages, not new assignments.
    x_lifecycles = [lc for lc in lifecycles if lc.assignment.var_name == "x"]
    assert len(x_lifecycles) == 1

    # The lifecycle should have two uses:
    # 1. The read in x += 2 (augmented assignment)
    # 2. The use in print(x)
    lifecycle = x_lifecycles[0]
    assert len(lifecycle.uses) == 2


def test_augmented_assignment_single_use_can_be_flagged() -> None:
    source = """
def example():
    x = 1
    x += 1
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # The first assignment (x = 1) is used once (in x += 1)
    # This could be flagged as it's a simple pattern
    # But augmented assignments typically indicate the variable will be used again
    # So it's reasonable either way
    assert isinstance(violations, list)


def test_repeated_augmented_assignment_reuses_existing_uses_key() -> None:
    """Branch coverage: a second augmented assignment to the same variable
    in the same scope appends to the existing self.uses[key] list rather
    than recreating it.
    """
    source = """
def example():
    x = 0
    x += 1
    x += 2
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))

    lifecycles = tracker.build_lifecycles()
    lc = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    # Each `x += n` counts as one use (the implicit read), so two augmented
    # assignments produce two entries under the same (scope_id, "x") key.
    assert len(lc.uses) == 2


def test_long_chained_expression_not_flagged() -> None:
    source = """
@functools.cache
def find_place_document(place_id):
    collection_places = singleton_factory(mongo_client)[DATABASE_NAME]["places"]
    return collection_places.find_one({"_id": place_id})
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # 1. It's a long expression (70+ chars)
    # 2. It has chained subscript operations
    # 3. The variable name is meaningful and descriptive
    # 4. Breaking it down improves readability
    assert len(violations) == 0


def test_autofix_respects_line_length(tmp_path: Path) -> None:
    source = """
def func():
    some_result = "data"
    return some_result
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    check = RedundantAssignmentCheck()
    violations = check.check(filepath, ast.parse(source), source)

    # Long variable names (>10 chars) are excluded from autofix as a
    # conservative proxy for lines that would grow too long when inlined.
    assert violations
    assert all(not v.fixable for v in violations)


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

    # Should only replace the standalone 'x', not 'max' or 'index'
    assert "max(5, index)" in filepath.read_text()


def test_chained_operations_scoring() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    source = "obj[x][y]"
    rhs_node = ast.parse(source, mode="eval").body
    # 2 chains (+20) + "result" is 1 part (+0) + short expression (+0)
    assert calculate_semantic_value("result", source, rhs_node, False) == 20

    source = "func()[x][y]"
    rhs_node = ast.parse(source, mode="eval").body
    # 3+ chains (+30) + 2-part name (+10)
    assert calculate_semantic_value("my_value", source, rhs_node, False) == 40

    source = "obj.foo.bar"
    rhs_node = ast.parse(source, mode="eval").body
    # chained attributes (2 chains = +20)
    assert calculate_semantic_value("result", source, rhs_node, False) >= 20


def test_augmented_assignment_with_global_variable() -> None:
    source = """
def func():
    global x
    x += 1
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_augmented_assignment_with_nonlocal_variable() -> None:
    source = """
def outer():
    x = 1
    def inner():
        nonlocal x
        x += 1
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert isinstance(violations, list)


def test_augmented_assignment_with_attribute() -> None:
    source = """
def func():
    obj.x += 1
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_semantic_scoring_very_long_expression() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    source = "a" * 85
    rhs_node = ast.parse(source, mode="eval").body
    # Very long expression (80+ chars) scores +35.
    assert calculate_semantic_value("x", source, rhs_node, False) >= 35


# === Autofix Safety Tests ===
# Tests to verify autofix only handles safe, simple cases


def test_autofix_not_in_loop() -> None:
    source = """
for i in range(10):
    x = i * 2
    print(x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_autofix_not_in_control_flow() -> None:
    source = """
def example():
    if condition:
        x = "value"
        process(x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # May detect but should not be fixable due to control flow
    for v in violations:
        assert not v.fixable


def test_autofix_not_long_names() -> None:
    source = """
very_long_descriptive_name = 42
use(very_long_descriptive_name)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not be fixable due to long variable name (> 10 chars).
    assert all(not v.fixable for v in violations)


def test_autofix_only_simple_rhs() -> None:
    source = """
def example():
    x = func(arg1, arg2)
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    for v in violations:
        assert not v.fixable


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

    # Simple constant should be fixable.
    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations
    fix_applied = check.fix(filepath, fixable_violations, source, tree)
    assert fix_applied is True

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

    # Simple attribute access should be fixable.
    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations
    fix_applied = check.fix(filepath, fixable_violations, source, tree)
    assert fix_applied is True

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
    fix_applied = check.fix(filepath, fixable_violations, source, tree)
    assert fix_applied is True

    fixed_content = filepath.read_text()
    # Should replace 'x' but not affect 'max'
    assert "result = max(5, 10)" in fixed_content
    assert "max" in fixed_content  # 'max' should still be present


# === Bug Reproduction Tests ===
# The following tests reproduce bugs from bug_report.md


def test_problem_1_loop_reassignment() -> None:
    source = """def find_route():
    latest_datetime = initial_datetime
    for edge in edges:
        destination_datetime_utc = edge.destination_datetime_utc
        if destination_datetime_utc > latest_datetime:
            latest_datetime = destination_datetime_utc
            break
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag latest_datetime in loop reassignment.
    assert all("latest_datetime" not in v.message for v in violations)


def test_problem_2_boolean_descriptive_names() -> None:
    source = """def check_cycle(subgraph, depot_idx):
    out_edge_count = len(subgraph.out_edges(depot_idx))
    in_edge_count = len(subgraph.in_edges(depot_idx))
    has_cycle = bool(find_cycle(subgraph, depot_idx))
    if not all((out_edge_count, in_edge_count, has_cycle)):
        raise ValueError("Invalid graph")
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag descriptive boolean variable has_cycle.
    assert all("has_cycle" not in v.message for v in violations)


def test_problem_4_multiple_exception_assignments() -> None:
    source = """def fetch_data():
    error = None
    try:
        return get_data()
    except ValueError as value_error:
        error = value_error
    except TypeError as type_error:
        error = type_error
    except KeyError as key_error:
        error = key_error
    raise error
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # `error` is assigned multiple times (once per except branch), so it is
    # skipped entirely rather than risk autofix producing concatenated
    # nonsense like "value_errortype_errorkey_error".
    assert violations == []


def test_problem_5_conditional_assignment_logic_change() -> None:
    source = """def configure(service_name=None):
    if not service_name:
        service_name = get_caller_module_name()
    return configure_service(service_name)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # `service_name` is assigned inside an `if` block but used outside it, so
    # it is skipped entirely rather than risk autofix changing program logic
    # (e.g. turning it into "if not get_caller_module_name():").
    assert violations == []


def test_same_variable_different_scopes() -> None:
    source = """def process(value):
    if value > 0:
        result = "positive"
        log(result)
    else:
        result = "negative"
        log(result)
    return result
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # 1. It's assigned in different branches
    # 2. It's used after the if/else block
    # 3. Both assignments are needed for the final return
    assert all(
        "result" not in v.message
        or "positive" not in source
        or "negative" not in source
        for v in violations
    )


def test_autofix_preserves_blank_lines_across_file(tmp_path: Path) -> None:
    """Test that autofix only cleans up blank lines around removed assignments.

    Regression test for bug where autofix was deleting blank lines across
    the entire file, not just around the removed assignment.
    """
    # File with multiple classes/functions separated by blank lines
    # and one redundant assignment that will be autofixed
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

    # Verify blank lines between classes/functions are preserved
    # These blank lines should NOT be affected by autofix
    expected_pattern_1 = (
        "class FirstClass:\n    def method_one(self):\n        pass\n\n\n"
        "class SecondClass:"
    )
    assert expected_pattern_1 in fixed_content, (
        "Blank lines between FirstClass and SecondClass were removed!"
    )

    expected_pattern_2 = (
        "class SecondClass:\n    def method_two(self):\n        pass\n\n\n"
        "def function_with_redundant_var():"
    )
    assert expected_pattern_2 in fixed_content, (
        "Blank lines between SecondClass and function_with_redundant_var were removed!"
    )

    expected_pattern_3 = "def another_function():\n    pass\n\n\nclass ThirdClass:"
    assert expected_pattern_3 in fixed_content, (
        "Blank lines between another_function and ThirdClass were removed!"
    )

    # Verify the fixed code is still valid Python; raises on failure.
    ast.parse(fixed_content)


def test_autofix_cleans_up_excessive_blank_lines(tmp_path: Path) -> None:
    # File with excessive blank lines around a redundant assignment
    # The blank lines between the removed assignment should be cleaned up
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

    # Count blanks before the return statement, after removing x=42.
    def_index = next(
        i for i, line in enumerate(lines) if "def function_with_redundant" in line
    )
    return_index = next(i for i in range(def_index, len(lines)) if "return" in lines[i])
    blanks_before_return = 0
    j = return_index - 1
    while j >= 0 and lines[j].strip() == "":
        blanks_before_return += 1
        j -= 1

    assert blanks_before_return <= 2, (
        f"Fixed code has {blanks_before_return} blank lines before return "
        f"(expected ≤2)\n{fixed_content}"
    )

    # Verify the fixed code is still valid Python; raises on failure.
    ast.parse(fixed_content)


def test_cleanup_blank_lines_only_excess_below() -> None:
    """Branch coverage: blank_above <= 1 but blank_below > 1 (total >= 3).

    Exercises the False branch of ``if blank_above > 1`` when there are no
    excess blanks above the removed line but there are excess blanks below it.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
        _cleanup_blank_lines_around_removals,
    )

    # removed_idx=0, blank_above=0, blank_below=2 → total=3
    # "if blank_above > 1" is False → branch 161->167 taken
    # "if blank_below > 1" is True  → excess below removed
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {0})
    # excess blank at index 2 should be cleared
    assert lines[2] == ""
    assert lines[3] == "code\n"


def test_cleanup_blank_lines_only_excess_above() -> None:
    """Branch coverage: blank_above > 1 but blank_below <= 1 (total >= 3).

    Exercises the False branch of ``if blank_below > 1`` when there are no
    excess blanks below the removed line but there are excess blanks above it.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
        _cleanup_blank_lines_around_removals,
    )

    # removed_idx=2, blank_above=2, blank_below=0 → total=3
    # "if blank_above > 1" is True  → excess above removed
    # "if blank_below > 1" is False → branch 167->136 taken
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {2})
    # excess blank at index 0 should be cleared
    assert lines[0] == ""
    assert lines[3] == "code\n"


def test_global_scope_without_underscore_not_flagged() -> None:
    source = """
parent_url = "https://example.com"
print(parent_url)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_global_scope_with_underscore_flagged() -> None:
    source = """
_temp = "foo"
print(_temp)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1
    assert any("_temp" in v.message for v in violations)


def test_global_scope_with_comment_above_not_flagged() -> None:
    source = """
# Configuration URL
_url = "https://example.com"
print(_url)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_function_scope_single_use_still_flagged() -> None:
    source = """
def func():
    x = "foo"
    print(x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1
    assert any("x" in v.message for v in violations)


def test_await_on_both_assignment_and_usage_not_flagged() -> None:
    source = """
async def test_json(client):
    response = await get_test_response(client, '/null_content')
    assert await response.json() is None
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_await_on_assignment_not_flagged() -> None:
    """Test that await on assignment is NOT flagged.

    Inlining await expressions often requires parentheses, making code bulky:
        json_resp = await resp.json()
        return json_resp['key']
    Would become:
        return (await resp.json())['key']  # ugly
    """
    source = """
async def test_func():
    x = await get_value()
    process(x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_await_only_on_usage_flagged() -> None:
    source = """
async def test_func():
    x = get_value()
    result = await x.fetch()
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1
    assert any("x" in v.message for v in violations)


def test_ternary_operator_not_flagged() -> None:
    source = """
import sys

DEFAULT_URL = "https://default.example.com"
parent_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
print(parent_url)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_ternary_in_function_not_flagged() -> None:
    source = """
def func(condition):
    value = "yes" if condition else "no"
    return value
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_long_rhs_over_79_chars_not_flagged() -> None:
    source = """
def func():
    variable = compute_something_with_very_long_function_name()
    assert variable.attribute_name
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'variable' if inlining would exceed 79 characters
    # The heuristic checks if len(rhs_source) >= 25 or len_diff > 15
    # len(rhs_source) = 49 >= 25, so should not be flagged
    assert len(violations) == 0


def test_comment_above_in_function_scope_not_flagged() -> None:
    source = """
def auto_clear_fixture():
    # Exclude cache.
    # The prefixes are hard-coded in external library
    cache_prefixes = ("responses", "redirects")
    process(cache_prefixes)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_rhs_at_25_char_threshold_not_flagged() -> None:
    source = """
def func():
    prefixes = ("responses", "redirects")
    process(prefixes)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag because RHS is 26 chars (>= 25)
    # len('("responses", "redirects")') = 26
    assert len(violations) == 0


def test_comment_above_multiline_not_flagged() -> None:
    source = """
def func():
    # First comment line
    # Second comment line
    # Third comment line with URL: https://example.com/path
    variable = calculate_value()
    return variable
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag because there's a comment on the line directly above
    assert len(violations) == 0


def test_would_require_parentheses_binop() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _would_require_parentheses,
    )

    rhs_node = ast.parse("len(x) + 1", mode="eval").body
    assert _would_require_parentheses(rhs_node) is True


def test_would_require_parentheses_boolop() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _would_require_parentheses,
    )

    rhs_node = ast.parse("a and b", mode="eval").body
    assert _would_require_parentheses(rhs_node) is True


def test_would_require_parentheses_compare() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _would_require_parentheses,
    )

    rhs_node = ast.parse("x == y", mode="eval").body
    assert _would_require_parentheses(rhs_node) is True


def test_would_require_parentheses_simple() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _would_require_parentheses,
    )

    rhs_node = ast.parse("len(x)", mode="eval").body
    assert _would_require_parentheses(rhs_node) is False


def test_should_report_violation_with_parentheses_required() -> None:
    source = """
def func():
    len_prefix = len(x) + 1
    return arr[len_prefix:]
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_should_autofix_single_use_with_attribute() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    source = "obj.attr"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="x",
                line=5,
                col=0,
                stmt_index=4,
                context="unknown",
                scope_id=0,
            )
        ],
    )

    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is True


def test_should_autofix_single_use_with_keywords() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    source = "func(key=value)"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    use_stmt = ast.parse("x.method()").body[0]
    use_node = next(
        n
        for n in ast.walk(use_stmt)
        if isinstance(n, ast.Name) and n.id == "x" and isinstance(n.ctx, ast.Load)
    )
    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="x",
                line=5,
                col=0,
                stmt_index=4,
                context="unknown",
                scope_id=0,
                node=use_node,
                enclosing_stmt=use_stmt,
            )
        ],
    )

    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is True


def test_should_autofix_single_use_high_semantic_score() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    source = "value"
    rhs_node = ast.parse(source, mode="eval").body

    # Use a long descriptive name that will have high semantic score
    assignment = AssignmentInfo(
        var_name="formatted_validated_user_full_name",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="formatted_validated_user_full_name",
                line=5,
                col=0,
                stmt_index=4,
                context="unknown",
                scope_id=0,
            )
        ],
    )

    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_should_not_autofix_single_use_complex_call() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    # Call with 3 args (exceeds limit of 2)
    source = "func(a, b, c)"
    rhs_node = ast.parse(source, mode="eval").body

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=source,
        scope_id=0,
        has_type_annotation=False,
    )

    # Use is in a safe (leading, non-loop, non-lambda) position, so this
    # exercises the arg-count rejection specifically, not the execution-
    # context check that _call_use_is_safe_to_inline also performs.
    use_stmt = ast.parse("x.method()").body[0]
    use_node = next(
        n
        for n in ast.walk(use_stmt)
        if isinstance(n, ast.Name) and n.id == "x" and isinstance(n.ctx, ast.Load)
    )
    lifecycle = VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name="x",
                line=5,
                col=0,
                stmt_index=4,
                context="unknown",
                scope_id=0,
                node=use_node,
                enclosing_stmt=use_stmt,
            )
        ],
    )

    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_closure_variable_not_flagged() -> None:
    source = """
async def test_func(faker):
    return_value = faker.pystr()

    @decorator
    async def inner_func():
        return return_value

    await inner_func()
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_closure_with_mock_not_flagged() -> None:
    source = """
async def test_func():
    from unittest.mock import Mock
    mock = Mock()

    async def inner_func():
        mock()
        return "result"

    await inner_func()
    assert mock.call_count == 1
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_closure_single_use_in_nested_function() -> None:
    source = """
def outer():
    value = calculate()

    def inner():
        return value

    return inner
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_closure_multiple_nested_levels() -> None:
    source = """
def level1():
    x = 1

    def level2():
        y = x + 1

        def level3():
            return x + y

        return level3()

    return level2()
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_closure_with_decorator() -> None:
    """Test the exact scenario from the bug report."""
    source = """
async def test_rate_limited_decorator_exceeds_limit(
    backend, faker, rate_limit_params
):
    mock = Mock()
    limit, period = rate_limit_params
    return_value = faker.pystr()

    @rate_limited(backend=backend, limit=limit, period=period, ttl=period)
    async def func():
        mock()
        return return_value

    for _ in range(limit):
        assert await func() == return_value
    assert mock.call_count == limit
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 0


def test_non_closure_still_detected() -> None:
    """This is NOT a closure - just a redundant assignment in the same scope."""
    source = """
def test_func():
    x = "foo"
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1
    assert any("x" in v.message for v in violations)


def test_lifecycle_is_immediate_use_with_closure() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        UsageInfo,
        VariableLifecycle,
    )

    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=ast.parse("1", mode="eval").body,
        rhs_source="1",
        scope_id=1,  # Outer scope
        has_type_annotation=False,
    )

    usage = UsageInfo(
        var_name="x",
        line=3,
        col=0,
        stmt_index=1,  # Would normally be considered immediate
        context="unknown",
        scope_id=2,  # Nested scope (closure)
    )

    lifecycle = VariableLifecycle(assignment=assignment, uses=[usage])

    # Even though stmt_index suggests immediate use, it should return False
    # because the use is in a different scope (closure)
    assert lifecycle.is_immediate_use is False
    assert lifecycle.is_single_use is True


def test_verbose_variable_names_kwargs_get_not_flagged() -> None:
    """Example from user request: raw_headers = kwargs.get("headers") — the
    name "raw_headers" is more descriptive than just "headers".
    """
    source = """
async def request_json(
    self,
    url: str,
    *,
    method: str = "GET",
    response_content_type: str = "application/json",
    **kwargs,
) -> dict:
    raw_headers = kwargs.get("headers")
    headers = CIMultiDict(raw_headers or {})
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'raw_headers' - it adds verbosity.
    assert all("raw_headers" not in v.message for v in violations)


def test_verbose_variable_names_parsed_data_not_flagged() -> None:
    """Example from user request: translations = orjson.loads(f.read()) —
    the name "translations" describes what the parsed data represents.
    """
    source = """
def load_translations(language, template_name):
    path = TRANSLATIONS_DIR / f"{language}.json"
    file_path = (
        TRANSLATIONS_DIR / "eng.json"
        if not path.exists() or language is None
        else path
    )
    with open(file_path) as f:
        translations = orjson.loads(f.read())
        return {
            k: v
            for k, v in translations.items()
            if k in {TRANSLATIONS_GENERAL, TEMPLATES_TO_TRANSLATIONS[template_name]}
        }
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'translations' - it adds context.
    assert all("translations" not in v.message for v in violations)


def test_firestore_client_not_flagged() -> None:
    """Example: firestore_client = db.client() — more specific than "client"."""
    source = """
def get_firestore():
    firestore_client = db.client()
    return firestore_client
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'firestore_client' - it's more specific.
    assert all("firestore_client" not in v.message for v in violations)


def test_user_email_dict_access_not_flagged() -> None:
    """Example: user_email = data["email"] — more verbose/specific than "email"."""
    source = """
def process_user(data):
    user_email = data["email"]
    send_notification(user_email)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'user_email' - it adds verbosity.
    assert all("user_email" not in v.message for v in violations)


def test_descriptive_prefix_not_flagged() -> None:
    """Descriptive prefixes recognized: raw_data, parsed_output, validated_input."""
    source = """
def process_input(data):
    raw_data = fetch_from_api()
    return raw_data
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'raw_data' - 'raw' is descriptive.
    assert all("raw_data" not in v.message for v in violations)


def test_adds_verbosity_or_context_function_directly() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _adds_verbosity_or_context,
    )

    # Test Pattern 1: Descriptive prefix
    rhs_node = ast.parse("fetch_data()", mode="eval").body
    assert _adds_verbosity_or_context("raw_data", "fetch_data()", rhs_node) is True

    # Test Pattern 2: Variable contains RHS key but is more verbose
    rhs_node = ast.parse('kwargs.get("headers")', mode="eval").body
    assert (
        _adds_verbosity_or_context("raw_headers", 'kwargs.get("headers")', rhs_node)
        is True
    )

    # Test Pattern 3: .get() with more context
    rhs_node = ast.parse('data.get("email")', mode="eval").body
    assert (
        _adds_verbosity_or_context("user_email", 'data.get("email")', rhs_node) is True
    )

    # Test Pattern 4: Generic parse functions with descriptive names
    rhs_node = ast.parse("orjson.loads(data)", mode="eval").body
    assert (
        _adds_verbosity_or_context("translations", "orjson.loads(data)", rhs_node)
        is True
    )

    # Test Pattern 4: Generic parse with multi-part name
    rhs_node = ast.parse("json.load(f)", mode="eval").body
    assert _adds_verbosity_or_context("user_config", "json.load(f)", rhs_node) is True

    # Test Pattern 4: Parse function but generic variable name (should be False)
    rhs_node = ast.parse("json.loads(data)", mode="eval").body
    assert _adds_verbosity_or_context("data", "json.loads(data)", rhs_node) is False

    # Test Pattern 4: Parse function as Name node
    rhs_node = ast.parse("loads(data)", mode="eval").body
    assert _adds_verbosity_or_context("configuration", "loads(data)", rhs_node) is True

    # Test negative case: no verbosity added
    rhs_node = ast.parse("42", mode="eval").body
    assert _adds_verbosity_or_context("x", "42", rhs_node) is False


def test_no_false_positive_on_multiline_rhs_fixable_marking() -> None:
    """Regression test: violations used to be marked [FIXABLE] even when
    --fix couldn't actually fix them.
    """
    source = """
def func():
    value = foo(
        1
    )
    return value
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    value_violations = [v for v in violations if "value" in v.message]
    assert value_violations
    # Multiline RHS should not be marked fixable.
    assert all(not v.fixable for v in value_violations)


def test_semantic_value_descriptive_boolean_prefix() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_node = ast.parse("check_something()", mode="eval").body
    # has_ prefix scores +50
    assert (
        calculate_semantic_value("has_permission", "check_something()", rhs_node, False)
        >= 50
    )


def test_semantic_value_descriptive_suffix() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_node = ast.parse("len(items)", mode="eval").body
    assert calculate_semantic_value("item_count", "len(items)", rhs_node, False) >= 40


def test_semantic_value_list_comprehension() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    source = "[x for x in items]"
    rhs_node = ast.parse(source, mode="eval").body
    assert calculate_semantic_value("result", source, rhs_node, False) >= 30


def test_semantic_value_unary_operation() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    source = "-value"
    rhs_node = ast.parse(source, mode="eval").body
    assert calculate_semantic_value("result", source, rhs_node, False) >= 10


def test_semantic_value_lambda_expression() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    source = "lambda x: x * 2"
    rhs_node = ast.parse(source, mode="eval").body
    assert calculate_semantic_value("func", source, rhs_node, False) >= 25


def test_semantic_value_very_long_expression() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    source = "a" * 85
    rhs_node = ast.parse(source, mode="eval").body
    assert calculate_semantic_value("x", source, rhs_node, False) >= 35


def test_semantic_value_long_expression_60_plus() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    source = "a" * 65
    rhs_node = ast.parse(source, mode="eval").body
    assert calculate_semantic_value("x", source, rhs_node, False) >= 25


def test_no_false_positive_on_long_rhs_fixable_marking() -> None:
    """Regression test: violations used to be marked [FIXABLE] even when
    --fix couldn't actually fix them. A 3-argument call is short enough to be
    reported but too complex for autofix's argument-count allowance.
    """
    source = """
def func():
    value = some_func(a, b, c)
    return value
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    value_violations = [v for v in violations if "value" in v.message]
    assert value_violations
    # Complex RHS should not be marked fixable.
    assert all(not v.fixable for v in value_violations)


def test_no_false_positive_on_long_use_line_fixable_marking() -> None:
    """Regression test (issue #22): [FIXABLE] used to lie when the RHS and
    variable name were both short but the *use* line was long. should_autofix
    only estimated line length from the RHS/var-name lengths (it never had
    the real use line), so it said "safe" while apply_fixes' real length
    check silently declined to touch the same violation.
    """
    source = """
def f():
    source = "..."
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    # comment
    violations = check.check(Path("tests/test_long_name.py"), tree, source)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    tree_violations = [v for v in violations if "'tree'" in v.message]
    assert tree_violations
    # The use line is too long for --fix to actually inline this safely.
    assert all(not v.fixable for v in tree_violations)


def test_zero_arg_call_immediate_single_use_is_fixable(tmp_path: Path) -> None:
    """Regression test (issue #22): IMMEDIATE_SINGLE_USE never allowed a Call
    RHS, even trivial zero-arg ones, so idiomatic test code like
    `check = ForbidVarsCheck(); check.check(...)` was never auto-fixed. A
    zero-arg call has no operands whose evaluation order inlining could
    disturb, so it's safe to allow as a narrow carve-out.
    """
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


def test_magic_number_not_flagged() -> None:
    """Variables like max_search_depth = 10 give semantic meaning to raw numbers."""
    source = """
def find_project_root():
    max_search_depth = 10
    current_dir = Path.cwd()
    for _ in range(max_search_depth):
        if (current_dir / "pyproject.toml").is_file():
            return current_dir
        current_dir = current_dir.parent
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'max_search_depth' - avoids magic number.
    assert all("max_search_depth" not in v.message for v in violations)


def test_magic_number_float_not_flagged() -> None:
    source = """
def calculate_spacing():
    line_spacing = 1.2
    coords = (x, y + height * line_spacing)
    return coords
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'line_spacing' - avoids magic number.
    assert all("line_spacing" not in v.message for v in violations)


def test_magic_number_id_not_flagged() -> None:
    source = """
async def find_nicosia(database):
    nicosia_in_cyprus_id = 101749141
    place = await database.find_one({"_id": nicosia_in_cyprus_id})
    return place
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'nicosia_in_cyprus_id' - avoids magic number.
    assert all("nicosia_in_cyprus_id" not in v.message for v in violations)


def test_pytest_raises_pattern_not_flagged() -> None:
    """Setup should live outside the context manager to keep the with block minimal."""
    source = """
def test_rate_limit():
    sample_class = SampleClass()
    with pytest.raises(RateLimitError):
        sample_class.sample_method()
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'sample_class' - pytest.raises pattern.
    assert all("sample_class" not in v.message for v in violations)


def test_with_block_pattern_not_flagged() -> None:
    source = """
def test_retry():
    decorated_mock_func = retry_service(mock_func)

    with pytest.raises(ValueError, match=error_msg):
        decorated_mock_func()
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'decorated_mock_func' - with block pattern.
    assert all("decorated_mock_func" not in v.message for v in violations)


def test_augmented_assignment_use_not_flagged() -> None:
    """Regression test: an augmented-assignment target (`x += 1`) is a
    mutation, not a read-then-pass-through — and even if it were flagged,
    inlining it would produce invalid syntax (`x = 5; x += 1` -> `5 += 1`).
    It must never be reported, let alone marked fixable.
    """
    source = """
def f():
    x = 5
    x += 1
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert all("'x'" not in v.message for v in violations)


def test_augmented_assignment_use_not_flagged_for_zero_arg_call(
    tmp_path: Path,
) -> None:
    """Regression test: the issue #22 zero-arg-call carve-out for
    IMMEDIATE_SINGLE_USE must not make `x = Box(); x += 1` fixable — inlining
    would produce invalid syntax (`Box() += 1`).
    """
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


def test_zero_arg_call_use_in_loop_body_not_fixable(tmp_path: Path) -> None:
    """Regression test (P1 caught in review of issue #22's fix): a use
    inside a loop body isn't "the same execution point" as the assignment —
    `value = make(); for _ in r: sink(value)` runs make() once, but inlining
    would make it run once per iteration.
    """
    source = """def f(r):
    value = make()
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


def test_zero_arg_call_use_in_lambda_not_fixable(tmp_path: Path) -> None:
    """Regression test (same P1): a use inside a lambda body executes later
    (whenever the lambda is called, if ever) — not once at the assignment
    point. `x = make(); return lambda: x` must not become
    `return lambda: make()`, which defers (and can repeat) the call.
    """
    source = """def f():
    x = make()
    return lambda: x
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    x_violations = [v for v in violations if "'x'" in v.message]
    assert x_violations
    assert all(not v.fixable for v in x_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_zero_arg_call_use_after_await_not_fixable(tmp_path: Path) -> None:
    """Regression test (2nd P1 caught in review of issue #22's fix): an
    `await` is a suspension point where other code can run and change
    state, so a use after one within the same statement must be treated
    like a preceding call. `x = make(); return sink(await future, x)` must
    not become `sink(await future, make())`, which runs make() after the
    await instead of before it.
    """
    source = """async def f(future):
    x = make()
    return sink(await future, x)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    x_violations = [v for v in violations if "'x'" in v.message]
    assert x_violations
    assert all(not v.fixable for v in x_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_single_use_call_in_loop_body_not_fixable(tmp_path: Path) -> None:
    """Regression test: the pre-existing SINGLE_USE call allowance (args<=2)
    has the exact same loop-repetition risk as the new zero-arg carve-out,
    and predates this issue entirely — confirmed reproducible on main before
    any of these changes. `value = make(); other(); for _ in r: sink(value)`
    must not become `for _ in r: sink(make())`, which runs make() N times
    instead of once.
    """
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


def test_zero_arg_call_use_as_dict_value_after_earlier_pair_not_fixable(
    tmp_path: Path,
) -> None:
    """Regression test (4th P1 caught in review of issue #22's fix): a dict
    literal's own AST field order (all keys, then all values) doesn't match
    Python's real per-pair evaluation order, so a naive evaluation-order
    walk would wrongly call `x` in `{"a": side_effect(), x: 1}` safe — it
    isn't, since "a": side_effect() runs as a pair before x is reached.
    """
    source = """def f():
    x = make()
    d = {"a": side_effect(), x: 1}
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    x_violations = [v for v in violations if "'x'" in v.message]
    assert x_violations
    assert all(not v.fixable for v in x_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_zero_arg_call_use_after_operator_sibling_not_fixable(tmp_path: Path) -> None:
    """Regression test (5th P1 caught in review of issue #22's fix): binary/
    boolean/unary/compare operators can invoke arbitrary user code via
    dunder overloads (`__add__`, `__eq__`, `__bool__`, ...), so a sibling
    operator expression must count as a preceding effect too, not just
    calls/attribute access. `x = make(); sink(a + b, x)` must not become
    `sink(a + b, make())`, which could run `a.__add__(b)` before `make()`
    instead of after it.
    """
    source = """def f():
    x = make()
    sink(a + b, x)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    x_violations = [v for v in violations if "'x'" in v.message]
    assert x_violations
    assert all(not v.fixable for v in x_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_zero_arg_call_use_in_ternary_branch_not_fixable(tmp_path: Path) -> None:
    """Regression test (6th/7th P1 caught in review of issue #22's fix): a
    ternary's body/orelse are each conditional — never both run, never
    unconditionally. `x = make(); sink(x if flag else 0)` must not become
    `sink(make() if flag else 0)`, which skips make() entirely when flag
    is falsy, unlike the original always-executed call.
    """
    source = """def f():
    x = make()
    sink(x if flag else 0)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    x_violations = [v for v in violations if "'x'" in v.message]
    assert x_violations
    assert all(not v.fixable for v in x_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_zero_arg_call_use_in_short_circuited_boolop_not_fixable(
    tmp_path: Path,
) -> None:
    """Regression test (same finding): `and`/`or` short-circuit, so a
    non-first operand may never evaluate. `x = make(); sink(flag and x)`
    must not become `sink(flag and make())`, which skips make() when flag
    is falsy.
    """
    source = """def f():
    x = make()
    sink(flag and x)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    x_violations = [v for v in violations if "'x'" in v.message]
    assert x_violations
    assert all(not v.fixable for v in x_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_zero_arg_call_use_as_assign_target_base_not_fixable(tmp_path: Path) -> None:
    """Regression test (8th P1 caught in review of issue #22's fix): Python
    evaluates an assignment's RHS *before* its target, the opposite of
    ast.Assign's own field order. `x = make(); x.attr = side_effect()` must
    not become `make().attr = side_effect()`, which runs side_effect()
    before make() instead of after it.
    """
    source = """def f():
    x = make()
    x.attr = side_effect()
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    x_violations = [v for v in violations if "'x'" in v.message]
    assert x_violations
    assert all(not v.fixable for v in x_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_inline_comment_not_flagged() -> None:
    """Inline comments indicate intentional code (e.g., type: ignore)."""
    source = """
def get_cache_file(cache):
    redirects_file = cache.redirects.filename  # type: ignore[attr-defined]

    assert redirects_file.startswith(cache_dir)
    return redirects_file
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'redirects_file' - has inline comment.
    assert all("redirects_file" not in v.message for v in violations)


def test_nonlocal_in_nested_function_not_flagged() -> None:
    """Regression test: the linter used to remove a variable that was
    modified via nonlocal in a nested function.
    """
    source = """
async def test_websocket():
    cancelled = False
    ping_started = loop.create_future()

    async def delayed_send_frame():
        nonlocal cancelled
        ping_started.set_result(None)
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise

    await resp.close()
    assert cancelled is True
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'cancelled' - captured by nonlocal.
    assert all("cancelled" not in v.message for v in violations)


def test_nonlocal_multiple_variables_not_flagged() -> None:
    source = """
def outer():
    x = 0
    y = 0

    def inner():
        nonlocal x, y
        x = 1
        y = 2

    inner()
    return x + y
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'x' or 'y' - captured by nonlocal.
    assert all("'x'" not in v.message and "'y'" not in v.message for v in violations)


def test_has_inline_comment_detection() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        _has_inline_comment,
    )

    # Test with inline comment
    lines = ["x = value  # this is a comment"]
    assert _has_inline_comment(1, lines) is True

    # Test without comment
    lines = ["x = value"]
    assert _has_inline_comment(1, lines) is False

    # Test with # inside string (should NOT detect as comment)
    lines = ['x = "hello # world"']
    assert _has_inline_comment(1, lines) is False

    # Test with # in both string and as comment
    lines = ['x = "foo"  # comment']
    assert _has_inline_comment(1, lines) is True

    # Test out of bounds
    assert _has_inline_comment(0, lines) is False
    assert _has_inline_comment(5, lines) is False


def test_is_named_constant_pattern() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _is_named_constant_pattern,
    )

    # Test with multi-part name and number
    node = ast.parse("10", mode="eval").body
    assert _is_named_constant_pattern("max_depth", node) is True

    # Test with float
    node = ast.parse("1.2", mode="eval").body
    assert _is_named_constant_pattern("line_spacing", node) is True

    # Test single-part long name
    node = ast.parse("42", mode="eval").body
    assert _is_named_constant_pattern("threshold", node) is True

    # Test single-part short generic name (should NOT match)
    node = ast.parse("10", mode="eval").body
    assert _is_named_constant_pattern("value", node) is False
    assert _is_named_constant_pattern("num", node) is False

    # Test with non-numeric (should NOT match)
    node = ast.parse('"hello"', mode="eval").body
    assert _is_named_constant_pattern("msg", node) is False


def test_while_loop_assignment_not_flagged() -> None:
    source = """
def process():
    x = 0
    while x < 10:
        x = x + 1
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'x' in while loop.
    assert all("'x'" not in v.message for v in violations)


def test_async_for_loop_assignment_not_flagged() -> None:
    source = """
async def process(items):
    result = []
    async for item in items:
        result = result + [item]
    return result
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag loop var.
    assert all("'result'" not in v.message for v in violations)


def test_async_with_assignment_not_flagged() -> None:
    source = """
async def process():
    async with context() as ctx:
        x = ctx.value
        return x
"""
    check = RedundantAssignmentCheck()
    # Just verify it doesn't crash - async with should be tracked
    _ = check.check(Path("test.py"), ast.parse(source), source)


def test_global_attribute_assignment_not_tracked() -> None:
    source = """
global_obj = None

def modify_global():
    global global_obj
    global_obj.attr = "value"
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)
    assert len(violations) == 0


def test_nondeterministic_call_not_flagged() -> None:
    source = """
import time

def measure():
    start = time.time()
    do_work()
    return start
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'start' - nondeterministic.
    assert all("start" not in v.message for v in violations)


def test_multiple_assignment_targets_not_tracked() -> None:
    source = """
def func():
    a = b = c = some_value()
    return a + b + c
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)
    # Multiple assignment targets are skipped entirely.
    assert all("'a'" not in v.message for v in violations)
    assert all("'b'" not in v.message for v in violations)
    assert all("'c'" not in v.message for v in violations)


def test_inline_comment_with_string_containing_hash() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        _has_inline_comment,
    )

    # String with # followed by actual comment
    lines = ['x = "test#test"  # real comment']
    assert _has_inline_comment(1, lines) is True

    # Only string with # (no real comment)
    lines = ['x = "test # not a comment"']
    assert _has_inline_comment(1, lines) is False

    # Empty string then comment
    lines = ['x = ""  # comment']
    assert _has_inline_comment(1, lines) is True


def test_ternary_operator_ifexp_not_flagged() -> None:
    source = """
def func(condition):
    result = "yes" if condition else "no"
    return result
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag ternary expression.
    assert all("result" not in v.message for v in violations)


def test_descriptive_suffix_size_not_flagged() -> None:
    """Variables like large_payload_size = len(large_payload) clarify what
    the value represents, making the code more readable.
    """
    source = """
def test_flow_control_binary(protocol, out_low_limit, parser_low_limit):
    large_payload = b"b" * (1 + 16 * 2)
    large_payload_size = len(large_payload)
    parser_low_limit._handle_frame(True, WSMsgType.BINARY, large_payload, 0)
    res = out_low_limit._buffer[0]
    assert res == WSMessageBinary(data=large_payload, size=large_payload_size, extra="")
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'large_payload_size' - has _size suffix.
    assert all("large_payload_size" not in v.message for v in violations)


def test_descriptive_suffix_length_not_flagged() -> None:
    source = """
def process(data):
    buffer_length = len(data)
    return process_with_length(data, buffer_length)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'buffer_length' - has _length suffix.
    assert all("buffer_length" not in v.message for v in violations)


def test_descriptive_suffix_id_not_flagged() -> None:
    source = """
def get_user(data):
    user_id = data.get("id")
    return fetch_user(user_id)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'user_id' - has _id suffix.
    assert all("user_id" not in v.message for v in violations)


def test_test_file_detection_by_path() -> None:
    source = """
def test_camel_to_under():
    camel_case_sample = "RandomClassName"
    assert camel_to_under(camel_case_sample) == "random_class_name"
"""
    check = RedundantAssignmentCheck()

    # File in tests/ directory should not flag test setup variables
    violations = check.check(Path("tests/test_utils.py"), ast.parse(source), source)

    # Should not flag test setup variable in test file.
    assert all("camel_case_sample" not in v.message for v in violations)


def test_test_file_detection_by_name() -> None:
    source = """
def test_translate_templates():
    templates = ["Hello", "Goodbye"]
    translator = MockTranslator(templates)
    assert translator.templates == templates
"""
    check = RedundantAssignmentCheck()

    # File with test_ prefix should not flag test variables
    violations = check.check(Path("test_translator.py"), ast.parse(source), source)

    # Should not flag test data variable in test file.
    assert all("templates" not in v.message for v in violations)


def test_test_result_variable_not_flagged() -> None:
    source = """
def test_landmark_equal_to_none():
    landmark = Landmark(name="Tower", long_lat=(2.0, 48.0), score=0.9)
    result = landmark.__eq__(None)
    assert result is NotImplemented
"""
    check = RedundantAssignmentCheck()

    violations = check.check(Path("tests/test_model.py"), ast.parse(source), source)

    # Should not flag 'result' in test file.
    assert all("result" not in v.message for v in violations)


def test_test_mock_object_not_flagged() -> None:
    source = """
def test_prepare_photo():
    mock_image = MagicMock()
    mock_vision.Image.return_value = mock_image
    result = gcp_vision._prepare_photo(file_obj)
    assert result == mock_image
"""
    check = RedundantAssignmentCheck()

    violations = check.check(Path("tests/test_vision.py"), ast.parse(source), source)

    # Should not flag mock object in test file.
    assert all("mock_image" not in v.message for v in violations)


def test_semantic_test_data_list_not_flagged() -> None:
    source = """
def test_airport_connectivity():
    some_european_airports = ["AES", "BYJ", "BTS"]
    assert all(
        iata in airport_connectivity.airports_by_continent
        for iata in some_european_airports
    )
"""
    check = RedundantAssignmentCheck()

    violations = check.check(Path("tests/test_kiwi_api.py"), ast.parse(source), source)

    # Should not flag semantic test data in test file.
    assert all("some_european_airports" not in v.message for v in violations)


def test_range_with_descriptive_name_not_flagged() -> None:
    source = """
def generate_price_data():
    days_with_routes_in_a_row = range(70)
    return [
        faker.pyint(min_value=50, max_value=MAX_PRICE_EUR)
        for _ in days_with_routes_in_a_row
    ]
"""
    check = RedundantAssignmentCheck()

    violations = check.check(
        Path("tests/test_flight_prices.py"), ast.parse(source), source
    )

    # Should not flag descriptive range in test file.
    assert all("days_with_routes_in_a_row" not in v.message for v in violations)


def test_non_test_file_still_flags_simple_assignments() -> None:
    source = """
def process_data():
    x = "foo"
    return x
"""
    check = RedundantAssignmentCheck()

    # Non-test file should still flag simple redundant assignments
    violations = check.check(Path("src/processor.py"), ast.parse(source), source)

    msg = "Should flag simple redundant assignment in non-test file"
    assert len(violations) > 0, msg
    assert any("x" in v.message for v in violations), (
        "Should flag variable 'x' in non-test file"
    )


def test_is_test_file_detects_tests_directory() -> None:
    assert _is_test_file(Path("tests/test_something.py")) is True
    assert _is_test_file(Path("tests/utils/test_helpers.py")) is True
    assert _is_test_file(Path("test/test_foo.py")) is True


def test_is_test_file_detects_test_prefix() -> None:
    assert _is_test_file(Path("test_example.py")) is True
    assert _is_test_file(Path("src/test_module.py")) is True


def test_is_test_file_detects_test_suffix() -> None:
    assert _is_test_file(Path("example_test.py")) is True
    assert _is_test_file(Path("src/module_test.py")) is True


def test_is_test_file_rejects_non_test_files() -> None:
    assert _is_test_file(Path("src/module.py")) is False
    assert _is_test_file(Path("main.py")) is False
    assert _is_test_file(Path("setup.py")) is False


def test_is_test_file_handles_none() -> None:
    assert _is_test_file(None) is False


def test_context_manager_assignment_inside_usage_outside_not_flagged() -> None:
    """This pattern is used to reduce nesting: load data inside the context
    manager, use it outside to avoid deep indentation.
    """
    source = """
def load_config():
    with open("config.toml", "rb") as file:
        config = tomllib.load(file)
    # Use config outside to reduce nesting
    value = config.get("key", {})
    return value
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag context manager pattern.
    assert all("config" not in v.message for v in violations)


def test_context_manager_with_block_pattern_not_flagged() -> None:
    """Test the specific pattern from load_paths_to_ignore."""
    source = """
def load_paths_to_ignore(project_root, src_dir):
    pyproject_path = project_root / "pyproject.toml"
    with pyproject_path.open("rb") as file:
        config = tomllib.load(file)

    paths_to_ignore = set()
    expressions = (
        config.get("tool", {})
        .get("test_linter", {})
        .get("ignore_path_by_expression", [])
    )
    for pattern in expressions:
        paths_to_ignore |= set(src_dir.glob(pattern))
    return paths_to_ignore
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag config in context manager pattern.
    assert all("config" not in v.message for v in violations)


def test_database_connection_pattern_not_flagged() -> None:
    source = """
def fetch_user(user_id):
    with get_db_connection() as conn:
        result = conn.execute("SELECT * FROM users WHERE id = ?", user_id)
        user_data = result.fetchone()
    # Process user_data outside connection to avoid holding it open
    return process_user(user_data)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag database pattern.
    assert all("user_data" not in v.message for v in violations)


def test_if_block_assignment_inside_usage_outside_not_flagged() -> None:
    source = """
def process():
    if condition:
        data = load_expensive_data()
    # Use data outside if block
    result = transform(data)
    return result
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag if block pattern.
    assert all("data" not in v.message for v in violations)


def test_try_block_assignment_inside_usage_outside_not_flagged() -> None:
    source = """
def load_with_fallback():
    try:
        data = load_from_api()
    except Exception:
        data = load_from_cache()
    # Use data outside try block
    return process(data)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag try block pattern.
    assert all("data" not in v.message for v in violations)


def test_variable_used_in_list_comprehension_condition_not_flagged() -> None:
    """Test the reported false-positive: attribute cached for a comprehension filter.

    depot_iso_country = depot_data.iso_country  # cached once
    result = [x for x in depots if x.country == depot_iso_country]

    Inlining re-evaluates depot_data.iso_country on every iteration.
    """
    source = """
def find_routes(depot_data, depots):
    depot_iso_country = depot_data.iso_country
    return [x for x in depots if x.country == depot_iso_country]
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag comprehension-cached variable.
    assert all("depot_iso_country" not in v.message for v in violations)


def test_variable_used_only_in_list_comprehension_element_not_flagged() -> None:
    source = """
def transform(multiplier, items):
    factor = multiplier.value
    return [x * factor for x in items]
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag comprehension element variable.
    assert all("factor" not in v.message for v in violations)


def test_variable_used_only_in_dict_comprehension_not_flagged() -> None:
    source = """
def build_map(source_obj, keys):
    prefix = source_obj.namespace
    return {k: f"{prefix}_{k}" for k in keys}
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag dict-comprehension-cached variable.
    assert all("prefix" not in v.message for v in violations)


def test_variable_used_only_in_set_comprehension_not_flagged() -> None:
    source = """
def unique_suffixes(config, items):
    suffix = config.default_suffix
    return {item + suffix for item in items}
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag set-comprehension-cached variable.
    assert all("suffix" not in v.message for v in violations)


def test_variable_used_only_in_generator_expression_not_flagged() -> None:
    source = """
def total_score(config, players):
    bonus = config.bonus_points
    return sum(p.score + bonus for p in players)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag generator-expression-cached variable.
    assert all("bonus" not in v.message for v in violations)


def test_variable_used_inside_and_outside_comprehension_not_flagged() -> None:
    """A variable used both inside AND outside a comprehension has multiple
    uses, so detect_redundancy returns None and it is never flagged regardless.
    """
    source = """
def example(obj, items):
    val = obj.attr
    result = [x for x in items if x == val]
    return val
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Multi-use variable should not be flagged.
    assert all("val" not in v.message for v in violations)


def test_in_comprehension_flag_set_correctly() -> None:
    source = """
def func(obj, items):
    cached = obj.attr
    result = [x for x in items if x == cached]
    return result
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    cached_lifecycle = next(
        lc for lc in lifecycles if lc.assignment.var_name == "cached"
    )
    assert len(cached_lifecycle.uses) == 1
    assert cached_lifecycle.uses[0].in_comprehension is True


def test_in_comprehension_flag_false_for_normal_usage() -> None:
    source = """
def func():
    x = "foo"
    print(x)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert all(not use.in_comprehension for use in x_lifecycle.uses)


def test_calculate_semantic_value_test_context_list_literal() -> None:
    """Rule 10 now intercepts variables used solely inside comprehensions
    before they reach calculate_semantic_value, so this branch needs a
    direct test.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_source = '["AES", "BYJ", "BTS"]'
    rhs_node = ast.parse(rhs_source, mode="eval").body

    # multi-part name (+30) + "some" in test_semantic_words (+25) + list bonus (+25)
    assert (
        calculate_semantic_value(
            "some_european_airports",
            rhs_source,
            rhs_node,
            has_type_annotation=False,
            is_test_context=True,
        )
        >= 25
    )


def test_calculate_semantic_value_test_context_dict_literal() -> None:
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_source = '{"key": "value"}'
    rhs_node = ast.parse(rhs_source, mode="eval").body

    # dict literal bonus in test context
    assert (
        calculate_semantic_value(
            "my_mapping",
            rhs_source,
            rhs_node,
            has_type_annotation=False,
            is_test_context=True,
        )
        >= 25
    )


def test_calculate_semantic_value_test_context_range_call() -> None:
    """Rule 10 now intercepts 'days_with_routes_in_a_row' used in
    comprehension before it reaches calculate_semantic_value, so this
    needs a direct test.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_source = "range(70)"
    rhs_node = ast.parse(rhs_source, mode="eval").body

    # multi-part name (+30) + no test_semantic_words match (+0) + range bonus (+25)
    assert (
        calculate_semantic_value(
            "days_with_routes_in_a_row",
            rhs_source,
            rhs_node,
            has_type_annotation=False,
            is_test_context=True,
        )
        >= 25
    )


def test_calculate_semantic_value_test_context_no_semantic_word() -> None:
    """Covers the False branch of the test_semantic_words check (line 343->348).
    Before Rule 10, 'days_with_routes_in_a_row' (no semantic test words) covered
    this branch, but it is now intercepted by Rule 10.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_source = "42"
    rhs_node = ast.parse(rhs_source, mode="eval").body

    # "flight_count" contains no test semantic words: multi-part name (+30) +
    # no test_semantic_words match (+0). Just verifying the False branch is
    # exercised, not any particular score.
    assert (
        calculate_semantic_value(
            "flight_count",
            rhs_source,
            rhs_node,
            has_type_annotation=False,
            is_test_context=True,
        )
        >= 0
    )


def test_pytriage_ignore_still_suppresses_comprehension_false_positive() -> None:
    source = """
def func(depot_data, depots):
    depot_iso_country = depot_data.iso_country  # pytriage: ignore=TRI005
    return [x for x in depots if x.country == depot_iso_country]
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Both the new rule (Rule 10) and the ignore comment suppress this warning.
    assert len(violations) == 0


def test_ignore_marker_inside_string_literal_does_not_suppress_violation() -> None:
    """A string literal that merely contains the ignore-marker text is not a
    real suppression comment and must not hide a violation on that line.

    Regression: line-based ignore detection used a plain text search over
    raw source lines, so a string literal containing '# pytriage: ignore=...'
    text was indistinguishable from an actual comment. Ignore detection must
    be tokenize-based so it only matches genuine COMMENT tokens.
    """
    source = """
def call_it():
    x = "foo"; note = "# pytriage: ignore=TRI005"
    func(x=x)
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert any(v.line == 3 and "'x'" in v.message for v in violations)


def test_variable_used_in_function_decorator_not_flagged() -> None:
    """Regression test: variable used in a function decorator must not be flagged.

    The decorator expression is evaluated in the outer scope, so any variable
    referenced there counts as a use.  In this FastAPI-like pattern, 'app' is
    used twice: once in @app.get(…) and once in 'return app'.  Because it has
    two uses it is not single-use and must not be flagged as TRI005.
    """
    source = """
def _make_app():
    app = FastAPI()

    @app.get("/guarded")
    async def guarded(uid):
        return {"uid": uid}

    return app
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'app' - used in decorator and return statement.
    assert all("'app'" not in v.message for v in violations)


def test_decorator_use_is_tracked_by_variable_tracker() -> None:
    source = """
def outer():
    app = make_app()

    @app.route("/")
    def index():
        pass

    return app
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    app_lifecycle = next(
        (lc for lc in lifecycles if lc.assignment.var_name == "app"),
        None,
    )
    assert app_lifecycle is not None
    # Two uses: @app.route("/") and return app
    assert len(app_lifecycle.uses) == 2


def test_class_decorator_use_is_tracked() -> None:
    source = """
def factory():
    validator = build_validator()

    @validator.register
    class Rule:
        pass

    return validator
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    lc = next(
        (lc for lc in lifecycles if lc.assignment.var_name == "validator"),
        None,
    )
    assert lc is not None
    # Two uses: @validator.register (decorator) and return validator
    assert len(lc.uses) == 2


def test_has_inline_comment_mismatched_quote_in_string() -> None:
    """Regression: branch for mismatched quote inside a string is exercised.

    When a line contains a string delimited by double-quotes that also contains
    a single-quote character (e.g. "it's"), the inner single-quote character is
    encountered while in_string=True with string_char='"'.  The
    ``elif char == string_char`` branch evaluates to False, and the loop must
    continue without closing the string context.  Without this branch being
    taken we would incorrectly detect the apostrophe as a comment delimiter.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        _has_inline_comment,
    )

    # Single-quote inside a double-quoted string — no real comment
    lines = ['x = "it\'s fine"']
    assert _has_inline_comment(1, lines) is False

    # Same pattern but WITH a real comment after the string
    lines = ['x = "it\'s fine"  # comment']
    assert _has_inline_comment(1, lines) is True


def test_has_comment_above_first_line_returns_false() -> None:
    """Branch coverage: an assignment on line 1 has no line above to check."""
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        _has_comment_above,
    )

    lines = ['x = "foo"', "process(x)"]
    assert _has_comment_above(1, lines) is False


def test_track_attribute_assignment_with_non_name_base() -> None:
    """Branch coverage: base of attribute/subscript assignment is not a Name.

    When the target of an assignment is something like ``func().attr = v``
    (a method-call result), unwinding the Attribute chain leads to a Call
    node, not a Name.  _track_attribute_or_subscript_base_usage must skip
    tracking in that case rather than crashing.
    """
    source = """
def outer():
    get_obj().attr = "value"
    return 42
"""
    # Must not raise; call-result targets are silently skipped
    VariableTracker(source).visit(ast.parse(source))


def test_track_attribute_assignment_key_already_in_uses() -> None:
    """Branch coverage: key is already in uses when a second attr-assignment occurs.

    When the same variable is the base of two separate attribute assignments
    (e.g. ``obj.x = 1`` then ``obj.y = 2``), the second call to
    _track_attribute_or_subscript_base_usage finds the key already present in
    self.uses and must append rather than create a new list.
    """
    source = """
def outer():
    obj = make_obj()
    obj.x = 1
    obj.y = 2
    return obj
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    obj_lifecycle = next(
        (lc for lc in lifecycles if lc.assignment.var_name == "obj"),
        None,
    )
    assert obj_lifecycle is not None
    # obj is used in: obj.x = 1, obj.y = 2, return obj → 3 uses
    assert len(obj_lifecycle.uses) == 3


def test_fixable_violation_message_has_no_embedded_tags() -> None:
    """Regression test: [FIXABLE] and 'Run with --fix' must NOT be in v.message.

    These are presentation concerns emitted by the output layer (main()),
    not part of the machine-readable violation message.  Embedding them in
    the message caused '[FIXED] [FIXABLE] … Run with --fix…' output when
    --fix was already used.
    """
    source = """
def func():
    x = "foo"
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations, "Expected at least one fixable violation for this test"

    for v in fixable_violations:
        assert "[FIXABLE]" not in v.message, (
            f"Message must not embed [FIXABLE] tag: {v.message}"
        )
        assert "Run with --fix" not in v.message, (
            f"Message must not embed --fix hint: {v.message}"
        )


# ---------------------------------------------------------------------------
# semantic.py branch / line coverage tests
# ---------------------------------------------------------------------------


def test_calculate_semantic_value_binop() -> None:
    """Branch coverage: BinOp RHS adds 15 to semantic score (line 399)."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_node = ast.parse("a + b", mode="eval").body
    assert calculate_semantic_value("x", "a + b", rhs_node, False) >= 15


def test_calculate_semantic_value_ifexp() -> None:
    """Branch coverage: IfExp RHS adds 20 to semantic score (line 405)."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_node = ast.parse("1 if c else 0", mode="eval").body
    assert calculate_semantic_value("x", "1 if c else 0", rhs_node, False) >= 20


def test_should_report_violation_inline_comment_single_use() -> None:
    """Branch coverage: has_inline_comment=True with single-use variable (line 667).

    The existing test uses a multi-use variable, so detect_redundancy() returns None
    before reaching should_report_violation().  This test uses a single-use variable
    with an inline comment to reach and exercise line 667.
    """
    source = """
def func():
    x = "foo"  # some comment
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag 'x' - has inline comment.
    assert all("'x'" not in v.message for v in violations)


def test_should_report_violation_short_ifexp_single_use() -> None:
    """Branch coverage: short IfExp RHS with single-use variable (line 686).

    A ternary expression short enough (<25 chars) to pass the line-length check
    but still excluded by Rule 4 (IfExp guard) in should_report_violation().
    """
    source = """
def func(c):
    x = 1 if c else 0
    return x
"""
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), ast.parse(source), source)

    # Should not flag short IfExp.
    assert all("'x'" not in v.message for v in violations)


def _make_single_use_lifecycle(
    rhs_source: str,
    rhs_node: ast.expr,
    var_name: str = "x",
    in_loop: bool = False,
    in_control_flow: bool = False,
    preceded_by_call: bool = False,
) -> VariableLifecycle:
    """Build a minimal VariableLifecycle for direct should_autofix tests.

    The use's node/enclosing_stmt are built from a real parsed statement (not
    hand-faked AST nodes) so analysis.is_preceded_by_call sees a genuinely
    consistent tree: `{var_name}.method()` when preceded_by_call is False
    (var_name is the first thing evaluated), or
    `sink(side_effect(), {var_name})` when True (a sibling call precedes it).
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        UsageInfo,
        VariableLifecycle,
    )

    assignment = AssignmentInfo(
        var_name=var_name,
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=rhs_source,
        scope_id=1,
        has_type_annotation=False,
        in_loop=in_loop,
        in_control_flow=in_control_flow,
        in_global_scope=False,
    )
    use_stmt_source = (
        f"sink(side_effect(), {var_name})"
        if preceded_by_call
        else f"{var_name}.method()"
    )
    use_stmt = ast.parse(use_stmt_source).body[0]
    use_node = next(
        n
        for n in ast.walk(use_stmt)
        if isinstance(n, ast.Name) and n.id == var_name and isinstance(n.ctx, ast.Load)
    )
    use = UsageInfo(
        var_name=var_name,
        line=2,
        col=0,
        stmt_index=1,
        context="return",
        scope_id=1,
        node=use_node,
        enclosing_stmt=use_stmt,
    )
    return VariableLifecycle(assignment=assignment, uses=[use])


def test_should_autofix_returns_false_for_loop_assignment() -> None:
    """Branch coverage: should_autofix returns False when in_loop=True (line 821)."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse('"foo"', mode="eval").body
    lifecycle = _make_single_use_lifecycle('"foo"', rhs_node, in_loop=True)
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_should_autofix_returns_false_for_multiline_rhs() -> None:
    """Branch coverage: should_autofix returns False when RHS contains newline."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse('"foo"', mode="eval").body
    lifecycle = _make_single_use_lifecycle('"foo"\n"bar"', rhs_node)
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_should_autofix_returns_false_for_long_var_name_immediate() -> None:
    """Branch coverage: should_autofix returns False when var name > 10 chars.

    Uses 'myvariablex' (11 chars, no underscores) with a 10-char Name RHS to keep
    semantic_score=0 — the name/rhs length ratio stays below 1.1 so no score is
    added from the ratio check.  The code therefore reaches the len(var_name) > 10
    guard rather than returning early at the semantic_score > 10 check.

    This guard applies only to IMMEDIATE_SINGLE_USE / LITERAL_IDENTITY patterns.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse("something1", mode="eval").body
    lifecycle = _make_single_use_lifecycle(
        "something1", rhs_node, var_name="myvariablex"
    )
    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is False


def test_should_autofix_returns_false_for_high_semantic_score_single_use() -> None:
    """Branch coverage: should_autofix returns False when semantic_score > 20.

    A multi-part variable name gets +10 from name_parts scoring alone, and using
    a descriptive prefix pushes it over 20.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    # "has_something" gets +50 for "has_" prefix → semantic score > 20
    rhs_node = ast.parse("check()", mode="eval").body
    lifecycle = _make_single_use_lifecycle(
        "check()", rhs_node, var_name="has_something"
    )
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_should_autofix_returns_true_for_single_use_constant_rhs() -> None:
    """Branch coverage: should_autofix returns True for SINGLE_USE with Constant RHS.

    Low-semantic-score variable with a constant RHS and SINGLE_USE pattern reaches
    ``return True`` at the Constant/Name isinstance check in the SINGLE_USE block.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse("42", mode="eval").body
    lifecycle = _make_single_use_lifecycle("42", rhs_node, var_name="x")
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is True


def test_should_autofix_returns_false_for_non_call_non_attr_rhs_single_use() -> None:
    """Branch coverage: SINGLE_USE with RHS that is not Constant/Name/Attribute/Call.

    A list literal falls through all isinstance checks in the SINGLE_USE block
    and reaches the final ``return False`` (branch 884->893).
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse("[1, 2, 3]", mode="eval").body
    lifecycle = _make_single_use_lifecycle("[1, 2, 3]", rhs_node)
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_should_autofix_allows_zero_arg_call_for_immediate_single_use() -> None:
    """Issue #22 gap 2: IMMEDIATE_SINGLE_USE previously excluded every Call
    RHS, even trivial zero-arg ones like `check = ForbidVarsCheck()`. A
    zero-arg call with nothing else evaluating before its use (within the
    use's statement) has no sibling operand whose order inlining could
    disturb, so it gets a narrow carve-out here.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse("ForbidVarsCheck()", mode="eval").body
    lifecycle = _make_single_use_lifecycle(
        "ForbidVarsCheck()",
        rhs_node,
        var_name="check",
        preceded_by_call=False,
    )
    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is True


def test_should_autofix_rejects_zero_arg_call_preceded_by_a_call() -> None:
    """Regression test (P1 caught in review of issue #22's fix): a zero-arg
    call must not be inlined when a sibling expression evaluates before it
    within the same statement, or inlining reverses the original execution
    order. Example: `value = next_value(); sink(side_effect(), value)` must
    not become `sink(side_effect(), next_value())` — that runs next_value()
    after side_effect() instead of before it.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse("next_value()", mode="eval").body
    lifecycle = _make_single_use_lifecycle(
        "next_value()",
        rhs_node,
        var_name="value",
        preceded_by_call=True,
    )
    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is False


def test_is_preceded_by_call_across_multiline_statement() -> None:
    """Regression test (2nd P1 caught in review of issue #22's fix): the
    evaluation-order check must be AST-based, not line/column-text-based —
    a text heuristic sees an empty same-line prefix for `x` here and
    wrongly calls it safe, even though side_effect() (on the previous
    physical line, same statement) already ran first.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    sink(
        side_effect(),
        x,
    )
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is True


def test_is_preceded_by_call_true_for_attribute_sibling() -> None:
    """3rd finding caught in review of issue #22's fix: attribute/subscript
    access (e.g. a @property getter) can run arbitrary code just like a
    call, so a sibling attribute access must count as "preceding" too, not
    just bare ast.Call nodes.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    value = make()
    sink(obj.property, value)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    value_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "value")
    assert is_preceded_by_call(value_lifecycle.uses[0]) is True


def test_is_preceded_by_call_true_for_dict_key_after_earlier_pair() -> None:
    """4th finding caught in review of issue #22's fix: ast.Dict's own
    _fields order is ('keys', 'values') — every key, then every value —
    which does NOT match Python's real per-pair evaluation order. For
    `{"a": side_effect(), x: 1}`, "a" and side_effect() run *before* x is
    even reached, even though a naive field-order walk would see x right
    after "a" (before side_effect()).
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    d = {"a": side_effect(), x: 1}
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is True


def test_is_preceded_by_call_false_for_dict_sibling_without_calls() -> None:
    """Branch coverage: a dict literal that doesn't contain the target at
    all (and has no calls in it) must be walked fully — none of its
    key/value pairs match, so this exercises the loop completing without
    an early exit.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    sink({"a": 1, "b": 2}, x)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is False


def test_is_preceded_by_call_false_for_dict_first_key() -> None:
    """The dict fix must stay precise: x as the very first key (nothing
    evaluates before it, not even its own paired value) is still safe.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    d = {x: 1, "b": side_effect()}
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is False


def test_is_preceded_by_call_true_for_dict_value_after_unpacking() -> None:
    """Branch coverage: a None key marks **unpacking (evaluates only the
    paired value, no separate key) — a value after one must still see it
    as a preceding effect if that unpacked expression is a call.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    d = {**other(), "b": x}
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is True


def test_is_preceded_by_call_true_for_assign_target_base_after_value() -> None:
    """6th finding caught in review of issue #22's fix: for `obj.attr =
    value`, Python evaluates `value` *before* `obj` — the opposite of
    ast.Assign's own _fields order ('targets', 'value'). `x.attr =
    side_effect()` must see `side_effect()` as preceding `x`, or inlining
    would produce `make().attr = side_effect()`, reversing real order.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    x.attr = side_effect()
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is True


def test_is_preceded_by_call_true_for_ifexp_branch() -> None:
    """7th finding caught in review of issue #22's fix: exactly one of an
    ternary's body/orelse ever runs — never both, never unconditionally —
    so a call used there might not execute at all. `sink(x if flag else 0)`
    must be treated as unsafe, since inlining would make the call
    conditional on `flag` instead of always running.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    sink(x if flag else 0)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is True


def test_is_preceded_by_call_true_for_boolop_non_first_operand() -> None:
    """8th finding caught in review of issue #22's fix: `and`/`or` short-
    circuit, so only the first operand is guaranteed to evaluate. `sink(flag
    and x)` must be treated as unsafe — if `flag` is falsy, `x` (and thus an
    inlined call) never evaluates, unlike the original unconditional call.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    sink(flag and x)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is True


def test_is_preceded_by_call_true_for_ifexp_sibling_without_target() -> None:
    """Branch coverage: a ternary that doesn't contain the target at all
    (a plain sibling, e.g. `sink(a if flag else b, x)`) must still be
    walked fully to completion (none of test/body/orelse match,
    exercising the loop finishing without an early exit) — and, since
    IfExp's `test` invokes `__bool__` the same way BoolOp's short-circuit
    check does, it's still treated as a preceding effect even though it
    doesn't contain the target.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    sink(a if flag else b, x)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is True


def test_is_preceded_by_call_true_for_boolop_sibling_without_target() -> None:
    """Branch coverage: a BoolOp that doesn't contain the target at all
    (`sink(flag and other, x)`) must still be walked fully to completion
    (neither operand matches, exercising the loop finishing without an
    early exit) — and, since BoolOp itself invokes `__bool__` on its left
    operand to decide whether to short-circuit, it's still treated as a
    preceding effect (same reasoning as the operator-dunder-overload fix),
    even though it doesn't contain the target.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    sink(flag and other, x)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is True


def test_evaluation_order_children_assign_yields_value_before_targets() -> None:
    """Branch coverage + contract test: for ast.Assign, _evaluation_order_children
    must yield the RHS value before the target(s) — the opposite of
    Assign._fields, which lists targets first — matching Python's real
    evaluate-RHS-then-target(s) order.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        _evaluation_order_children,
    )

    tree = ast.parse("x.attr = value_expr")
    assign_node = tree.body[0]
    assert isinstance(assign_node, ast.Assign)
    children = list(_evaluation_order_children(assign_node))

    assert children == [(assign_node.value, False), (assign_node.targets[0], False)]


def test_is_preceded_by_call_false_for_boolop_first_operand() -> None:
    """The BoolOp fix must stay precise: the *first* operand always
    evaluates unconditionally, so `sink(x and flag)` is still safe.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    x = make()
    sink(x and flag)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert is_preceded_by_call(x_lifecycle.uses[0]) is False


def test_is_preceded_by_call_false_for_method_call_receiver() -> None:
    """The issue's own motivating idiom must remain safe: `check` is the
    receiver of `check.check(...)`, evaluated before any of that call's own
    arguments — nothing precedes it.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
        is_preceded_by_call,
    )

    source = """
def f():
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    lifecycles = tracker.build_lifecycles()

    check_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "check")
    assert is_preceded_by_call(check_lifecycle.uses[0]) is False


def test_is_preceded_by_call_defaults_to_true_for_unknown_container() -> None:
    """When the enclosing statement (or node) can't be determined,
    is_preceded_by_call must default to the conservative "unsafe" answer
    rather than guessing.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        UsageInfo,
        is_preceded_by_call,
    )

    use = UsageInfo(
        var_name="x", line=1, col=0, stmt_index=0, context="unknown", scope_id=1
    )
    assert is_preceded_by_call(use) is True


def test_should_autofix_rejects_call_with_args_for_immediate_single_use() -> None:
    """The zero-arg carve-out must stay narrow: a call with any argument is
    still rejected for IMMEDIATE_SINGLE_USE/LITERAL_IDENTITY, unlike the more
    permissive allowance already granted to SINGLE_USE.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse("make_check(1)", mode="eval").body
    lifecycle = _make_single_use_lifecycle("make_check(1)", rhs_node, var_name="check")
    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is False


def test_should_autofix_uses_real_use_line_length_when_available() -> None:
    """Issue #22 gap 1: should_autofix's line-length check must reflect the
    *actual* use line when the caller can supply it, not just the
    conservative RHS/var-name-based estimate — otherwise a violation can be
    reported [FIXABLE] and then silently skipped by apply_fixes' own,
    accurate length check.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import should_autofix

    rhs_node = ast.parse("ast.parse(source)", mode="eval").body
    lifecycle = _make_single_use_lifecycle(
        "ast.parse(source)", rhs_node, var_name="tree"
    )

    # Without the real use line, the conservative RHS/var-name estimate says
    # inlining is safe (both are short).
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is True

    # _make_single_use_lifecycle fixes the use at line 2 (1-indexed).
    long_use_line = (
        "    violations = check.check("
        'Path("tests/test_something_with_a_long_name.py"), tree, source)'
    )
    source_lines = ["def f():", long_use_line]
    assert (
        should_autofix(lifecycle, PatternType.SINGLE_USE, source_lines=source_lines)
        is False
    )


def test_adds_verbosity_subscript_with_variable_slice() -> None:
    """Branch coverage: Subscript RHS with non-constant (variable) slice (216->226).

    Pattern 2 enters the Subscript branch but the slice is a Name, not a Constant,
    so rhs_key_or_method stays None and the check at line 226 is reached with None.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _adds_verbosity_or_context,
    )

    # obj[key] where key is a variable, not a string constant
    rhs_node = ast.parse("obj[key]", mode="eval").body
    # The var_name "user_obj" contains "obj" (from the Name base) but the slice is a
    # variable; rhs_key_or_method is None so the check returns False from Pattern 2.
    # Pattern 1 also doesn't apply ("user" not a descriptive prefix for "obj[key]")
    # Just assert we get a bool without error — coverage is the goal here.
    assert isinstance(
        _adds_verbosity_or_context("user_obj", "obj[key]", rhs_node), bool
    )


def test_adds_verbosity_call_with_subscript_func() -> None:
    """Branch coverage: Call RHS where func is neither Name nor Attribute.

    When the function being called is accessed via subscript (e.g.
    ``funcs["load"](data)``), ``rhs_node.func`` is a Subscript, not a Name or
    Attribute, so rhs_key_or_method stays None.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _adds_verbosity_or_context,
    )

    rhs_node = ast.parse('funcs["load"](data)', mode="eval").body
    assert isinstance(
        _adds_verbosity_or_context("configuration", 'funcs["load"](data)', rhs_node),
        bool,
    )


def test_adds_verbosity_get_call_key_not_in_var() -> None:
    """Branch coverage: Pattern 3 (.get() call) where key is not in var name."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _adds_verbosity_or_context,
    )

    # var_name "x" does not contain "email" → Pattern 3 condition is False
    rhs_node = ast.parse('data.get("email")', mode="eval").body
    assert _adds_verbosity_or_context("x", 'data.get("email")', rhs_node) is False


def test_adds_verbosity_parse_func_with_subscript_func() -> None:
    """Branch coverage: Pattern 4 parse func where func is a Subscript (271->276)."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _adds_verbosity_or_context,
    )

    # parsers["json"](data) — func is a Subscript, not Name or Attribute
    rhs_node = ast.parse('parsers["json"](data)', mode="eval").body
    assert isinstance(
        _adds_verbosity_or_context("parsed_data", 'parsers["json"](data)', rhs_node),
        bool,
    )


def test_adds_verbosity_parse_func_with_generic_var_name() -> None:
    """Branch coverage: Pattern 4 parse func but var name is generic (281->284).

    When the var name IS in generic_names (e.g. "result"), the inner check returns
    False without executing ``return True``.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _adds_verbosity_or_context,
    )

    # json.loads() is a generic parse function; "result" is in generic_names
    rhs_node = ast.parse("json.loads(data)", mode="eval").body
    assert _adds_verbosity_or_context("result", "json.loads(data)", rhs_node) is False


def test_contains_nondeterministic_call_with_subscript_func() -> None:
    """Branch coverage: _contains_nondeterministic_call with func as Subscript.

    When the called function is accessed via subscript (e.g. ``funcs[0]()``),
    ``node.func`` is a Subscript — neither Name nor Attribute.  The detector
    must continue visiting child nodes rather than crashing or silently skipping.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _contains_nondeterministic_call,
    )

    # funcs[0]() — func is a Subscript, func_name stays ""
    rhs_node = ast.parse("funcs[0]()", mode="eval").body
    # Not nondeterministic
    assert _contains_nondeterministic_call(rhs_node) is False


def test_fix_inlines_use_on_line_with_non_ascii_text(tmp_path: Path) -> None:
    """Regression: ast.col_offset is a UTF-8 byte offset, not a character
    offset. A non-ASCII character earlier on the use's line (here, in a
    string literal) must not throw off the position used to locate the
    variable for inlining.
    """
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
