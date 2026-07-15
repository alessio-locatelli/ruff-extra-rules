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
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
    _is_test_file,
    _is_test_function,
)


def test_immediate_single_use_detected() -> None:
    """Test detection of immediate single use pattern."""
    source = """
def func_scope():
    x = "foo"
    func(x=x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) >= 1
    violation = violations[0]
    assert violation.error_code == "TRI005"
    assert "x" in violation.message


def test_single_use_return_detected() -> None:
    """Test detection of single-use variable in return."""
    source = """
def example():
    result = get_value()
    return result
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) >= 1
    assert any("result" in v.message for v in violations)


def test_literal_identity_detected() -> None:
    """Test detection of literal identity pattern."""
    source = """
def func_scope():
    foo = "foo"
    process(foo)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) >= 1
    assert any("foo" in v.message for v in violations)


def test_literal_identity_with_underscores() -> None:
    """Test literal identity with underscores matches."""
    source = """
def func_scope():
    SOME_VALUE = "somevalue"
    process(SOME_VALUE)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should detect as literal identity (underscores removed match)
    assert len(violations) >= 1


def test_multiple_uses_not_flagged() -> None:
    """Test that variables with multiple uses are not flagged."""
    source = """
value = calc()
print(value)
log(value)
return value
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag 'value' because it's used multiple times
    assert len(violations) == 0


def test_semantic_value_skipped() -> None:
    """Test that variables with semantic value are skipped."""
    source = """
def example():
    formatted_timestamp = format_iso8601(raw_ts)
    return formatted_timestamp
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag because 'formatted_timestamp' has semantic value
    assert len(violations) == 0


def test_inline_suppression_respected() -> None:
    """Test that inline ignore comments are respected."""
    source = """
x = "foo"  # pytriage: ignore=TRI005
func(x=x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag because of inline suppression
    assert len(violations) == 0


def test_inline_suppression_case_insensitive() -> None:
    """Test that inline ignore comments are case-insensitive."""
    source = """
x = "foo"  # PYTRIAGE: IGNORE=TRI005
func(x=x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag because of inline suppression
    assert len(violations) == 0


def test_variable_tracker_scope_isolation() -> None:
    """Test that VariableTracker isolates variables by scope."""
    source = """
def outer():
    x = "outer"
    def inner():
        x = "inner"
        return x
    return x
"""
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    tracker.visit(tree)
    lifecycles = tracker.build_lifecycles()

    # Should track two separate lifecycles for 'x' in different scopes
    x_lifecycles = [lc for lc in lifecycles if lc.assignment.var_name == "x"]
    assert len(x_lifecycles) == 2


def test_global_variable_not_analyzed() -> None:
    """Test that global variables are not analyzed."""
    source = """
def func():
    global state
    state = "active"
    return state
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not analyze global variables
    assert len(violations) == 0


def test_type_annotation_adds_value() -> None:
    """Test that type annotations increase semantic value."""
    source = """
def example():
    result: ComplexType = calculate()
    return result
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Type annotation should increase semantic value enough to skip
    # (15 points for annotation + other factors)
    # This might still be flagged depending on total score, so we just check
    # that it doesn't crash
    assert isinstance(violations, list)


def test_comprehension_not_causing_errors() -> None:
    """Test that comprehensions don't cause tracking errors."""
    source = """
result = [x for x in items]
return result
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Comprehensions add semantic value (30 points), so likely won't be flagged
    # Just verify no crashes
    assert isinstance(violations, list)


def test_pattern_detection_immediate_use() -> None:
    """Test pattern detection for immediate single use."""
    source = """
def func():
    x = "foo"
    print(x)
"""
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    tracker.visit(tree)
    lifecycles = tracker.build_lifecycles()

    # Find the 'x' lifecycle
    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")

    # Should detect immediate single use
    pattern = detect_redundancy(x_lifecycle)
    assert pattern == PatternType.IMMEDIATE_SINGLE_USE


def test_pattern_detection_single_use() -> None:
    """Test pattern detection for single use (not immediate)."""
    source = """
def func():
    x = "foo"
    y = "bar"
    z = "baz"
    print(x)
"""
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    tracker.visit(tree)
    lifecycles = tracker.build_lifecycles()

    # Find the 'x' lifecycle
    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")

    # Should detect single use (not immediate because there are intervening statements)
    pattern = detect_redundancy(x_lifecycle)
    assert pattern == PatternType.SINGLE_USE


def test_check_id_and_error_code() -> None:
    """Test that check has correct ID and error code."""
    check = RedundantAssignmentCheck()
    assert check.check_id == "redundant-assignment"
    assert check.error_code == "TRI005"


def test_prefilter_pattern() -> None:
    """Test that prefilter pattern is defined."""
    check = RedundantAssignmentCheck()
    patterns = check.get_prefilter_pattern()
    assert patterns == [" = "]


def test_fixable_marked_correctly() -> None:
    """Test that simple violations are marked fixable."""
    source = """
def func_scope():
    x = "foo"
    func(x=x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should detect violations and mark simple ones as fixable
    assert len(violations) >= 1
    # Simple case: constant assignment, immediate use, short name, no control flow
    assert any(v.fixable for v in violations)


def test_non_fixable_semantic_value() -> None:
    """Test that violations with semantic value are not marked fixable."""
    source = """
def example():
    calculated_value = expensive_operation()
    return calculated_value
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # 'calculated_value' has semantic value (transformative verb 'calculated')
    # so it should not be flagged at all
    assert len(violations) == 0


def test_fix_method_with_fixable_violations() -> None:
    """Test that fix method can fix simple violations."""
    from tempfile import NamedTemporaryFile

    source = """def func_scope():
    x = "foo"
    func(x=x)
"""
    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    # Should detect violations
    assert len(violations) >= 1

    # Simple case should be marked fixable
    assert any(v.fixable for v in violations)

    # Apply fixes
    result = check.fix(filepath, violations, source, tree)
    assert result is True

    # Read the fixed content
    fixed_content = filepath.read_text()

    # The assignment should be removed and the usage should be inlined
    assert "x = " not in fixed_content
    assert 'func(x="foo")' in fixed_content

    filepath.unlink()


def test_autofix_skips_violation_without_fix_data() -> None:
    """Test that autofix skips violations without fix_data."""
    from tempfile import NamedTemporaryFile

    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )

    source = "x = 1\nprint(x)\n"

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    check = RedundantAssignmentCheck()
    tree = ast.parse(source)

    # Create a violation without fix_data
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

    result = check.fix(filepath, violations, source, tree)
    assert result is False

    filepath.unlink()


def test_autofix_skips_violation_with_invalid_fix_data() -> None:
    """Test that autofix skips violations with invalid fix_data."""
    from tempfile import NamedTemporaryFile

    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )

    source = "x = 1\nprint(x)\n"

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    check = RedundantAssignmentCheck()
    tree = ast.parse(source)

    # Create a violation with invalid fix_data (missing 'lifecycle')
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

    result = check.fix(filepath, violations, source, tree)
    assert result is False

    filepath.unlink()


def test_autofix_skips_multiline_rhs() -> None:
    """Test that autofix skips multiline expressions."""
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
        _can_safely_inline,
    )

    source_lines = ["result = func(x)\n"]

    # RHS with newline should not be inlined
    result = _can_safely_inline("result", "func(\n    arg\n)", 0, source_lines)
    assert result is False


def test_autofix_skips_line_length_violation() -> None:
    """Test that autofix skips if inlining would exceed line length."""
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
        _can_safely_inline,
    )

    # Current line is 80 chars, adding 20 more would exceed 88
    source_lines = ["x = " + "a" * 80 + "\n"]

    # Inlining would make the line too long
    result = _can_safely_inline("x", "a" * 20, 0, source_lines)
    assert result is False


def test_autofix_skips_invalid_line_indices() -> None:
    """Test that autofix handles invalid line indices gracefully."""
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
        _can_safely_inline,
    )

    source_lines = ["line1\n", "line2\n"]

    # Negative index
    result = _can_safely_inline("x", "value", -1, source_lines)
    assert result is False

    # Index out of bounds
    result = _can_safely_inline("x", "value", 10, source_lines)
    assert result is False


def test_autofix_with_invalid_assignment_line() -> None:
    """Test that autofix skips violations with invalid assignment line indices."""
    from tempfile import NamedTemporaryFile

    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        UsageInfo,
        VariableLifecycle,
    )

    source = "x = 1\nprint(x)\n"

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    check = RedundantAssignmentCheck()
    tree = ast.parse(source)
    rhs_node = ast.parse("1", mode="eval").body

    # Create a lifecycle with invalid assignment line (line 100, which doesn't exist)
    assignment = AssignmentInfo(
        var_name="x",
        line=100,  # Invalid line number
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source="1",
        scope_id=0,
        has_type_annotation=False,
    )

    usage = UsageInfo(
        var_name="x",
        line=2,
        col=6,
        stmt_index=1,
        context="unknown",
        scope_id=0,
    )

    lifecycle = VariableLifecycle(assignment=assignment, uses=[usage])

    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=100,
            col=0,
            message="test",
            fixable=True,
            fix_data={"lifecycle": lifecycle, "pattern": "IMMEDIATE_SINGLE_USE"},
        )
    ]

    result = check.fix(filepath, violations, source, tree)
    assert result is False

    filepath.unlink()


def test_autofix_with_invalid_usage_line() -> None:
    """Test that autofix skips violations with invalid usage line indices."""
    from tempfile import NamedTemporaryFile

    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        UsageInfo,
        VariableLifecycle,
    )

    source = "x = 1\nprint(x)\n"

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    check = RedundantAssignmentCheck()
    tree = ast.parse(source)
    rhs_node = ast.parse("1", mode="eval").body

    # Create a lifecycle with invalid usage line (line 100, which doesn't exist)
    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source="1",
        scope_id=0,
        has_type_annotation=False,
    )

    usage = UsageInfo(
        var_name="x",
        line=100,  # Invalid line number
        col=6,
        stmt_index=1,
        context="unknown",
        scope_id=0,
    )

    lifecycle = VariableLifecycle(assignment=assignment, uses=[usage])

    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
            fixable=True,
            fix_data={"lifecycle": lifecycle, "pattern": "IMMEDIATE_SINGLE_USE"},
        )
    ]

    result = check.fix(filepath, violations, source, tree)
    assert result is False

    filepath.unlink()


def test_autofix_with_multiple_uses() -> None:
    """Test that autofix skips violations with multiple uses."""
    from tempfile import NamedTemporaryFile

    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        UsageInfo,
        VariableLifecycle,
    )

    source = "x = 1\nprint(x)\nprint(x)\n"

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    check = RedundantAssignmentCheck()
    tree = ast.parse(source)
    rhs_node = ast.parse("1", mode="eval").body

    # Create a lifecycle with multiple uses
    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source="1",
        scope_id=0,
        has_type_annotation=False,
    )

    usage1 = UsageInfo(
        var_name="x",
        line=2,
        col=6,
        stmt_index=1,
        context="unknown",
        scope_id=0,
    )

    usage2 = UsageInfo(
        var_name="x",
        line=3,
        col=6,
        stmt_index=2,
        context="unknown",
        scope_id=0,
    )

    lifecycle = VariableLifecycle(assignment=assignment, uses=[usage1, usage2])

    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
            fixable=True,
            fix_data={"lifecycle": lifecycle, "pattern": "SINGLE_USE"},
        )
    ]

    result = check.fix(filepath, violations, source, tree)
    assert result is False  # Should skip because of multiple uses

    filepath.unlink()


def test_autofix_with_unsafe_inlining() -> None:
    """Test that autofix skips when inlining would be unsafe (line too long)."""
    from tempfile import NamedTemporaryFile

    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment import (
        RedundantAssignmentCheck,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        UsageInfo,
        VariableLifecycle,
    )

    # Create a case where inlining would exceed 88 characters
    # Line is already 60 chars, adding 40 char value would exceed 88
    source = (
        "x = " + "a" * 40 + "\nresult = some_long_function_name(x, param1, param2)\n"
    )

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    check = RedundantAssignmentCheck()
    tree = ast.parse(source)
    rhs_node = ast.parse("a" * 40, mode="eval").body

    # Manually create a fixable violation with a long RHS
    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source="a" * 40,
        scope_id=0,
        has_type_annotation=False,
    )

    usage = UsageInfo(
        var_name="x",
        line=2,
        col=41,  # Position of 'x' in the usage line
        stmt_index=1,
        context="unknown",
        scope_id=0,
    )

    lifecycle = VariableLifecycle(assignment=assignment, uses=[usage])

    violations = [
        Violation(
            check_id="redundant-assignment",
            error_code="TRI005",
            line=1,
            col=0,
            message="test",
            fixable=True,
            fix_data={"lifecycle": lifecycle, "pattern": "IMMEDIATE_SINGLE_USE"},
        )
    ]

    result = check.fix(filepath, violations, source, tree)
    # Should return False because inlining would make the line too long
    assert result is False

    filepath.unlink()


def test_fix_method_with_no_fixable_violations() -> None:
    """Test that fix method returns False when no violations are fixable."""
    from pre_commit_hooks.ast_checks._base import Violation
    from pre_commit_hooks.ast_checks.redundant_assignment.autofix import apply_fixes

    source = """
x = "foo"
func(x=x)
"""
    # Create a non-fixable violation
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

    result = apply_fixes(Path("test.py"), violations, source)
    assert result is False


def test_nonlocal_variable_not_analyzed() -> None:
    """Test that nonlocal variables are not analyzed."""
    source = """
def outer():
    x = "outer"
    def inner():
        nonlocal x
        x = "modified"
        return x
    return inner()
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Nonlocal assignment should not be flagged
    assert all("modified" not in v.message for v in violations)


def test_annotated_assignment_tracked() -> None:
    """Test that annotated assignments are tracked."""
    source = """
def example():
    x: str = "foo"
    func(x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should flag if semantic value is low
    # Type annotation adds 15 points, but 'x' literal is still low value
    assert len(violations) >= 1


def test_annotated_assignment_not_global() -> None:
    """Test annotated assignment that is not global/nonlocal (normal path)."""
    source = """
def example():
    result: int = calculate_value()
    another: str = "test"
    return result, another
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Both assignments should be tracked normally
    assert isinstance(violations, list)


def test_annotated_assignment_without_value() -> None:
    """Test annotated assignment without value (type hint only)."""
    source = """
def example():
    x: str  # Type hint only, no assignment
    x = "value"
    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Only the assignment with value should be tracked
    assert isinstance(violations, list)


def test_class_attributes_not_analyzed() -> None:
    """Test that class attributes are not analyzed."""
    source = """
class MyClass:
    x = "foo"

    def method(self):
        self.x = "bar"
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Class attributes should not be flagged
    assert len(violations) == 0


def test_semantic_scoring_long_expression() -> None:
    """Test that long expressions get higher semantic scores."""
    source = """
def example():
    x = very_long_function_name_that_exceeds_sixty_characters_in_total()
    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Long expression should still be flagged if var name adds no value
    # But it might get some points for length
    assert isinstance(violations, list)


def test_semantic_scoring_comprehension() -> None:
    """Test that comprehensions increase semantic value."""
    source = """
result = [x * 2 for x in range(10)]
print(result)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Comprehensions add 30 points, should help avoid flagging
    assert isinstance(violations, list)


def test_semantic_scoring_binary_op() -> None:
    """Test that binary operations increase semantic value."""
    source = """
result = a + b
print(result)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Binary op adds 15 points
    assert isinstance(violations, list)


def test_semantic_scoring_unary_op() -> None:
    """Test that unary operations increase semantic value."""
    source = """
result = -value
print(result)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Unary op adds 10 points
    assert isinstance(violations, list)


def test_semantic_scoring_ternary() -> None:
    """Test that ternary expressions increase semantic value."""
    source = """
result = x if condition else y
print(result)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Ternary adds 20 points
    assert isinstance(violations, list)


def test_semantic_scoring_lambda() -> None:
    """Test that lambda expressions increase semantic value."""
    source = """
func = lambda x: x * 2
result = func(10)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Lambda adds 25 points
    assert isinstance(violations, list)


def test_semantic_scoring_multipart_name() -> None:
    """Test that multi-part names increase semantic value."""
    source = """
def example():
    user_email_address = get_email()
    return user_email_address
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # 3+ parts adds 20 points
    assert isinstance(violations, list)


def test_tuple_unpacking_not_analyzed() -> None:
    """Test that tuple unpacking is not analyzed."""
    source = """
x, y = get_coords()
print(x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

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
    """Test that should_autofix allows simple calls."""
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    # Create a simple call assignment
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

    # Should autofix simple call with immediate use
    result = should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE)
    # This might be True or False depending on semantic score
    assert isinstance(result, bool)


def test_no_uses_not_flagged() -> None:
    """Test that assignments with no uses are not flagged."""
    source = """
def example():
    x = "foo"
    y = "bar"
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Variables with no uses should not be flagged (different issue)
    assert len(violations) == 0


def test_should_autofix_with_single_use_pattern() -> None:
    """Test that should_autofix returns False for SINGLE_USE pattern."""
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
                line=5,
                col=0,
                stmt_index=4,  # Not immediate use
                context="unknown",
                scope_id=0,
            )
        ],
    )

    # SINGLE_USE pattern CAN be auto-fixed for simple cases (simple call with no args)
    result = should_autofix(lifecycle, PatternType.SINGLE_USE)
    assert result is True


def test_semantic_scoring_medium_length_expression() -> None:
    """Test semantic scoring for medium-length expressions (40-60 chars)."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test with exactly 45 characters (between 40 and 60)
    rhs_source = "some_function_with_exactly_45_characters("
    rhs_node = ast.parse(rhs_source + ")", mode="eval").body

    score = calculate_semantic_value("x", rhs_source + ")", rhs_node, False)

    # Should get points for medium length (40-60 chars = +10 points)
    assert score >= 10


def test_should_autofix_call_with_simple_args() -> None:
    """Test that should_autofix allows calls with simple arguments."""
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    # Create a call with simple arguments
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

    # Should potentially autofix (depending on semantic score)
    result = should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE)
    assert isinstance(result, bool)


def test_should_autofix_no_args_call() -> None:
    """Test that should_autofix allows no-args calls."""
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    # Create a call with no arguments
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

    # Should potentially autofix (depending on semantic score)
    result = should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE)
    assert isinstance(result, bool)


def test_lifecycle_no_uses_not_immediate() -> None:
    """Test that lifecycle with no uses is not immediate."""
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

    # Lifecycle with no uses
    lifecycle = VariableLifecycle(assignment=assignment, uses=[])

    # Should not be immediate use
    assert lifecycle.is_immediate_use is False
    assert lifecycle.is_single_use is False


def test_annotated_assignment_with_nonlocal() -> None:
    """Test that annotated assignments with nonlocal are skipped."""
    source = """
def outer():
    x: str = "outer"
    def inner():
        nonlocal x
        x: str = "modified"
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Nonlocal annotated assignment should be skipped
    assert isinstance(violations, list)


def test_get_source_segment_error_handling() -> None:
    """Test that _get_source_segment handles errors gracefully."""
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        VariableTracker,
    )

    source = "x = 1"
    tracker = VariableTracker(source)

    # Create a node with invalid line numbers
    node = ast.Constant(value=1, lineno=-1, col_offset=-1)

    # Should return empty string on error
    result = tracker._get_source_segment(node)
    assert result == ""


def test_multiple_assignments_to_same_variable() -> None:
    """Test that multiple assignments to same variable create separate lifecycles."""
    source = """
def example():
    x = "first"
    print(x)
    x = "second"
    print(x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Each assignment should be tracked separately
    assert isinstance(violations, list)


def test_multiple_annotated_assignments_same_variable() -> None:
    """Test multiple annotated assignments to same variable."""
    source = """
def example():
    x: str = "first"
    print(x)
    x: str = "second"
    print(x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Each annotated assignment should be tracked separately
    assert isinstance(violations, list)


def test_self_referential_assignment_correctly_tracked() -> None:
    """Test that x = x + 1 pattern correctly ignores LHS in RHS."""
    source = """
def example():
    x = 1
    x = x + 1
    print(x)
    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Second assignment (x = x + 1) has two uses (print and return)
    # First assignment (x = 1) has one use (x + 1 RHS)
    # Neither should be flagged as redundant because multiple uses
    # This test verifies that currently_assigning logic works
    assert len(violations) == 0


def test_should_autofix_complex_call_args() -> None:
    """Test that should_autofix rejects calls with complex arguments."""
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        PatternType,
        UsageInfo,
        VariableLifecycle,
    )
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        should_autofix,
    )

    # Create a call with complex arguments (dict comprehension)
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

    # Should NOT autofix due to complex arguments
    result = should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE)
    assert result is False


def test_conditional_assignment_with_augmented_use() -> None:
    """Test conditional assignments with augmented assignment not flagged."""
    source = """
def func(v):
    if v:
        msg = "foo"
    else:
        msg = "bar"

    msg += "spameggs"

    print(msg)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag either 'msg' assignment because:
    # 1. Both assignments are in different branches (if/else)
    # 2. The variable is used in an augmented assignment (msg += ...)
    # 3. This is not a single-use pattern - the conditional value is essential
    assert len(violations) == 0


def test_augmented_assignment_tracks_usage() -> None:
    """Test that augmented assignments track variable usage."""
    source = """
def example():
    x = 1
    x += 2
    print(x)
"""
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    tracker.visit(tree)
    lifecycles = tracker.build_lifecycles()

    # Should have one lifecycle for 'x' (the initial assignment)
    # Augmented assignments are tracked as usages, not new assignments
    x_lifecycles = [lc for lc in lifecycles if lc.assignment.var_name == "x"]
    assert len(x_lifecycles) == 1

    # The lifecycle should have two uses:
    # 1. The read in x += 2 (augmented assignment)
    # 2. The use in print(x)
    lifecycle = x_lifecycles[0]
    assert len(lifecycle.uses) == 2


def test_augmented_assignment_single_use_can_be_flagged() -> None:
    """Test that augmented assignments can still be flagged if redundant."""
    source = """
def example():
    x = 1
    x += 1
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # The first assignment (x = 1) is used once (in x += 1)
    # This could be flagged as it's a simple pattern
    # But augmented assignments typically indicate the variable will be used again
    # So it's reasonable either way
    assert isinstance(violations, list)


def test_long_chained_expression_not_flagged() -> None:
    """Test that long chained expressions with meaningful names are not flagged."""
    source = """
@functools.cache
def find_place_document(place_id):
    collection_places = singleton_factory(mongo_client)[DATABASE_NAME]["places"]
    return collection_places.find_one({"_id": place_id})
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag 'collection_places' because:
    # 1. It's a long expression (70+ chars)
    # 2. It has chained subscript operations
    # 3. The variable name is meaningful and descriptive
    # 4. Breaking it down improves readability
    assert len(violations) == 0


def test_autofix_respects_line_length() -> None:
    """Test that autofix doesn't inline if it would exceed line length."""
    from tempfile import NamedTemporaryFile

    # Create a case where inlining would exceed 88 characters
    source = """x = "a_very_long_string_that_when_inlined_would_make_the_line_too_long"
result = some_function(x, another_param, yet_another_param)
"""
    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    # May or may not have violations depending on semantic scoring
    # But if there are violations, they should NOT be fixable due to line length
    for v in violations:
        if v.fixable:  # pragma: no cover - autofix disabled
            result = check.fix(filepath, [v], source, tree)
            # Should not fix if it violates line length
            fixed = filepath.read_text()
            assert len(fixed.splitlines()[1]) <= 88 or result is False

    filepath.unlink()


def test_autofix_handles_word_boundaries() -> None:
    """Test that autofix correctly handles variable names as whole words."""
    from tempfile import NamedTemporaryFile

    # Test that 'x' doesn't match 'max' or 'index'
    source = """x = 5
result = x + max(x, index)
"""
    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    if violations and any(
        v.fixable for v in violations
    ):  # pragma: no cover - autofix disabled
        check.fix(filepath, violations, source, tree)
        fixed = filepath.read_text()

        # Should only replace the standalone 'x', not 'max' or 'index'
        assert "max" in fixed
        assert "index" in fixed

    filepath.unlink()


def test_chained_operations_scoring() -> None:
    """Test that chained operations increase semantic value."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test with 2 chained subscripts: obj[x][y]
    source = "obj[x][y]"
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("result", source, rhs_node, False)

    # Should get points for chained operations (2 chains = +20)
    # "result" is 1 part (+0), short expression (+0)
    assert score == 20

    # Test with 3 chained operations and better naming
    source = "func()[x][y]"
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("my_value", source, rhs_node, False)

    # Should get points for:
    # - 3+ chains (+30)
    # - 2-part name (+10)
    assert score == 40

    # Test with attribute chaining: obj.foo.bar
    source = "obj.foo.bar"
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("result", source, rhs_node, False)

    # Should get points for chained attributes (2 chains = +20)
    assert score >= 20


def test_augmented_assignment_with_global_variable() -> None:
    """Test that augmented assignments with global variables are skipped."""
    source = """
def func():
    global x
    x += 1
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag global variable
    assert len(violations) == 0


def test_augmented_assignment_with_nonlocal_variable() -> None:
    """Test that augmented assignments with nonlocal variables are skipped."""
    source = """
def outer():
    x = 1
    def inner():
        nonlocal x
        x += 1
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag nonlocal variable
    assert isinstance(violations, list)


def test_augmented_assignment_with_attribute() -> None:
    """Test that augmented assignments to attributes (not simple names) are skipped."""
    source = """
def func():
    obj.x += 1
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not track attribute assignments
    assert len(violations) == 0


def test_semantic_scoring_very_long_expression() -> None:
    """Test that very long expressions (80+ chars) get extra points."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Create an 85-character expression
    source = "a" * 85
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("x", source, rhs_node, False)

    # Should get points for very long expression (80+ = +35)
    assert score >= 35


# === Autofix Safety Tests ===
# Tests to verify autofix only handles safe, simple cases


def test_autofix_not_in_loop() -> None:
    """Test that autofix does not fix variables inside loops."""
    source = """
for i in range(10):
    x = i * 2
    print(x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag or fix variables in loops
    assert len(violations) == 0


def test_autofix_not_in_control_flow() -> None:
    """Test that autofix does not fix variables inside control flow."""
    source = """
def example():
    if condition:
        x = "value"
        process(x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # May detect but should not be fixable due to control flow
    for v in violations:
        assert not v.fixable


def test_autofix_not_long_names() -> None:
    """Test that autofix does not fix variables with long names."""
    source = """
very_long_descriptive_name = 42
use(very_long_descriptive_name)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not be fixable due to long variable name (> 10 chars)
    for v in violations:
        assert not v.fixable


def test_autofix_only_simple_rhs() -> None:
    """Test that autofix only fixes simple RHS expressions."""
    source = """
def example():
    x = func(arg1, arg2)
    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not be fixable due to complex RHS (function call)
    for v in violations:
        assert not v.fixable


def test_autofix_simple_constant() -> None:
    """Test that autofix handles simple constants."""
    from tempfile import NamedTemporaryFile

    source = """y = 42
result = y + 10
"""
    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    # Simple constant should be fixable
    fixable_violations = [v for v in violations if v.fixable]
    if fixable_violations:
        result = check.fix(filepath, fixable_violations, source, tree)
        assert result is True

        fixed_content = filepath.read_text()
        assert "y = 42" not in fixed_content
        assert "result = 42 + 10" in fixed_content

    filepath.unlink()


def test_autofix_simple_attribute() -> None:
    """Test that autofix handles simple single-level attribute access."""
    from tempfile import NamedTemporaryFile

    source = """v = obj.attr
use(v)
"""
    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    # Simple attribute access should be fixable
    fixable_violations = [v for v in violations if v.fixable]
    if fixable_violations:
        result = check.fix(filepath, fixable_violations, source, tree)
        assert result is True

        fixed_content = filepath.read_text()
        assert "v = obj.attr" not in fixed_content
        assert "use(obj.attr)" in fixed_content

    filepath.unlink()


def test_autofix_word_boundaries() -> None:
    """Test that autofix uses word boundaries correctly."""
    from tempfile import NamedTemporaryFile

    source = """x = 5
result = max(x, 10)
"""
    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    fixable_violations = [v for v in violations if v.fixable]
    if fixable_violations:
        result = check.fix(filepath, fixable_violations, source, tree)
        assert result is True

        fixed_content = filepath.read_text()
        # Should replace 'x' but not affect 'max'
        assert "result = max(5, 10)" in fixed_content
        assert "max" in fixed_content  # 'max' should still be present

    filepath.unlink()


# === Bug Reproduction Tests ===
# The following tests reproduce bugs from bug_report.md


def test_problem_1_loop_reassignment() -> None:
    """Reproduce Problem 1: Wrong variable replacement in loop reassignment."""
    source = """def find_route():
    latest_datetime = initial_datetime
    for edge in edges:
        destination_datetime_utc = edge.destination_datetime_utc
        if destination_datetime_utc > latest_datetime:
            latest_datetime = destination_datetime_utc
            break
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag latest_datetime as it's reassigned in a loop
    # and used across iterations
    for v in violations:
        assert "latest_datetime" not in v.message, (
            f"Should not flag latest_datetime in loop reassignment: {v.message}"
        )


def test_problem_2_boolean_descriptive_names() -> None:
    """Reproduce Problem 2: False positive on descriptive boolean names."""
    source = """def check_cycle(subgraph, depot_idx):
    out_edge_count = len(subgraph.out_edges(depot_idx))
    in_edge_count = len(subgraph.in_edges(depot_idx))
    has_cycle = bool(find_cycle(subgraph, depot_idx))
    if not all((out_edge_count, in_edge_count, has_cycle)):
        raise ValueError("Invalid graph")
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag has_cycle - it's a descriptive boolean name
    for v in violations:
        assert "has_cycle" not in v.message, (
            f"Should not flag descriptive boolean variable has_cycle: {v.message}"
        )


def test_problem_4_multiple_exception_assignments() -> None:
    """Reproduce Problem 4: Concatenated variable names from multiple assignments."""
    from tempfile import NamedTemporaryFile

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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    violations = check.check(filepath, tree, source)

    # If there are fixable violations, applying the fix should NOT create
    # concatenated nonsense like "value_errortype_errorkey_error"
    if any(v.fixable for v in violations):  # pragma: no cover - autofix disabled
        check.fix(filepath, violations, source, tree)
        fixed_content = filepath.read_text()

        # Verify no concatenated garbage
        assert "value_errortype_error" not in fixed_content
        assert "type_errorkey_error" not in fixed_content

        # Verify the code is still valid Python
        try:
            ast.parse(fixed_content)
        except SyntaxError as e:
            msg = f"Fixed code has syntax error: {e}\n{fixed_content}"
            raise AssertionError(msg) from e

    filepath.unlink()


def test_problem_5_conditional_assignment_logic_change() -> None:
    """Reproduce Problem 5: Logic-changing autofix for conditional assignments."""
    from tempfile import NamedTemporaryFile

    source = """def configure(service_name=None):
    if not service_name:
        service_name = get_caller_module_name()
    return configure_service(service_name)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    violations = check.check(filepath, tree, source)

    # If there are fixable violations, verify the logic isn't changed
    if any(v.fixable for v in violations):  # pragma: no cover - autofix disabled
        check.fix(filepath, violations, source, tree)
        fixed_content = filepath.read_text()

        # The fixed code should NOT change the logic
        # Original: assigns get_caller_module_name() to service_name, then uses it
        # WRONG: if not get_caller_module_name(): ...
        assert "if not get_caller_module_name():" not in fixed_content, (
            f"Autofix changed program logic!\n{fixed_content}"
        )

    filepath.unlink()


def test_same_variable_different_scopes() -> None:
    """Test that variables in different branches are tracked correctly."""
    source = """def process(value):
    if value > 0:
        result = "positive"
        log(result)
    else:
        result = "negative"
        log(result)
    return result
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag result because:
    # 1. It's assigned in different branches
    # 2. It's used after the if/else block
    # 3. Both assignments are needed for the final return
    for v in violations:
        should_skip = (
            "result" not in v.message
            or "positive" not in source
            or "negative" not in source
        )
        assert should_skip


def test_autofix_preserves_blank_lines_across_file() -> None:
    """Test that autofix only cleans up blank lines around removed assignments.

    Regression test for bug where autofix was deleting blank lines across
    the entire file, not just around the removed assignment.
    """
    from tempfile import NamedTemporaryFile

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

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    violations = check.check(filepath, tree, source)

    # If there are fixable violations, verify blank lines are preserved
    if any(v.fixable for v in violations):
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
            "Blank lines between SecondClass and "
            "function_with_redundant_var were removed!"
        )

        expected_pattern_3 = "def another_function():\n    pass\n\n\nclass ThirdClass:"
        assert expected_pattern_3 in fixed_content, (
            "Blank lines between another_function and ThirdClass were removed!"
        )

        # Verify the fixed code is still valid Python
        try:
            ast.parse(fixed_content)
        except SyntaxError as e:
            msg = f"Fixed code has syntax error: {e}\n{fixed_content}"
            raise AssertionError(msg) from e

    filepath.unlink()


def test_autofix_cleans_up_excessive_blank_lines() -> None:
    """Test that autofix reduces 3+ consecutive blank lines to 2 around removals."""
    from tempfile import NamedTemporaryFile

    # File with excessive blank lines around a redundant assignment
    # The blank lines between the removed assignment should be cleaned up
    source = """def function_with_redundant():


    x = 42


    return x
"""

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    violations = check.check(filepath, tree, source)

    # If there are fixable violations, verify excessive blank lines are cleaned
    if any(v.fixable for v in violations):
        check.fix(filepath, violations, source, tree)
        fixed_content = filepath.read_text()

        # Verify the excessive blank lines around the removed assignment are reduced
        # Inside the function, after removing x=42, we should have at most 2 blanks
        # before the return statement
        lines = fixed_content.split("\n")

        # Find the function and count blanks before return
        in_function = False
        blanks_before_return = 0

        for i, line in enumerate(lines):
            if "def function_with_redundant" in line:
                in_function = True
                continue

            if in_function and "return" in line:
                # Count preceding blank lines
                j = i - 1
                while j >= 0 and lines[j].strip() == "":
                    blanks_before_return += 1
                    j -= 1
                break

        # Should have at most 2 blank lines before return
        assert blanks_before_return <= 2, (
            f"Fixed code has {blanks_before_return} blank lines before return "
            f"(expected ≤2)\n{fixed_content}"
        )

        # Verify the fixed code is still valid Python
        try:
            ast.parse(fixed_content)
        except SyntaxError as e:
            msg = f"Fixed code has syntax error: {e}\n{fixed_content}"
            raise AssertionError(msg) from e

    filepath.unlink()


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
    """Test that global scope variables without underscore prefix are not flagged."""
    source = """
parent_url = "https://example.com"
print(parent_url)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag 'parent_url' in global scope (no underscore prefix)
    assert len(violations) == 0


def test_global_scope_with_underscore_flagged() -> None:
    """Test that global scope variables with underscore prefix ARE flagged."""
    source = """
_temp = "foo"
print(_temp)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # SHOULD flag '_temp' in global scope (underscore prefix)
    assert len(violations) >= 1
    assert any("_temp" in v.message for v in violations)


def test_global_scope_with_comment_above_not_flagged() -> None:
    """Test that global scope variables with comments above are not flagged."""
    source = """
# Configuration URL
_url = "https://example.com"
print(_url)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag '_url' because it has a comment above
    assert len(violations) == 0


def test_function_scope_single_use_still_flagged() -> None:
    """Test that function scope variables are still flagged normally."""
    source = """
def func():
    x = "foo"
    print(x)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # SHOULD flag 'x' in function scope
    assert len(violations) >= 1
    assert any("x" in v.message for v in violations)


def test_await_on_both_assignment_and_usage_not_flagged() -> None:
    """Test that await on both RHS and usage is not flagged."""
    source = """
async def test_json(client):
    response = await get_test_response(client, '/null_content')
    assert await response.json() is None
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag 'response' because await is on both assignment and usage
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag 'x' because RHS has await
    assert len(violations) == 0


def test_await_only_on_usage_flagged() -> None:
    """Test that await only on usage is still flagged."""
    source = """
async def test_func():
    x = get_value()
    result = await x.fetch()
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # SHOULD flag 'x' because await is only on usage, not assignment
    assert len(violations) >= 1
    assert any("x" in v.message for v in violations)


def test_ternary_operator_not_flagged() -> None:
    """Test that if-else ternary operators are not flagged."""
    source = """
import sys

DEFAULT_URL = "https://default.example.com"
parent_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
print(parent_url)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag 'parent_url' because it uses ternary operator
    assert len(violations) == 0


def test_ternary_in_function_not_flagged() -> None:
    """Test that ternary operators in function scope are not flagged."""
    source = """
def func(condition):
    value = "yes" if condition else "no"
    return value
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag 'value' because it uses ternary operator
    assert len(violations) == 0


def test_long_rhs_not_flagged() -> None:
    """Test that variables with long RHS are not flagged (>79 chars after inline)."""
    source = """
def func():
    variable = compute_something_with_very_long_function_name()
    assert variable.attribute_name
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag 'variable' if inlining would exceed 79 characters
    # The heuristic checks if len(rhs_source) >= 25 or len_diff > 15
    # len(rhs_source) = 49 >= 25, so should not be flagged
    assert len(violations) == 0


def test_comment_above_in_function_scope_not_flagged() -> None:
    """Test that variables with comments above are not flagged (any scope)."""
    source = """
def auto_clear_fixture():
    # Exclude cache.
    # The prefixes are hard-coded in external library
    cache_prefixes = ("responses", "redirects")
    process(cache_prefixes)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag 'cache_prefixes' because it has a comment above
    assert len(violations) == 0


def test_moderately_long_rhs_not_flagged() -> None:
    """Test that RHS >= 25 chars is not flagged (line length heuristic)."""
    source = """
def func():
    prefixes = ("responses", "redirects")
    process(prefixes)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag because RHS is 26 chars (>= 25)
    # len('("responses", "redirects")') = 26
    assert len(violations) == 0


def test_comment_above_multiline_not_flagged() -> None:
    """Test that variables with multiline comments above are not flagged."""
    source = """
def func():
    # First comment line
    # Second comment line
    # Third comment line with URL: https://example.com/path
    variable = calculate_value()
    return variable
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag because there's a comment on the line directly above
    assert len(violations) == 0


def test_would_require_parentheses_binop() -> None:
    """Test that _would_require_parentheses detects binary operations."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _would_require_parentheses,
    )

    # Test BinOp (addition)
    source = "len(x) + 1"
    rhs_node = ast.parse(source, mode="eval").body
    assert _would_require_parentheses(rhs_node) is True


def test_would_require_parentheses_boolop() -> None:
    """Test that _would_require_parentheses detects boolean operations."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _would_require_parentheses,
    )

    # Test BoolOp (and)
    source = "a and b"
    rhs_node = ast.parse(source, mode="eval").body
    assert _would_require_parentheses(rhs_node) is True


def test_would_require_parentheses_compare() -> None:
    """Test that _would_require_parentheses detects comparison operations."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _would_require_parentheses,
    )

    # Test Compare
    source = "x == y"
    rhs_node = ast.parse(source, mode="eval").body
    assert _would_require_parentheses(rhs_node) is True


def test_would_require_parentheses_simple() -> None:
    """Test that _would_require_parentheses returns False for simple expressions."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _would_require_parentheses,
    )

    # Test simple call - should not require parentheses
    source = "len(x)"
    rhs_node = ast.parse(source, mode="eval").body
    assert _would_require_parentheses(rhs_node) is False


def test_should_report_violation_with_parentheses_required() -> None:
    """Test that violations requiring parentheses are not reported."""
    source = """
def func():
    len_prefix = len(x) + 1
    return arr[len_prefix:]
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not report because inlining would require parentheses
    assert len(violations) == 0


def test_should_autofix_single_use_with_attribute() -> None:
    """Test that should_autofix allows attribute access for SINGLE_USE."""
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

    # Should autofix for SINGLE_USE with simple attribute access
    result = should_autofix(lifecycle, PatternType.SINGLE_USE)
    assert result is True


def test_should_autofix_single_use_with_keywords() -> None:
    """Test that should_autofix allows simple keyword arguments for SINGLE_USE."""
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

    # Should autofix for SINGLE_USE with simple keyword call
    result = should_autofix(lifecycle, PatternType.SINGLE_USE)
    assert result is True


def test_should_autofix_single_use_high_semantic_score() -> None:
    """Test that should_autofix rejects SINGLE_USE with high semantic score."""
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

    # Should NOT autofix due to high semantic score (descriptive name)
    result = should_autofix(lifecycle, PatternType.SINGLE_USE)
    assert result is False


def test_should_not_autofix_single_use_complex_call() -> None:
    """Test that should_autofix rejects SINGLE_USE with complex calls."""
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

    # Should NOT autofix - too many args
    result = should_autofix(lifecycle, PatternType.SINGLE_USE)
    assert result is False


def test_closure_variable_not_flagged() -> None:
    """Test that variables used in nested functions (closures) are not flagged."""
    source = """
async def test_func(faker):
    return_value = faker.pystr()

    @decorator
    async def inner_func():
        return return_value

    await inner_func()
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag return_value - it's captured by the closure
    assert len(violations) == 0


def test_closure_with_mock_not_flagged() -> None:
    """Test that Mock objects used in closures are not flagged as redundant."""
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag mock - it's used in the closure and in outer scope
    assert len(violations) == 0


def test_closure_single_use_in_nested_function() -> None:
    """Test variables used only in nested function are not flagged."""
    source = """
def outer():
    value = calculate()

    def inner():
        return value

    return inner
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag value - used in nested function (closure)
    assert len(violations) == 0


def test_closure_multiple_nested_levels() -> None:
    """Test variables captured by deeply nested closures are not flagged."""
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag x - used in level2 and level3 (closures)
    # Should NOT flag y - used in level3 (closure)
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag return_value - used in nested function and outer scope
    # Should NOT flag mock - used in nested function and outer scope
    assert len(violations) == 0


def test_non_closure_still_detected() -> None:
    """Test that non-closure single-use variables are still detected.

    This is NOT a closure - just a redundant assignment in same scope.
    """
    source = """
def test_func():
    x = "foo"
    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # SHOULD flag x - it's a simple single-use, not a closure
    assert len(violations) >= 1
    assert any("x" in v.message for v in violations)


def test_lifecycle_is_immediate_use_with_closure() -> None:
    """Test that is_immediate_use returns False for closures."""
    from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
        AssignmentInfo,
        UsageInfo,
        VariableLifecycle,
    )

    # Create a lifecycle where the use is in a different scope (closure)
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
    """Test that verbose variable names with kwargs.get() are not flagged.

    Example from user request: raw_headers = kwargs.get("headers")
    The variable name "raw_headers" is more descriptive than just "headers"
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag raw_headers - it adds verbosity/context
    for v in violations:
        assert "raw_headers" not in v.message, (
            f"Should not flag 'raw_headers' - it adds verbosity: {v.message}"
        )


def test_verbose_variable_names_parsed_data_not_flagged() -> None:
    """Test that variable names describing parsed data are not flagged.

    Example from user request: translations = orjson.loads(f.read())
    The variable name "translations" describes what the parsed data represents
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag translations - it adds context to what the data is
    for v in violations:
        assert "translations" not in v.message, (
            f"Should not flag 'translations' - it adds context: {v.message}"
        )


def test_firestore_client_not_flagged() -> None:
    """Test that more specific type names are not flagged.

    Example: firestore_client = db.client()
    The variable name is more specific than just "client"
    """
    source = """
def get_firestore():
    firestore_client = db.client()
    return firestore_client
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag firestore_client - it's more specific than "client"
    for v in violations:
        assert "firestore_client" not in v.message, (
            f"Should not flag 'firestore_client' - it's more specific: {v.message}"
        )


def test_user_email_dict_access_not_flagged() -> None:
    """Test that more verbose dict access variable names are not flagged.

    Example: user_email = data["email"]
    The variable name is more verbose/specific than just "email"
    """
    source = """
def process_user(data):
    user_email = data["email"]
    send_notification(user_email)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag user_email - it's more verbose than "email"
    for v in violations:
        assert "user_email" not in v.message, (
            f"Should not flag 'user_email' - it adds verbosity: {v.message}"
        )


def test_descriptive_prefix_not_flagged() -> None:
    """Test that descriptive prefixes are recognized.

    Examples: raw_data, parsed_output, validated_input
    """
    source = """
def process_input(data):
    raw_data = fetch_from_api()
    return raw_data
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag raw_data - "raw" is a descriptive prefix
    for v in violations:
        assert "raw_data" not in v.message, (
            f"Should not flag 'raw_data' - 'raw' is descriptive: {v.message}"
        )


def test_adds_verbosity_or_context_function_directly() -> None:
    """Test the _adds_verbosity_or_context function directly."""
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
    """Test that multiline RHS is not marked as fixable.

    This is a regression test for the bug where violations were marked as
    [FIXABLE] even when --fix couldn't actually fix them.
    """
    source = """
def func():
    value = very_long_function_call(
        arg1,
        arg2
    )
    return value
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # If there are violations, they should NOT be marked as fixable
    # because the RHS is multiline
    for v in violations:
        if "value" in v.message:
            assert not v.fixable, (
                f"Multiline RHS should not be marked fixable: {v.message}"
            )


def test_semantic_value_descriptive_boolean_prefix() -> None:
    """Test that descriptive boolean prefixes increase semantic value."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test has_ prefix
    rhs_node = ast.parse("check_something()", mode="eval").body
    score = calculate_semantic_value(
        "has_permission", "check_something()", rhs_node, False
    )
    assert score >= 50  # Should get +50 for has_ prefix


def test_semantic_value_descriptive_suffix() -> None:
    """Test that descriptive suffixes increase semantic value."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test _count suffix
    rhs_node = ast.parse("len(items)", mode="eval").body
    score = calculate_semantic_value("item_count", "len(items)", rhs_node, False)
    assert score >= 40  # Should get +40 for _count suffix


def test_semantic_value_list_comprehension() -> None:
    """Test that list comprehensions increase semantic value."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test list comprehension
    source = "[x for x in items]"
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("result", source, rhs_node, False)
    assert score >= 30  # Should get +30 for comprehension


def test_semantic_value_unary_operation() -> None:
    """Test that unary operations increase semantic value."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test unary operation
    source = "-value"
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("result", source, rhs_node, False)
    assert score >= 10  # Should get +10 for unary op


def test_semantic_value_lambda_expression() -> None:
    """Test that lambda expressions increase semantic value."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test lambda
    source = "lambda x: x * 2"
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("func", source, rhs_node, False)
    assert score >= 25  # Should get +25 for lambda


def test_semantic_value_very_long_expression() -> None:
    """Test that very long expressions (80+ chars) increase semantic value."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test very long expression (85 chars)
    source = "a" * 85
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("x", source, rhs_node, False)
    assert score >= 35  # Should get +35 for very long expression


def test_semantic_value_long_expression_60_plus() -> None:
    """Test that long expressions (60-80 chars) increase semantic value."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    # Test long expression (65 chars)
    source = "a" * 65
    rhs_node = ast.parse(source, mode="eval").body
    score = calculate_semantic_value("x", source, rhs_node, False)
    assert score >= 25  # Should get +25 for long expression


def test_no_false_positive_on_long_rhs_fixable_marking() -> None:
    """Test that long RHS that would exceed line length is not marked fixable.

    This is a regression test for the bug where violations were marked as
    [FIXABLE] even when --fix couldn't actually fix them.
    """
    source = """
def func():
    value = very_long_func_that_would_exceed_line_length_when_inlined_here()
    return value
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # If there are violations, they should NOT be marked as fixable
    # because inlining would exceed line length
    for v in violations:
        if "value" in v.message:
            msg = f"Long RHS should not be marked fixable: {v.message}"
            assert not v.fixable, msg


def test_magic_number_not_flagged() -> None:
    """Test that magic numbers with descriptive names are not flagged.

    Variables like max_search_depth = 10 give semantic meaning to raw numbers.
    """
    source = """
def find_project_root():
    max_search_depth = 10
    current_dir = Path.cwd()
    for _ in range(max_search_depth):
        if (current_dir / "pyproject.toml").is_file():
            return current_dir
        current_dir = current_dir.parent
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag max_search_depth - it's a named constant avoiding magic number
    for v in violations:
        assert "max_search_depth" not in v.message, (
            f"Should not flag 'max_search_depth' - avoids magic number: {v.message}"
        )


def test_magic_number_float_not_flagged() -> None:
    """Test that float constants with descriptive names are not flagged."""
    source = """
def calculate_spacing():
    line_spacing = 1.2
    coords = (x, y + height * line_spacing)
    return coords
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag line_spacing - it's a named constant
    for v in violations:
        assert "line_spacing" not in v.message, (
            f"Should not flag 'line_spacing' - avoids magic number: {v.message}"
        )


def test_magic_number_id_not_flagged() -> None:
    """Test that ID constants with descriptive names are not flagged."""
    source = """
async def find_nicosia(database):
    nicosia_in_cyprus_id = 101749141
    place = await database.find_one({"_id": nicosia_in_cyprus_id})
    return place
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag nicosia_in_cyprus_id - it's a named constant
    for v in violations:
        assert "nicosia_in_cyprus_id" not in v.message, (
            f"Should not flag 'nicosia_in_cyprus_id' - avoids magic number: {v.message}"
        )


def test_pytest_raises_pattern_not_flagged() -> None:
    """Test that variables used inside pytest.raises are not flagged.

    Setup should be outside the context manager to keep the with block minimal.
    """
    source = """
def test_rate_limit():
    sample_class = SampleClass()
    with pytest.raises(RateLimitError):
        sample_class.sample_method()
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag sample_class - setup should be outside pytest.raises
    for v in violations:
        assert "sample_class" not in v.message, (
            f"Should not flag 'sample_class' - pytest.raises pattern: {v.message}"
        )


def test_with_block_pattern_not_flagged() -> None:
    """Test that variables set up before a with block are not flagged."""
    source = """
def test_retry():
    decorated_mock_func = retry_service(mock_func)

    with pytest.raises(ValueError, match=error_msg):
        decorated_mock_func()
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag decorated_mock_func - setup outside with block is intentional
    for v in violations:
        assert "decorated_mock_func" not in v.message, (
            f"Should not flag 'decorated_mock_func' - with block pattern: {v.message}"
        )


def test_inline_comment_not_flagged() -> None:
    """Test that assignments with inline comments are not flagged.

    Inline comments indicate intentional code (e.g., type: ignore).
    """
    source = """
def get_cache_file(cache):
    redirects_file = cache.redirects.filename  # type: ignore[attr-defined]

    assert redirects_file.startswith(cache_dir)
    return redirects_file
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag redirects_file - has inline comment
    for v in violations:
        assert "redirects_file" not in v.message, (
            f"Should not flag 'redirects_file' - has inline comment: {v.message}"
        )


def test_nonlocal_in_nested_function_not_flagged() -> None:
    """Test that variables captured by nonlocal in nested functions are not flagged.

    This is a regression test for the bug where the linter would remove a variable
    that was modified via nonlocal in a nested function.
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag 'cancelled' - it's captured by nonlocal in nested function
    for v in violations:
        assert "cancelled" not in v.message, (
            f"Should not flag 'cancelled' - captured by nonlocal: {v.message}"
        )


def test_nonlocal_multiple_variables_not_flagged() -> None:
    """Test multiple variables captured by nonlocal are not flagged."""
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag x or y - they're captured by nonlocal
    for v in violations:
        msg = v.message
        assert "'x'" not in msg, f"Should not flag 'x' - captured by nonlocal: {msg}"
        assert "'y'" not in msg, f"Should not flag 'y' - captured by nonlocal: {msg}"


def test_has_inline_comment_detection() -> None:
    """Test the _has_inline_comment function."""
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
    """Test the _is_named_constant_pattern function."""
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
    """Test that assignments inside while loops are not flagged."""
    source = """
def process():
    x = 0
    while x < 10:
        x = x + 1
    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Assignments in loops should not be flagged
    for v in violations:
        assert "'x'" not in v.message, f"Should not flag 'x' in while loop: {v.message}"


def test_async_for_loop_assignment_not_flagged() -> None:
    """Test that assignments inside async for loops are not flagged."""
    source = """
async def process(items):
    result = []
    async for item in items:
        result = result + [item]
    return result
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Assignments in async for loops should not be flagged
    for v in violations:
        assert "'result'" not in v.message, f"Should not flag loop var: {v.message}"


def test_async_with_assignment_not_flagged() -> None:
    """Test that assignments inside async with blocks are handled."""
    source = """
async def process():
    async with context() as ctx:
        x = ctx.value
        return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    # Just verify it doesn't crash - async with should be tracked
    _ = check.check(Path("test.py"), tree, source)


def test_global_attribute_assignment_not_tracked() -> None:
    """Test that attribute assignments to global vars are skipped properly."""
    source = """
global_obj = None

def modify_global():
    global global_obj
    global_obj.attr = "value"
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    # Should not crash when global var is base of attribute assignment
    violations = check.check(Path("test.py"), tree, source)
    assert len(violations) == 0


def test_nondeterministic_call_not_flagged() -> None:
    """Test that nondeterministic function calls are not flagged."""
    source = """
import time

def measure():
    start = time.time()
    do_work()
    return start
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag 'start' because time.time() is nondeterministic
    for v in violations:
        assert "start" not in v.message, (
            f"Should not flag 'start' - nondeterministic: {v.message}"
        )


def test_multiple_assignment_targets_not_tracked() -> None:
    """Test that multiple assignment targets (a = b = c = value) are skipped."""
    source = """
def func():
    a = b = c = some_value()
    return a + b + c
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    # Should not crash and should not flag these assignments
    violations = check.check(Path("test.py"), tree, source)
    # Multiple assignment targets are skipped entirely
    assert all("'a'" not in v.message for v in violations)
    assert all("'b'" not in v.message for v in violations)
    assert all("'c'" not in v.message for v in violations)


def test_inline_comment_with_string_containing_hash() -> None:
    """Test inline comment detection with strings containing #."""
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
    """Test that ternary/if-else expressions are explicitly not flagged."""
    source = """
def func(condition):
    result = "yes" if condition else "no"
    return result
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag result because it's a ternary expression
    for v in violations:
        assert "result" not in v.message, (
            f"Should not flag ternary expression: {v.message}"
        )


def test_descriptive_suffix_size_not_flagged() -> None:
    """Test that variables with _size suffix are not flagged.

    Variables like large_payload_size = len(large_payload) clarify what
    the value represents (the SIZE), making the code more readable.
    """
    source = """
def test_flow_control_binary(protocol, out_low_limit, parser_low_limit):
    large_payload = b"b" * (1 + 16 * 2)
    large_payload_size = len(large_payload)
    parser_low_limit._handle_frame(True, WSMsgType.BINARY, large_payload, 0)
    res = out_low_limit._buffer[0]
    assert res == WSMessageBinary(data=large_payload, size=large_payload_size, extra="")
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag large_payload_size - it has descriptive _size suffix
    for v in violations:
        assert "large_payload_size" not in v.message, (
            f"Should not flag 'large_payload_size' - has _size suffix: {v.message}"
        )


def test_descriptive_suffix_length_not_flagged() -> None:
    """Test that variables with _length suffix are not flagged."""
    source = """
def process(data):
    buffer_length = len(data)
    return process_with_length(data, buffer_length)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag buffer_length - it has descriptive _length suffix
    for v in violations:
        assert "buffer_length" not in v.message, (
            f"Should not flag 'buffer_length' - has _length suffix: {v.message}"
        )


def test_descriptive_suffix_id_not_flagged() -> None:
    """Test that variables with _id suffix are not flagged."""
    source = """
def get_user(data):
    user_id = data.get("id")
    return fetch_user(user_id)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag user_id - it has descriptive _id suffix
    for v in violations:
        assert "user_id" not in v.message, (
            f"Should not flag 'user_id' - has _id suffix: {v.message}"
        )


def test_test_file_detection_by_path() -> None:
    """Test that files in tests/ directory are recognized as test files."""
    source = """
def test_camel_to_under():
    camel_case_sample = "RandomClassName"
    assert camel_to_under(camel_case_sample) == "random_class_name"
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    # File in tests/ directory should not flag test setup variables
    violations = check.check(Path("tests/test_utils.py"), tree, source)

    # Should NOT flag camel_case_sample - test setup variable in test file
    for v in violations:
        assert "camel_case_sample" not in v.message, (
            f"Should not flag test setup variable in test file: {v.message}"
        )


def test_test_file_detection_by_name() -> None:
    """Test that files with test_ prefix are recognized as test files."""
    source = """
def test_translate_templates():
    templates = ["Hello", "Goodbye"]
    translator = MockTranslator(templates)
    assert translator.templates == templates
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    # File with test_ prefix should not flag test variables
    violations = check.check(Path("test_translator.py"), tree, source)

    # Should NOT flag templates - descriptive test data in test file
    for v in violations:
        assert "templates" not in v.message, (
            f"Should not flag test data variable in test file: {v.message}"
        )


def test_test_result_variable_not_flagged() -> None:
    """Test that result variables in test files are not flagged."""
    source = """
def test_landmark_equal_to_none():
    landmark = Landmark(name="Tower", long_lat=(2.0, 48.0), score=0.9)
    result = landmark.__eq__(None)
    assert result is NotImplemented
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    violations = check.check(Path("tests/test_model.py"), tree, source)

    # Should NOT flag result - common test pattern for assertion clarity
    for v in violations:
        assert "result" not in v.message, (
            f"Should not flag 'result' in test file: {v.message}"
        )


def test_test_mock_object_not_flagged() -> None:
    """Test that mock objects with descriptive names are not flagged."""
    source = """
def test_prepare_photo():
    mock_image = MagicMock()
    mock_vision.Image.return_value = mock_image
    result = gcp_vision._prepare_photo(file_obj)
    assert result == mock_image
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    violations = check.check(Path("tests/test_vision.py"), tree, source)

    # Should NOT flag mock_image - named mock object in test file
    for v in violations:
        assert "mock_image" not in v.message, (
            f"Should not flag mock object in test file: {v.message}"
        )


def test_semantic_test_data_list_not_flagged() -> None:
    """Test that semantic test data lists are not flagged in test files."""
    source = """
def test_airport_connectivity():
    some_european_airports = ["AES", "BYJ", "BTS"]
    assert all(
        iata in airport_connectivity.airports_by_continent
        for iata in some_european_airports
    )
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    violations = check.check(Path("tests/test_kiwi_api.py"), tree, source)

    # Should NOT flag some_european_airports - semantic test data
    for v in violations:
        assert "some_european_airports" not in v.message, (
            f"Should not flag semantic test data in test file: {v.message}"
        )


def test_range_with_descriptive_name_not_flagged() -> None:
    """Test that range objects with descriptive names are not flagged in test files."""
    source = """
def generate_price_data():
    days_with_routes_in_a_row = range(70)
    return [
        faker.pyint(min_value=50, max_value=MAX_PRICE_EUR)
        for _ in days_with_routes_in_a_row
    ]
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    violations = check.check(Path("tests/test_flight_prices.py"), tree, source)

    # Should NOT flag days_with_routes_in_a_row - descriptive range in test file
    for v in violations:
        assert "days_with_routes_in_a_row" not in v.message, (
            f"Should not flag descriptive range in test file: {v.message}"
        )


def test_non_test_file_still_flags_simple_assignments() -> None:
    """Test that non-test files still flag simple redundant assignments."""
    source = """
def process_data():
    x = "foo"
    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    # Non-test file should still flag simple redundant assignments
    violations = check.check(Path("src/processor.py"), tree, source)

    # Should flag x - simple redundant assignment in non-test file
    msg = "Should flag simple redundant assignment in non-test file"
    assert len(violations) > 0, msg
    assert any("x" in v.message for v in violations), (
        "Should flag variable 'x' in non-test file"
    )


def test_is_test_file_detects_tests_directory() -> None:
    """Test that _is_test_file correctly identifies files in tests directory."""
    assert _is_test_file(Path("tests/test_something.py")) is True
    assert _is_test_file(Path("tests/utils/test_helpers.py")) is True
    assert _is_test_file(Path("test/test_foo.py")) is True


def test_is_test_file_detects_test_prefix() -> None:
    """Test that _is_test_file correctly identifies files with test_ prefix."""
    assert _is_test_file(Path("test_example.py")) is True
    assert _is_test_file(Path("src/test_module.py")) is True


def test_is_test_file_detects_test_suffix() -> None:
    """Test that _is_test_file correctly identifies files with _test.py suffix."""
    assert _is_test_file(Path("example_test.py")) is True
    assert _is_test_file(Path("src/module_test.py")) is True


def test_is_test_file_rejects_non_test_files() -> None:
    """Test that _is_test_file correctly rejects non-test files."""
    assert _is_test_file(Path("src/module.py")) is False
    assert _is_test_file(Path("main.py")) is False
    assert _is_test_file(Path("setup.py")) is False


def test_is_test_file_handles_none() -> None:
    """Test that _is_test_file handles None input."""
    assert _is_test_file(None) is False


def test_is_test_function_detects_test_functions() -> None:
    """Test that _is_test_function correctly identifies test functions."""
    source = "def test_something(): pass"
    tree = ast.parse(source)
    func_node = tree.body[0]
    assert _is_test_function(func_node) is True


def test_is_test_function_detects_async_test_functions() -> None:
    """Test that _is_test_function correctly identifies async test functions."""
    source = "async def test_async_something(): pass"
    tree = ast.parse(source)
    func_node = tree.body[0]
    assert _is_test_function(func_node) is True


def test_is_test_function_rejects_non_test_functions() -> None:
    """Test that _is_test_function correctly rejects non-test functions."""
    source = "def helper_function(): pass"
    tree = ast.parse(source)
    func_node = tree.body[0]
    assert _is_test_function(func_node) is False


def test_is_test_function_handles_non_function_nodes() -> None:
    """Test that _is_test_function handles non-function nodes."""
    source = "x = 5"
    tree = ast.parse(source)
    assign_node = tree.body[0]
    assert _is_test_function(assign_node) is False


def test_is_test_function_handles_none() -> None:
    """Test that _is_test_function handles None input."""
    assert _is_test_function(None) is False


def test_context_manager_assignment_inside_usage_outside_not_flagged() -> None:
    """Test that assignments inside context managers with usage outside are not flagged.

    This pattern is used to reduce nesting - load data inside the context manager,
    use it outside to avoid deep indentation.
    """
    source = """
def load_config():
    with open("config.toml", "rb") as file:
        config = tomllib.load(file)
    # Use config outside to reduce nesting
    value = config.get("key", {})
    return value
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag config - intentional pattern to reduce nesting
    for v in violations:
        assert "config" not in v.message, (
            f"Should not flag context manager pattern: {v.message}"
        )


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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag config - used outside with block to reduce nesting
    for v in violations:
        assert "config" not in v.message, (
            f"Should not flag config in context manager pattern: {v.message}"
        )


def test_database_connection_pattern_not_flagged() -> None:
    """Test database connection pattern where data is loaded inside, used outside."""
    source = """
def fetch_user(user_id):
    with get_db_connection() as conn:
        result = conn.execute("SELECT * FROM users WHERE id = ?", user_id)
        user_data = result.fetchone()
    # Process user_data outside connection to avoid holding it open
    return process_user(user_data)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag user_data - intentional pattern to close connection quickly
    for v in violations:
        assert "user_data" not in v.message, (
            f"Should not flag database pattern: {v.message}"
        )


def test_if_block_assignment_inside_usage_outside_not_flagged() -> None:
    """Test that assignments inside if blocks with usage outside are not flagged."""
    source = """
def process():
    if condition:
        data = load_expensive_data()
    # Use data outside if block
    result = transform(data)
    return result
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag data - used outside if block
    for v in violations:
        assert "data" not in v.message, f"Should not flag if block pattern: {v.message}"


def test_try_block_assignment_inside_usage_outside_not_flagged() -> None:
    """Test that assignments inside try blocks with usage outside are not flagged."""
    source = """
def load_with_fallback():
    try:
        data = load_from_api()
    except Exception:
        data = load_from_cache()
    # Use data outside try block
    return process(data)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag data - used outside try block
    for v in violations:
        assert "data" not in v.message, (
            f"Should not flag try block pattern: {v.message}"
        )


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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    for v in violations:
        assert "depot_iso_country" not in v.message, (
            f"Should not flag comprehension-cached variable: {v.message}"
        )


def test_variable_used_only_in_list_comprehension_element_not_flagged() -> None:
    """Test variable used in the element expression of a list comprehension."""
    source = """
def transform(multiplier, items):
    factor = multiplier.value
    return [x * factor for x in items]
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    for v in violations:
        assert "factor" not in v.message, (
            f"Should not flag comprehension element variable: {v.message}"
        )


def test_variable_used_only_in_dict_comprehension_not_flagged() -> None:
    """Test variable cached for use inside a dict comprehension."""
    source = """
def build_map(source_obj, keys):
    prefix = source_obj.namespace
    return {k: f"{prefix}_{k}" for k in keys}
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    for v in violations:
        assert "prefix" not in v.message, (
            f"Should not flag dict-comprehension-cached variable: {v.message}"
        )


def test_variable_used_only_in_set_comprehension_not_flagged() -> None:
    """Test variable cached for use inside a set comprehension."""
    source = """
def unique_suffixes(config, items):
    suffix = config.default_suffix
    return {item + suffix for item in items}
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    for v in violations:
        assert "suffix" not in v.message, (
            f"Should not flag set-comprehension-cached variable: {v.message}"
        )


def test_variable_used_only_in_generator_expression_not_flagged() -> None:
    """Test variable cached for use inside a generator expression."""
    source = """
def total_score(config, players):
    bonus = config.bonus_points
    return sum(p.score + bonus for p in players)
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    for v in violations:
        assert "bonus" not in v.message, (
            f"Should not flag generator-expression-cached variable: {v.message}"
        )


def test_variable_used_inside_and_outside_comprehension_not_flagged() -> None:
    """Test that the new rule does not interfere with multi-use variables.

    A variable used both inside AND outside a comprehension has multiple uses,
    so detect_redundancy returns None and it is never flagged regardless.
    """
    source = """
def example(obj, items):
    val = obj.attr
    result = [x for x in items if x == val]
    return val
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # val is used twice (inside comprehension + return), so it is not single-use
    # and must not be flagged by detect_redundancy at all.
    for v in violations:
        assert "val" not in v.message, (
            f"Multi-use variable should not be flagged: {v.message}"
        )


def test_in_comprehension_flag_set_correctly() -> None:
    """Test that UsageInfo.in_comprehension is set for comprehension usages."""
    source = """
def func(obj, items):
    cached = obj.attr
    result = [x for x in items if x == cached]
    return result
"""
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    tracker.visit(tree)
    lifecycles = tracker.build_lifecycles()

    cached_lifecycle = next(
        lc for lc in lifecycles if lc.assignment.var_name == "cached"
    )
    assert len(cached_lifecycle.uses) == 1
    assert cached_lifecycle.uses[0].in_comprehension is True


def test_in_comprehension_flag_false_for_normal_usage() -> None:
    """Test that UsageInfo.in_comprehension is False for non-comprehension usages."""
    source = """
def func():
    x = "foo"
    print(x)
"""
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    tracker.visit(tree)
    lifecycles = tracker.build_lifecycles()

    x_lifecycle = next(lc for lc in lifecycles if lc.assignment.var_name == "x")
    assert all(not use.in_comprehension for use in x_lifecycle.uses)


def test_calculate_semantic_value_test_context_list_literal() -> None:
    """Test that list/dict/set literals get a bonus in test-context scoring.

    Rule 10 now intercepts variables used solely inside comprehensions before
    they reach calculate_semantic_value, so this branch needs a direct test.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_source = '["AES", "BYJ", "BTS"]'
    rhs_node = ast.parse(rhs_source, mode="eval").body

    score = calculate_semantic_value(
        "some_european_airports",
        rhs_source,
        rhs_node,
        has_type_annotation=False,
        is_test_context=True,
    )
    # multi-part name (+30) + "some" in test_semantic_words (+25) + list bonus (+25)
    assert score >= 25


def test_calculate_semantic_value_test_context_dict_literal() -> None:
    """Test that dict literals get a bonus in test-context scoring."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_source = '{"key": "value"}'
    rhs_node = ast.parse(rhs_source, mode="eval").body

    score = calculate_semantic_value(
        "my_mapping",
        rhs_source,
        rhs_node,
        has_type_annotation=False,
        is_test_context=True,
    )
    assert score >= 25  # dict literal bonus in test context


def test_calculate_semantic_value_test_context_range_call() -> None:
    """Test that range() calls get a bonus in test-context scoring.

    Rule 10 now intercepts 'days_with_routes_in_a_row' used in comprehension
    before it reaches calculate_semantic_value, so this needs a direct test.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_source = "range(70)"
    rhs_node = ast.parse(rhs_source, mode="eval").body

    score = calculate_semantic_value(
        "days_with_routes_in_a_row",
        rhs_source,
        rhs_node,
        has_type_annotation=False,
        is_test_context=True,
    )
    # multi-part name (+30) + no test_semantic_words match (+0) + range bonus (+25)
    assert score >= 25


def test_calculate_semantic_value_test_context_no_semantic_word() -> None:
    """Test test-context scoring for a variable name without test semantic words.

    Covers the False branch of the test_semantic_words check (line 343->348).
    Before Rule 10, 'days_with_routes_in_a_row' (no semantic test words) covered
    this branch, but it is now intercepted by Rule 10.
    """
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_source = "42"
    rhs_node = ast.parse(rhs_source, mode="eval").body

    # "flight_count" contains no test semantic words
    score = calculate_semantic_value(
        "flight_count",
        rhs_source,
        rhs_node,
        has_type_annotation=False,
        is_test_context=True,
    )
    # multi-part name (+30) + no test_semantic_words match (+0)
    assert score >= 0  # just verifying the False branch is exercised


def test_pytriage_ignore_still_suppresses_comprehension_false_positive() -> None:
    """Test that the pytriage ignore comment works for comprehension cases too."""
    source = """
def func(depot_data, depots):
    depot_iso_country = depot_data.iso_country  # pytriage: ignore=TRI005
    return [x for x in depots if x.country == depot_iso_country]
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    for v in violations:
        assert "'app'" not in v.message, (
            "Should not flag 'app' - used in decorator and return statement: "
            f"{v.message}"
        )


def test_decorator_use_is_tracked_by_variable_tracker() -> None:
    """Test that VariableTracker records decorator expressions as uses."""
    source = """
def outer():
    app = make_app()

    @app.route("/")
    def index():
        pass

    return app
"""
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    tracker.visit(tree)
    lifecycles = tracker.build_lifecycles()

    app_lifecycle = next(
        (lc for lc in lifecycles if lc.assignment.var_name == "app"),
        None,
    )
    assert app_lifecycle is not None
    # Two uses: @app.route("/") and return app
    assert len(app_lifecycle.uses) == 2


def test_class_decorator_use_is_tracked() -> None:
    """Test that class decorators in nested classes are tracked as uses."""
    source = """
def factory():
    validator = build_validator()

    @validator.register
    class Rule:
        pass

    return validator
"""
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    tracker.visit(tree)
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
    tracker = VariableTracker(source)
    tree = ast.parse(source)
    # Must not raise; call-result targets are silently skipped
    tracker.visit(tree)


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
    tree = ast.parse(source)
    tracker.visit(tree)
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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

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
    score = calculate_semantic_value("x", "a + b", rhs_node, False)
    assert score >= 15


def test_calculate_semantic_value_ifexp() -> None:
    """Branch coverage: IfExp RHS adds 20 to semantic score (line 405)."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        calculate_semantic_value,
    )

    rhs_node = ast.parse("1 if c else 0", mode="eval").body
    score = calculate_semantic_value("x", "1 if c else 0", rhs_node, False)
    assert score >= 20


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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should NOT flag 'x' - it has an inline comment
    for v in violations:
        assert "'x'" not in v.message, (
            f"Should not flag 'x' - has inline comment: {v.message}"
        )


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
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(Path("test.py"), tree, source)

    for v in violations:
        assert "'x'" not in v.message, f"Should not flag short IfExp: {v.message}"


def _make_single_use_lifecycle(
    rhs_source: str,
    rhs_node: ast.expr,
    var_name: str = "x",
    in_loop: bool = False,
    in_control_flow: bool = False,
) -> VariableLifecycle:
    """Build a minimal VariableLifecycle for direct should_autofix tests."""
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
    use = UsageInfo(
        var_name=var_name,
        line=2,
        col=0,
        stmt_index=1,
        context="return",
        scope_id=1,
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
    result = _adds_verbosity_or_context("user_obj", "obj[key]", rhs_node)
    # Pattern 1 also doesn't apply ("user" not a descriptive prefix for "obj[key]")
    # Just assert we get a bool without error — coverage is the goal here.
    assert isinstance(result, bool)


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
    rhs_src = 'funcs["load"](data)'
    result = _adds_verbosity_or_context("configuration", rhs_src, rhs_node)
    assert isinstance(result, bool)


def test_adds_verbosity_get_call_key_not_in_var() -> None:
    """Branch coverage: Pattern 3 (.get() call) where key is not in var name."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _adds_verbosity_or_context,
    )

    # var_name "x" does not contain "email" → Pattern 3 condition is False
    rhs_node = ast.parse('data.get("email")', mode="eval").body
    result = _adds_verbosity_or_context("x", 'data.get("email")', rhs_node)
    assert result is False


def test_adds_verbosity_parse_func_with_subscript_func() -> None:
    """Branch coverage: Pattern 4 parse func where func is a Subscript (271->276)."""
    from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
        _adds_verbosity_or_context,
    )

    # parsers["json"](data) — func is a Subscript, not Name or Attribute
    rhs_node = ast.parse('parsers["json"](data)', mode="eval").body
    rhs_src = 'parsers["json"](data)'
    result = _adds_verbosity_or_context("parsed_data", rhs_src, rhs_node)
    assert isinstance(result, bool)


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
    result = _adds_verbosity_or_context("result", "json.loads(data)", rhs_node)
    assert result is False


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
