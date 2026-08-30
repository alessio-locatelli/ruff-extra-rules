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
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import AggressivenessLevel
from tests.redundant_assignment._helpers import _check


def _tracker(source: str) -> VariableTracker:
    return VariableTracker(source, *classify_comment_lines(source), ast.parse(source))


def _lifecycle_for(source: str, var_name: str) -> VariableLifecycle:
    tracker = _tracker(source)
    tracker.visit(ast.parse(source))
    return next(lc for lc in tracker.build_lifecycles() if lc.assignment.var_name == var_name)


def _lifecycle_count(source: str, var_name: str) -> int:
    tracker = _tracker(source)
    tracker.visit(ast.parse(source))
    return len([lc for lc in tracker.build_lifecycles() if lc.assignment.var_name == var_name])


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
    assert _check(source) == []


def test_augmented_assignment_tracks_usage() -> None:
    source = """
def example():
    x = 1
    x += 2
    print(x)
"""
    lifecycle = _lifecycle_for(source, "x")
    assert len(lifecycle.uses) == 2


def test_repeated_augmented_assignment_reuses_existing_uses_key() -> None:
    source = """
def example():
    x = 0
    x += 1
    x += 2
"""
    lifecycle = _lifecycle_for(source, "x")
    assert len(lifecycle.uses) == 2


def test_try_star_assignment_is_protected_from_redundancy_reporting() -> None:
    source = """
def f():
    try:
        value = load()
    except* ValueError:
        pass

    try:
        use(value)
    except ValueError:
        pass
"""
    lifecycle = _lifecycle_for(source, "value")

    assert lifecycle.assignment.in_try is True
    assert lifecycle.assignment.in_control_flow is True
    assert _check(source, level=AggressivenessLevel.PERMISSIVE) == []


@pytest.mark.parametrize(
    "source",
    [
        """
def f():
    try:
        pass
    except ValueError:
        value = load()
        return value
""",
        """
def f():
    try:
        pass
    except ValueError:
        pass
    else:
        value = load()
        return value
""",
        """
def f():
    try:
        pass
    except ValueError:
        pass
    finally:
        value = load()
        return value
""",
    ],
)
def test_try_depth_does_not_include_non_body_clauses(source: str) -> None:
    lifecycle = _lifecycle_for(source, "value")

    assert lifecycle.assignment.in_try is False
    assert lifecycle.assignment.in_control_flow is True
    assert len(_check(source, level=AggressivenessLevel.PERMISSIVE)) == 1


@pytest.mark.parametrize(
    "source",
    [
        """
def outer():
    try:
        def inner():
            value = load()
            return value
    except ValueError:
        pass
""",
        """
def outer():
    try:
        class Inner:
            def method(self):
                value = load()
                return value
    except ValueError:
        pass
""",
    ],
)
def test_try_depth_does_not_include_deferred_function_bodies(source: str) -> None:
    lifecycle = _lifecycle_for(source, "value")

    assert lifecycle.assignment.in_try is False
    assert lifecycle.assignment.in_control_flow is True
    assert len(_check(source, level=AggressivenessLevel.PERMISSIVE)) == 1


def test_function_defaults_are_visited_in_the_enclosing_try_context() -> None:
    source = """
def outer():
    try:
        value = source()
        @decorate(value)
        def inner(*, required, argument=value):
            return argument
    except ValueError:
        pass
"""
    lifecycle = _lifecycle_for(source, "value")

    assert len(lifecycle.uses) == 2


def test_named_expr_rebinding_skipped_for_global_variable() -> None:
    source = """
def func():
    global x
    return (x := 1)
"""
    _tracker(source).visit(ast.parse(source))


def test_tuple_unpacking_rebinding_skipped_for_global_variable() -> None:
    source = """
def func():
    global x
    x, y = compute()
"""
    _tracker(source).visit(ast.parse(source))


def test_starred_tuple_target_recorded_as_rebinding() -> None:
    source = """
def func():
    first = None
    if cond:
        first, *rest = compute()
    return first
"""
    assert _lifecycle_count(source, "first") == 2


def test_attribute_target_nested_in_tuple_tracked_as_usage() -> None:
    source = """
def func(obj):
    obj.attr, first = compute()
    return first
"""
    tracker = _tracker(source)
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
    assert len(lifecycle.uses) == 2


def test_track_attribute_assignment_with_non_name_base() -> None:
    source = """
def outer():
    get_obj().attr = "value"
    return 42
"""
    _tracker(source).visit(ast.parse(source))


def test_track_attribute_assignment_key_already_in_uses() -> None:
    source = """
def outer():
    obj = make_obj()
    obj.x = 1
    obj.y = 2
    return obj
"""
    lifecycle = _lifecycle_for(source, "obj")
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


@pytest.mark.parametrize(
    ("source", "var_name", "expected"),
    [
        (
            """
def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller():
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row=days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            True,
        ),
        (
            """
def caller():
    x = 42
    return func(other=x)
""",
            "x",
            False,
        ),
        (
            """
def caller():
    x = 42
    return func(x)
""",
            "x",
            False,
        ),
        (
            """
def caller():
    x = 42
    return func(x=x + 1)
""",
            "x",
            False,
        ),
    ],
    ids=["keyword-echo", "different-keyword", "positional", "expression-value"],
)
def test_is_keyword_argument_echo(source: str, var_name: str, *, expected: bool) -> None:
    lifecycle = _lifecycle_for(source, var_name)
    assert lifecycle.uses[0].is_keyword_argument_echo is expected


@pytest.mark.parametrize(
    ("source", "var_name", "expected"),
    [
        (
            """
def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller():
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            True,
        ),
        (
            """
def caller():
    x = 42
    return func(x)
""",
            "x",
            False,
        ),
        (
            """
def func(x):
    return x


def func(x):
    return x


def caller():
    x = 42
    return func(x)
""",
            "x",
            False,
        ),
        (
            """
@decorator
def func(x):
    return x


def caller():
    x = 42
    return func(x)
""",
            "x",
            False,
        ),
        (
            """
def caller(obj):
    x = 42
    return obj.func(x)
""",
            "x",
            False,
        ),
        (
            """
def func(x, y):
    return x, y


def caller(items):
    x = 42
    return func(*items, x)
""",
            "x",
            False,
        ),
        (
            """
def func(*args):
    return args


def caller():
    x = 42
    return func(1, 2, x)
""",
            "x",
            False,
        ),
        (
            """
def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller(func):
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller():
    func = other_func
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
def outer():
    def helper(days_with_routes_in_a_row):
        return days_with_routes_in_a_row


def caller():
    days_with_routes_in_a_row = 42
    return helper(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
class Foo:
    def helper(self, days_with_routes_in_a_row):
        return days_with_routes_in_a_row


def caller():
    days_with_routes_in_a_row = 42
    return helper(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller():
    days_with_routes_in_a_row = 42
    del func
    return func(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller(value):
    days_with_routes_in_a_row = 42
    match value:
        case _ as func:
            return func(days_with_routes_in_a_row)
    return None
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
from other_module import *


def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller():
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller(value):
    days_with_routes_in_a_row = 42
    match value:
        case {"key": _, **func}:
            return func(days_with_routes_in_a_row)
    return None
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
def func(days_with_routes_in_a_row):
    return days_with_routes_in_a_row


def caller[func]():
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
    ],
    ids=[
        "positional-echo",
        "undefined-callee-not-echo",
        "ambiguous-name-not-echo",
        "decorated-callee-not-echo",
        "attribute-call-not-echo",
        "preceding-starred-arg-not-echo",
        "index-consumed-by-vararg-not-echo",
        "callee-shadowed-by-parameter-not-echo",
        "callee-shadowed-by-local-reassignment-not-echo",
        "callee-is-nested-function-not-echo",
        "callee-is-method-not-echo",
        "callee-shadowed-by-del-not-echo",
        "callee-shadowed-by-match-capture-not-echo",
        "wildcard-import-not-echo",
        "callee-shadowed-by-match-mapping-rest-not-echo",
        "callee-shadowed-by-type-parameter-not-echo",
    ],
)
def test_is_positional_argument_echo(source: str, var_name: str, *, expected: bool) -> None:
    lifecycle = _lifecycle_for(source, var_name)
    assert lifecycle.uses[0].is_positional_argument_echo is expected


def test_for_iterator_use_is_not_in_loop() -> None:
    source = """
def func():
    value = compute()
    for item in consume(value):
        pass
"""
    lifecycle = _lifecycle_for(source, "value")
    assert lifecycle.uses[0].in_loop is False


def test_for_else_use_is_not_in_loop_but_is_control_flow() -> None:
    source = """
def func(items):
    value = compute()
    for item in items:
        pass
    else:
        use(value)
"""
    lifecycle = _lifecycle_for(source, "value")
    assert lifecycle.uses[0].in_loop is False
    assert lifecycle.uses[0].in_control_flow is True


def test_while_else_use_is_not_in_loop_but_is_control_flow() -> None:
    source = """
def func(cond):
    value = compute()
    while cond:
        pass
    else:
        use(value)
"""
    lifecycle = _lifecycle_for(source, "value")
    assert lifecycle.uses[0].in_loop is False
    assert lifecycle.uses[0].in_control_flow is True


def test_for_else_use_after_possible_break_is_not_reported() -> None:
    source = """
def func(items):
    x = make()
    for item in items:
        if item == x:
            break
    else:
        return x
"""
    assert _check(source, level=AggressivenessLevel.PERMISSIVE) == []


def test_suspension_precedes_use_fallback_for_use_without_node() -> None:
    source = """
async def f(obj):
    cached = obj.attr
    cached += await something()
"""
    lifecycle = _lifecycle_for(source, "cached")
    assert lifecycle.rhs_reference_reassigned_before_use is True


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


def test_get_source_segment_returns_empty_string_without_end_position() -> None:
    node = ast.Constant(value=1, lineno=-1, col_offset=-1)
    assert _tracker("x = 1")._get_source_segment(node) == ""


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
            """
def func():
    x = 5
    x += 1
""",
            "x",
            None,
        ),
        (
            """
def func(me):
    state = me.state(State)
    state.value = 5
""",
            "state",
            None,
        ),
        (
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
            """
def func():
    old = get_obj().attr
    use(old)
""",
            "old",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
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
            """
def func(x):
    v = x
    return (x := 2, v)
""",
            "v",
            None,
        ),
        (
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
            """
def func(x):
    old = x; x = 2
    return old
""",
            "old",
            None,
        ),
        (
            """
def func(buf, size_location):
    resume_location = buf.tell()
    length = buf.tell()
    buf.seek(size_location)
    buf.write(pack(length - size_location))
    buf.seek(resume_location)
""",
            "resume_location",
            None,
        ),
        (
            """
def func(obj):
    value = obj.compute()
    log(other)
    return value
""",
            "value",
            PatternType.SINGLE_USE,
        ),
        (
            """
def func(obj):
    value = obj.compute(
        obj,
    )
    return value
""",
            "value",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
def func(obj):
    value = obj.compute()
    obj.consume(value)
""",
            "value",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
def func():
    start_bytes = get_cache_bytes()
    yield
    return get_cache_bytes() - start_bytes
""",
            "start_bytes",
            None,
        ),
        (
            """
async def func(obj):
    cached = obj.attr
    await other()
    return cached
""",
            "cached",
            None,
        ),
        (
            """
async def func(obj, cond):
    if cond:
        cached = obj.attr
        await other()
        return cached
    return None
""",
            "cached",
            None,
        ),
        (
            """
async def func(obj, cond):
    if cond:
        cached = obj.attr
        consume(cached)
        await other()
""",
            "cached",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
async def func(obj, cond):
    if cond:
        cached = obj.attr; await other(); return cached
    return None
""",
            "cached",
            None,
        ),
        (
            """
def func(obj):
    value = obj.compute()
    yield value
""",
            "value",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
def func():
    start_bytes = get_cache_bytes()
    yield from other()
    return get_cache_bytes() - start_bytes
""",
            "start_bytes",
            None,
        ),
        (
            """
def func(self, items):
    topology = self._get_topology()
    for item in items:
        use(item, topology)
""",
            "topology",
            None,
        ),
        (
            """
def func(items):
    for item in items:
        value = compute(item)
        use(value)
""",
            "value",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
def func(items):
    x = 5
    for i in items:
        sink(i, x)
""",
            "x",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
def func():
    value = 1
    for item in consume(value):
        pass
""",
            "value",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
async def func(obj):
    await something()
    cached = obj.attr
    return cached
""",
            "cached",
            PatternType.IMMEDIATE_SINGLE_USE,
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
        "snapshot-before-method-call-receiver-mutation-is-not-redundant",
        "method-call-receiver-with-no-other-usage-is-still-redundant",
        "multiline-call-receiver-self-reference-is-not-a-mutation-hazard",
        "use-statement-receiver-self-reference-is-not-a-mutation-hazard",
        "snapshot-before-yield-suspension-point-is-not-redundant",
        "snapshot-before-await-suspension-point-is-not-redundant",
        "snapshot-before-await-in-sibling-statement-sharing-coarse-stmt-index-is-not-redundant",
        "await-in-sibling-statement-after-use-sharing-coarse-stmt-index-is-still-redundant",
        "semicolon-separated-await-sharing-line-and-stmt-index-is-not-redundant",
        "yield-at-the-use-itself-is-not-a-suspension-hazard",
        "snapshot-before-yield-from-suspension-point-is-not-redundant",
        "hoisted-value-used-inside-a-later-loop-is-not-redundant",
        "assignment-and-use-sharing-the-same-loop-is-not-a-hoist-hazard",
        "constant-hoisted-before-a-loop-is-still-redundant",
        "use-in-for-loop-iterator-is-not-a-hoist-hazard",
        "suspension-point-before-assignment-is-not-a-hazard",
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
            # The evaluation-order check must be AST-based, not
            # line/column-text-based — a text heuristic sees an empty
            # same-line prefix for `x` here and wrongly calls it safe, even
            # though side_effect() (on the previous physical line, same
            # statement) already ran first.
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
    check = MeaninglessVarsCheck()
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
        # A single-quote inside a double-quoted string (e.g.
        # "it's") must not be mistaken for a comment delimiter.
        ('x = "it\'s fine"', False),
        ('x = "it\'s fine"  # comment', True),
        # A naive single-char-lookback escape check would treat this
        # closing quote as itself escaped (only the immediately preceding
        # backslash checked, not the full run), leaving the scanner stuck
        # "inside" the string through the rest of the line — silently
        # hiding a real trailing comment, which --fix would then delete
        # along with the assignment it decorated.
        ('x = "\\\\"  # comment', True),
        # An embedded, unescaped quote inside a triple-quoted
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
