from __future__ import annotations

import ast

import pytest

from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
    AssignmentInfo,
    PatternType,
    UsageInfo,
    VariableLifecycle,
    VariableTracker,
    _evaluation_order_children,
    _has_comment_above,
    _has_inline_comment,
    detect_redundancy,
    is_preceded_by_call,
)
from tests.redundant_assignment._helpers import _check


def _lifecycle_for(source: str, var_name: str) -> VariableLifecycle:
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    return next(lc for lc in tracker.build_lifecycles() if lc.assignment.var_name == var_name)


def _lifecycle_count(source: str, var_name: str) -> int:
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    return len([lc for lc in tracker.build_lifecycles() if lc.assignment.var_name == var_name])


# ---------------------------------------------------------------------------
# VariableTracker / lifecycle building
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "var_name", "count"),
    [
        (
            """
def outer():
    x = "outer"
    def inner():
        x = "inner"
        return x
    return x
""",
            "x",
            2,
        ),
        (
            """
def example():
    x = "first"
    print(x)
    x = "second"
    print(x)
""",
            "x",
            2,
        ),
        (
            """
def example():
    x: str = "first"
    print(x)
    x: str = "second"
    print(x)
""",
            "x",
            2,
        ),
    ],
    ids=["scope-isolation", "multiple-plain-assignments", "multiple-annotated-assignments"],
)
def test_lifecycle_count_for_variable(source: str, var_name: str, count: int) -> None:
    assert _lifecycle_count(source, var_name) == count


def test_self_referential_assignment_correctly_tracked() -> None:
    source = """
def example():
    x = 1
    x = x + 1
    print(x)
    return x
"""
    # Second assignment (x = x + 1) has two uses (print and return). First
    # assignment (x = 1) has one use (x + 1 RHS). Neither should be
    # flagged as redundant because both have multiple uses.
    assert _check(source) == []


def test_augmented_assignment_tracks_usage() -> None:
    source = """
def example():
    x = 1
    x += 2
    print(x)
"""
    lifecycle = _lifecycle_for(source, "x")
    # Two uses: the read in `x += 2` (augmented assignment) and the use
    # in `print(x)`.
    assert len(lifecycle.uses) == 2


def test_repeated_augmented_assignment_reuses_existing_uses_key() -> None:
    # Branch coverage: a second augmented assignment to the same variable
    # in the same scope appends to the existing self.uses[key] list
    # rather than recreating it.
    source = """
def example():
    x = 0
    x += 1
    x += 2
"""
    lifecycle = _lifecycle_for(source, "x")
    # Each `x += n` counts as one use (the implicit read).
    assert len(lifecycle.uses) == 2


def test_decorator_use_is_tracked_by_variable_tracker() -> None:
    source = """
def outer():
    app = make_app()

    @app.route("/")
    def index():
        pass

    return app
"""
    lifecycle = _lifecycle_for(source, "app")
    # Two uses: @app.route("/") and return app.
    assert len(lifecycle.uses) == 2


def test_class_decorator_use_is_tracked() -> None:
    source = """
def factory():
    validator = build_validator()

    @validator.register
    class Rule:
        pass

    return validator
"""
    lifecycle = _lifecycle_for(source, "validator")
    # Two uses: @validator.register (decorator) and return validator.
    assert len(lifecycle.uses) == 2


def test_track_attribute_assignment_with_non_name_base() -> None:
    # Branch coverage: when the target of an assignment is something like
    # ``func().attr = v`` (a method-call result), unwinding the Attribute
    # chain leads to a Call node, not a Name.
    # _track_attribute_or_subscript_base_usage must skip tracking rather
    # than crashing.
    source = """
def outer():
    get_obj().attr = "value"
    return 42
"""
    # Must not raise; call-result targets are silently skipped.
    VariableTracker(source).visit(ast.parse(source))


def test_track_attribute_assignment_key_already_in_uses() -> None:
    # Branch coverage: when the same variable is the base of two separate
    # attribute assignments (``obj.x = 1`` then ``obj.y = 2``), the second
    # call to _track_attribute_or_subscript_base_usage finds the key
    # already present in self.uses and must append rather than create a
    # new list.
    source = """
def outer():
    obj = make_obj()
    obj.x = 1
    obj.y = 2
    return obj
"""
    lifecycle = _lifecycle_for(source, "obj")
    # obj is used in: obj.x = 1, obj.y = 2, return obj -> 3 uses.
    assert len(lifecycle.uses) == 3


def test_in_comprehension_flag_set_correctly() -> None:
    source = """
def func(obj, items):
    cached = obj.attr
    result = [x for x in items if x == cached]
    return result
"""
    lifecycle = _lifecycle_for(source, "cached")
    assert len(lifecycle.uses) == 1
    assert lifecycle.uses[0].in_comprehension is True


def test_in_comprehension_flag_false_for_normal_usage() -> None:
    source = """
def func():
    x = "foo"
    print(x)
"""
    lifecycle = _lifecycle_for(source, "x")
    assert all(not use.in_comprehension for use in lifecycle.uses)


def test_variable_tracker_scope_isolation() -> None:
    assert (
        _lifecycle_count(
            """
def outer():
    x = "outer"
    def inner():
        x = "inner"
        return x
    return x
""",
            "x",
        )
        == 2
    )


def test_get_source_segment_error_handling() -> None:
    node = ast.Constant(value=1, lineno=-1, col_offset=-1)
    assert VariableTracker("x = 1")._get_source_segment(node) == ""


# ---------------------------------------------------------------------------
# detect_redundancy() / lifecycle properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "var_name", "pattern"),
    [
        (
            """
def func():
    x = "foo"
    print(x)
""",
            "x",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
def func():
    x = "foo"
    y = "bar"
    z = "baz"
    print(x)
""",
            "x",
            PatternType.SINGLE_USE,
        ),
        (
            # An augmented-assignment target (`x += 1`) can't be inlined —
            # the result (`5 += 1`) is invalid syntax — and isn't the
            # read-then-forward pattern TRI005 targets anyway.
            """
def func():
    x = 5
    x += 1
""",
            "x",
            None,
        ),
    ],
    ids=["immediate-use", "single-use-with-intervening-statements", "augmented-assignment-is-not-redundant"],
)
def test_detect_redundancy(source: str, var_name: str, pattern: PatternType | None) -> None:
    assert detect_redundancy(_lifecycle_for(source, var_name)) == pattern


def test_match_statement_case_body_use_not_immediate() -> None:
    # A use inside a match/case body must be treated as control flow (like
    # an if/elif branch), not as an ordinary use that always runs —
    # otherwise it could be reported/autofixed as if the case always
    # matched.
    source = """
def f(command):
    value = make()
    match command:
        case "go":
            sink(value)
"""
    assert all("'value'" not in v.message for v in _check(source))


def test_lifecycle_no_uses_not_immediate() -> None:
    rhs_node = ast.parse("func()", mode="eval").body
    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source="func()",
        scope_id=0,
        has_type_annotation=False,
    )
    lifecycle = VariableLifecycle(assignment=assignment, uses=[])

    assert lifecycle.is_immediate_use is False
    assert lifecycle.is_single_use is False


def test_lifecycle_is_immediate_use_with_closure() -> None:
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

    # Even though stmt_index suggests immediate use, it should return
    # False because the use is in a different scope (closure).
    assert lifecycle.is_immediate_use is False
    assert lifecycle.is_single_use is True


def test_evaluation_order_children_assign_yields_value_before_targets() -> None:
    # Branch coverage + contract test: for ast.Assign,
    # _evaluation_order_children must yield the RHS value before the
    # target(s) — the opposite of Assign._fields, which lists targets
    # first — matching Python's real evaluate-RHS-then-target(s) order.
    tree = ast.parse("x.attr = value_expr")
    assign_node = tree.body[0]
    assert isinstance(assign_node, ast.Assign)
    children = list(_evaluation_order_children(assign_node))

    assert children == [(assign_node.value, False), (assign_node.targets[0], False)]


# ---------------------------------------------------------------------------
# is_preceded_by_call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "var_name", "expected"),
    [
        (
            # Regression (2nd P1 in issue #22's fix): the evaluation-order
            # check must be AST-based, not line/column-text-based — a
            # text heuristic sees an empty same-line prefix for `x` here
            # and wrongly calls it safe, even though side_effect() (on the
            # previous physical line, same statement) already ran first.
            """
def f():
    x = make()
    sink(
        side_effect(),
        x,
    )
""",
            "x",
            True,
        ),
        (
            # Attribute/subscript access (e.g. a @property getter) can
            # run arbitrary code just like a call, so a sibling attribute
            # access must count as "preceding" too.
            """
def f():
    value = make()
    sink(obj.property, value)
""",
            "value",
            True,
        ),
        (
            # ast.Dict's own _fields order is ('keys', 'values') — every
            # key, then every value — which does NOT match Python's real
            # per-pair evaluation order.
            """
def f():
    x = make()
    d = {"a": side_effect(), x: 1}
""",
            "x",
            True,
        ),
        (
            # Branch coverage: a dict literal that doesn't contain the
            # target at all (and has no calls in it) must be walked fully.
            """
def f():
    x = make()
    sink({"a": 1, "b": 2}, x)
""",
            "x",
            False,
        ),
        (
            # x as the very first key (nothing evaluates before it, not
            # even its own paired value) is still safe.
            """
def f():
    x = make()
    d = {x: 1, "b": side_effect()}
""",
            "x",
            False,
        ),
        (
            # Branch coverage: a None key marks **unpacking (evaluates
            # only the paired value) — a value after one must still see it
            # as a preceding effect if that unpacked expression is a call.
            """
def f():
    x = make()
    d = {**other(), "b": x}
""",
            "x",
            True,
        ),
        (
            # Python evaluates `obj.attr = value` by computing `value`
            # *before* `obj` — the opposite of ast.Assign's own _fields
            # order.
            """
def f():
    x = make()
    x.attr = side_effect()
""",
            "x",
            True,
        ),
        (
            # Exactly one of a ternary's body/orelse ever runs — a call
            # used there might not execute at all.
            """
def f():
    x = make()
    sink(x if flag else 0)
""",
            "x",
            True,
        ),
        (
            # `and`/`or` short-circuit, so only the first operand is
            # guaranteed to evaluate.
            """
def f():
    x = make()
    sink(flag and x)
""",
            "x",
            True,
        ),
        (
            # Branch coverage: a ternary that doesn't contain the target
            # at all must still be walked fully — and since IfExp's `test`
            # invokes `__bool__`, it's still a preceding effect.
            """
def f():
    x = make()
    sink(a if flag else b, x)
""",
            "x",
            True,
        ),
        (
            # Branch coverage: a BoolOp that doesn't contain the target at
            # all must still be walked fully — and since BoolOp invokes
            # `__bool__` on its left operand, it's still a preceding
            # effect.
            """
def f():
    x = make()
    sink(flag and other, x)
""",
            "x",
            True,
        ),
        (
            # The BoolOp fix must stay precise: the *first* operand always
            # evaluates unconditionally, so `sink(x and flag)` is safe.
            """
def f():
    x = make()
    sink(x and flag)
""",
            "x",
            False,
        ),
        (
            # The issue's own motivating idiom must remain safe: `check`
            # is the receiver of `check.check(...)`, evaluated before any
            # of that call's own arguments — nothing precedes it.
            """
def f():
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)
""",
            "check",
            False,
        ),
    ],
    ids=[
        "multiline-statement",
        "attribute-sibling",
        "dict-key-after-earlier-pair",
        "dict-sibling-without-calls",
        "dict-first-key",
        "dict-value-after-unpacking",
        "assign-target-base-after-value",
        "ifexp-branch",
        "boolop-non-first-operand",
        "ifexp-sibling-without-target",
        "boolop-sibling-without-target",
        "boolop-first-operand",
        "method-call-receiver",
    ],
)
def test_is_preceded_by_call(source: str, var_name: str, *, expected: bool) -> None:
    lifecycle = _lifecycle_for(source, var_name)
    assert is_preceded_by_call(lifecycle.uses[0]) is expected


def test_is_preceded_by_call_defaults_to_true_for_unknown_container() -> None:
    # When the enclosing statement (or node) can't be determined,
    # is_preceded_by_call must default to the conservative "unsafe" answer
    # rather than guessing.
    use = UsageInfo(var_name="x", line=1, col=0, stmt_index=0, context="unknown", scope_id=1)
    assert is_preceded_by_call(use) is True


# ---------------------------------------------------------------------------
# _has_inline_comment / _has_comment_above
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("x = value  # this is a comment", True),
        ("x = value", False),
        ('x = "hello # world"', False),  # `#` inside a string, not a comment.
        ('x = "foo"  # comment', True),  # `#` in both string and as a comment.
        ('x = "test#test"  # real comment', True),  # String with `#` followed by a real comment.
        ('x = "test # not a comment"', False),  # Only a string containing `#`.
        ('x = ""  # comment', True),  # Empty string then comment.
        # Regression: a single-quote inside a double-quoted string (e.g.
        # "it's") must not be mistaken for a comment delimiter.
        ('x = "it\'s fine"', False),
        ('x = "it\'s fine"  # comment', True),
    ],
    ids=[
        "with-comment",
        "without-comment",
        "hash-inside-string",
        "hash-in-string-and-comment",
        "hash-in-string-then-real-comment",
        "only-string-with-hash",
        "empty-string-then-comment",
        "mismatched-quote-in-string",
        "mismatched-quote-with-real-comment",
    ],
)
def test_has_inline_comment(line: str, *, expected: bool) -> None:
    assert _has_inline_comment(1, [line]) is expected


def test_has_inline_comment_out_of_bounds() -> None:
    lines = ["x = value"]
    assert _has_inline_comment(0, lines) is False
    assert _has_inline_comment(5, lines) is False


def test_has_comment_above_first_line_returns_false() -> None:
    # Branch coverage: an assignment on line 1 has no line above to check.
    lines = ['x = "foo"', "process(x)"]
    assert _has_comment_above(1, lines) is False
