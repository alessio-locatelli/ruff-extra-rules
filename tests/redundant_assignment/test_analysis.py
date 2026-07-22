from __future__ import annotations

import ast

import pytest

from pre_commit_hooks.ast_checks._base import classify_comment_lines
from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
    AssignmentInfo,
    PatternType,
    UsageInfo,
    VariableLifecycle,
    VariableTracker,
    _evaluation_order_children,
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


def test_named_expr_rebinding_skipped_for_global_variable() -> None:
    # Branch coverage: a walrus target declared `global` in this scope
    # must not be tracked as a rebinding use here — matching
    # visit_AugAssign's own global exclusion, since a global rebinding
    # isn't a local snapshot hazard this tracker resolves.
    source = """
def func():
    global x
    return (x := 1)
"""
    # Must not raise; global walrus targets are silently skipped.
    VariableTracker(source).visit(ast.parse(source))


def test_tuple_unpacking_rebinding_skipped_for_global_variable() -> None:
    # Branch coverage: a tuple-unpacking target declared `global` in this
    # scope must not be recorded as a rebinding marker here either — same
    # exclusion as the plain-Name assignment path and the walrus case
    # above.
    source = """
def func():
    global x
    x, y = compute()
"""
    VariableTracker(source).visit(ast.parse(source))


def test_starred_tuple_target_recorded_as_rebinding() -> None:
    # A Starred element inside a tuple-unpacking target (`a, *b = ...`)
    # rebinds `first` just like a plain Name element would.
    source = """
def func():
    first = None
    if cond:
        first, *rest = compute()
    return first
"""
    assert _lifecycle_count(source, "first") == 2


def test_attribute_target_nested_in_tuple_tracked_as_usage() -> None:
    # An Attribute/Subscript element inside a tuple-unpacking target
    # (`obj.attr, first = ...`) reads `obj`, same as a bare `obj.attr =
    # value` would.
    source = """
def func(obj):
    obj.attr, first = compute()
    return first
"""
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    obj_uses = tracker.uses[next(key for key in tracker.uses if key[1] == "obj")]
    assert any(use.context == "attribute_or_subscript_assignment" for use in obj_uses)


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
        (
            # Issue #76 calibration case: a "single-purpose accessor
            # assigned then immediately mutated" (`state = me.state(State);
            # state.value = 5`) is never even read — the sole use is a
            # mutation, not a pass-through — so it isn't a redundant
            # assignment either, regardless of how low its semantic score
            # would otherwise be.
            """
def func(me):
    state = me.state(State)
    state.value = 5
""",
            "state",
            None,
        ),
        (
            # "Snapshot the old value before reassigning it" (issue #74):
            # `value` is rebound between the tracked assignment and its
            # use, so `old_value` and a later inlined `value` would read
            # genuinely different states — this isn't a redundant
            # assignment at all.
            """
def func(value):
    old_value = value
    value = compute_new()
    use(old_value)
""",
            "old_value",
            None,
        ),
        (
            # Same hazard via an augmented assignment, which both mutates
            # and rebinds `value` — the "reassign or mutate" half of issue
            # #74's phrasing not covered by the plain-reassignment case
            # above.
            """
def func(value):
    old_value = value
    value += 1
    use(old_value)
""",
            "old_value",
            None,
        ),
        (
            # Same hazard, but the reassignment mutates the exact
            # attribute reference (`obj.attr`) the RHS read from, rather
            # than rebinding a plain name. An intervening, unrelated read
            # of `obj` between the assignment and the mutation must not
            # itself be mistaken for the hazard.
            """
def func(obj):
    old_attr = obj.attr
    log(obj)
    obj.attr = compute_new()
    use(old_attr)
""",
            "old_attr",
            None,
        ),
        (
            # Same hazard via an augmented assignment to the attribute
            # reference itself (`obj.attr += 1` both reads and reassigns
            # `obj.attr`, mirroring the plain-name augmented-assignment
            # case above).
            """
def func(obj):
    old_attr = obj.attr
    obj.attr += 1
    use(old_attr)
""",
            "old_attr",
            None,
        ),
        (
            # An Attribute RHS whose base doesn't unwind to a plain name
            # (e.g. a call result) has no trackable reference to check for
            # reassignment, so it isn't disqualified by the issue #74
            # guard — it's just an ordinary redundant assignment.
            """
def func():
    old = get_obj().attr
    use(old)
""",
            "old",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            # `obj` is read again *after* old_attr's own use, with no
            # mutation anywhere — not a snapshot hazard. This later,
            # out-of-range read must stop the range scan rather than being
            # mistaken for an in-range one.
            """
def func(obj):
    old_attr = obj.attr
    log(obj)
    use(old_attr)
    log(obj)
""",
            "old_attr",
            PatternType.SINGLE_USE,
        ),
        (
            # `x` is reassigned only in the `else` branch, mutually
            # exclusive with the `if` branch that uses `old` — `x` can
            # never actually change on the path that reaches `return old`.
            # Statements nested in different branches of the same
            # if/else share one coarse stmt_index, so this must rely on
            # source order (the reassignment is textually after the use)
            # rather than stmt_index alone to avoid a false hazard.
            """
def func(cond, x):
    old = x
    if cond:
        return old
    else:
        x = 99
""",
            "old",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            # A walrus expression rebinds `x` within the same statement
            # that uses `v` — inlining `v` as `x` here would read the
            # just-rebound value (2) instead of the one captured at
            # assignment time.
            """
def func(x):
    v = x
    return (x := 2, v)
""",
            "v",
            None,
        ),
        (
            # Regression: `old`'s assignment, the `self._server_session =
            # ...` reassignment, and `old`'s own use are all nested inside
            # the same top-level `if`, so they share one coarse
            # stmt_index. Bisecting the hazard scan on stmt_index (instead
            # of line) skipped straight past the reassignment, silently
            # missing this snapshot-before-reassignment hazard.
            """
def func(self):
    if isinstance(self._server_session, EmptyServerSession):
        old = self._server_session
        self._server_session = self._client.get_server_session()
        if old.started_retryable_write:
            self._server_session.inc_transaction_id()
""",
            "old",
            None,
        ),
        (
            # Regression: two semicolon-separated statements on one
            # physical line have the same `line` but distinct
            # `stmt_index`. Bisecting the hazard scan on line alone (the
            # fix for the coarse-stmt_index case above) skipped straight
            # past this same-line reassignment.
            """
def func(x):
    old = x; x = 2
    return old
""",
            "old",
            None,
        ),
    ],
    ids=[
        "immediate-use",
        "single-use-with-intervening-statements",
        "augmented-assignment-is-not-redundant",
        "mutation-only-single-use-is-not-redundant",
        "snapshot-before-name-reassignment-is-not-redundant",
        "snapshot-before-name-augmented-reassignment-is-not-redundant",
        "snapshot-before-attribute-reassignment-is-not-redundant",
        "snapshot-before-attribute-augmented-reassignment-is-not-redundant",
        "attribute-rhs-with-non-name-base-is-not-a-snapshot-hazard",
        "later-out-of-range-read-is-not-a-snapshot-hazard",
        "reassignment-in-mutually-exclusive-else-branch-is-not-a-snapshot-hazard",
        "snapshot-before-named-expression-rebinding-is-not-redundant",
        "snapshot-before-attribute-reassignment-sharing-coarse-stmt-index-is-not-redundant",
        "snapshot-before-name-reassignment-sharing-a-physical-line-is-not-redundant",
    ],
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
# classify_comment_lines (used for AssignmentInfo.has_inline_comment /
# has_comment_above)
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
        # Regression: a naive single-char-lookback escape check used to
        # treat this closing quote as itself escaped (only the immediately
        # preceding backslash was checked, not the full run), leaving the
        # scanner stuck "inside" the string through the rest of the line —
        # silently hiding a real trailing comment, which --fix would then
        # have deleted along with the assignment it decorated.
        ('x = "\\\\"  # comment', True),
        # Regression: an embedded, unescaped quote inside a triple-quoted
        # string desyncs a single-quote-at-a-time toggle from the real
        # triple-quote delimiter, again hiding a real trailing comment.
        ('x = """a"b"""  # comment', True),
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
        "escaped-backslash-before-closing-quote",
        "embedded-quote-in-triple-quoted-string",
    ],
)
def test_classify_comment_lines_trailing_comment(line: str, *, expected: bool) -> None:
    _comment_only, trailing = classify_comment_lines(line + "\n")
    assert (1 in trailing) is expected


def test_classify_comment_lines_comment_only_line() -> None:
    comment_only, trailing = classify_comment_lines('# standalone\nx = "foo"\n')
    assert comment_only == {1}
    assert trailing == set()


def test_classify_comment_lines_no_comments() -> None:
    assert classify_comment_lines("x = 1\nprint(x)\n") == (set(), set())


def test_has_comment_above_true_for_standalone_comment() -> None:
    source = """
def f():
    # documented on purpose
    data = "foo"
    print(data)
"""
    lifecycle = _lifecycle_for(source, "data")
    assert lifecycle.assignment.has_comment_above is True


def test_has_comment_above_false_first_statement_in_function() -> None:
    # Branch coverage: an assignment with no comment line above it.
    source = """
def f():
    data = "foo"
    print(data)
"""
    lifecycle = _lifecycle_for(source, "data")
    assert lifecycle.assignment.has_comment_above is False
