from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
    AssignmentInfo,
    UsageInfo,
    VariableLifecycle,
)
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
    _adds_verbosity_or_context,
    _contains_nondeterministic_call,
    _is_generic_call_result_name,
    _is_named_constant_pattern,
    _is_named_string_constant_pattern,
    _is_test_file,
    _would_require_parentheses,
    calculate_semantic_value,
    should_autofix,
)


def _make_single_use_lifecycle(
    rhs_source: str,
    rhs_node: ast.expr,
    var_name: str = "x",
    *,
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
    use_stmt_source = f"sink(side_effect(), {var_name})" if preceded_by_call else f"{var_name}.method()"
    use_stmt = ast.parse(use_stmt_source).body[0]
    use_node = next(
        n for n in ast.walk(use_stmt) if isinstance(n, ast.Name) and n.id == var_name and isinstance(n.ctx, ast.Load)
    )
    use = UsageInfo(
        var_name=var_name,
        line=2,
        col=0,
        stmt_index=1,
        context="unknown",
        scope_id=1,
        node=use_node,
        enclosing_stmt=use_stmt,
    )
    return VariableLifecycle(assignment=assignment, uses=[use])


def _lifecycle_no_node(rhs_source: str, var_name: str = "x") -> VariableLifecycle:
    """A lifecycle whose use has no real AST node attached (unknown context)."""
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assignment = AssignmentInfo(
        var_name=var_name,
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=rhs_source,
        scope_id=0,
        has_type_annotation=False,
    )
    return VariableLifecycle(
        assignment=assignment,
        uses=[UsageInfo(var_name=var_name, line=2, col=0, stmt_index=1, context="unknown", scope_id=0)],
    )


def _lifecycle_with_use_node(
    rhs_source: str,
    var_name: str = "x",
    use_stmt_source: str = "x.method()",
    # Defaults to immediate (assignment at stmt_index 0, use at 1) — an
    # Attribute/Call RHS use must be the very next statement to be
    # mechanically safe to inline (issue #76). Callers testing a
    # non-immediate use pass a larger value explicitly.
    use_stmt_index: int = 1,
) -> VariableLifecycle:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assignment = AssignmentInfo(
        var_name=var_name,
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=rhs_source,
        scope_id=0,
        has_type_annotation=False,
    )
    use_stmt = ast.parse(use_stmt_source).body[0]
    use_node = next(
        n for n in ast.walk(use_stmt) if isinstance(n, ast.Name) and n.id == var_name and isinstance(n.ctx, ast.Load)
    )
    return VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name=var_name,
                line=5,
                col=0,
                stmt_index=use_stmt_index,
                context="unknown",
                scope_id=0,
                node=use_node,
                enclosing_stmt=use_stmt,
            )
        ],
    )


# ---------------------------------------------------------------------------
# should_autofix
#
# Issue #76 unified autofix eligibility: it's no longer pattern-dependent
# and no longer gated by a semantic-score ceiling (that ceiling now only
# governs *reporting*, via should_report_violation's AggressivenessLevel).
# Autofix is purely mechanical safety — loop/control-flow position, RHS
# shape/arg count, line length, and call-reordering/deferral hazards.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rhs_source", "var_name"),
    [
        # "unknown" context, no real use node attached, so
        # is_preceded_by_call defaults to the conservative "unsafe" answer
        # regardless of RHS shape or variable name.
        ("get_value()", "x"),
        ("func(1, 2)", "x"),
        ("func()", "x"),
        ("func({k: v for k, v in items})", "x"),
        ("check()", "has_something"),
    ],
    ids=["simple-call", "call-with-simple-args", "no-args-call", "complex-call-args", "descriptive-name"],
)
def test_should_autofix_no_node(rhs_source: str, var_name: str) -> None:
    lifecycle = _lifecycle_no_node(rhs_source, var_name)
    assert should_autofix(lifecycle) is False


@pytest.mark.parametrize(
    ("rhs_source", "expected"),
    [
        ("obj.attr", True),
        ("func(key=value)", True),
        ("func(a, b, c)", False),  # Exceeds the 2-arg limit.
    ],
    ids=["attribute", "keywords", "complex-call-rejected"],
)
def test_should_autofix_with_real_node(rhs_source: str, *, expected: bool) -> None:
    lifecycle = _lifecycle_with_use_node(rhs_source)
    assert should_autofix(lifecycle) is expected


def test_should_autofix_allows_simple_zero_arg_call() -> None:
    lifecycle = _lifecycle_with_use_node("get_value()")
    assert should_autofix(lifecycle) is True


@pytest.mark.parametrize("rhs_source", ["get_value()", "obj.attr"], ids=["call", "attribute"])
def test_should_autofix_rejects_non_immediate_attribute_or_call_use(rhs_source: str) -> None:
    # Regression (code review of issue #76): Attribute/Call RHS can run
    # arbitrary code when evaluated, so inlining one is only safe when the
    # use is the very next statement — otherwise an intervening
    # statement's own effects could end up reordered relative to it, e.g.
    # `result = pop(queue); queue.clear(); return result` must not become
    # `queue.clear(); return pop(queue)`, which pops after clearing
    # instead of before. A Constant/Name RHS has no such hazard (see
    # test_should_autofix_returns_true_for_single_use_constant_rhs), so
    # this restriction is specific to Attribute/Call.
    lifecycle = _lifecycle_with_use_node(rhs_source, use_stmt_index=4)
    assert should_autofix(lifecycle) is False


def test_should_autofix_returns_false_for_loop_assignment() -> None:
    rhs_node = ast.parse('"foo"', mode="eval").body
    lifecycle = _make_single_use_lifecycle('"foo"', rhs_node, in_loop=True)
    assert should_autofix(lifecycle) is False


def test_should_autofix_returns_false_for_multiline_rhs() -> None:
    rhs_node = ast.parse('"foo"', mode="eval").body
    lifecycle = _make_single_use_lifecycle('"foo"\n"bar"', rhs_node)
    assert should_autofix(lifecycle) is False


def test_should_autofix_allows_long_var_name_when_line_length_is_fine() -> None:
    # Issue #76: the old var-name-length > 10 guard was only ever a crude
    # proxy for "inlining might push the line too long" — now superseded
    # entirely by the real line-length check below (with the actual use
    # line, or the RHS-length estimate as a fallback), so a long name on
    # its own is no longer disqualifying.
    rhs_node = ast.parse("something1", mode="eval").body
    lifecycle = _make_single_use_lifecycle("something1", rhs_node, var_name="myvariablex")
    assert should_autofix(lifecycle) is True


def test_should_autofix_returns_true_for_single_use_constant_rhs() -> None:
    rhs_node = ast.parse("42", mode="eval").body
    lifecycle = _make_single_use_lifecycle("42", rhs_node, var_name="x")
    assert should_autofix(lifecycle) is True


def test_should_autofix_returns_false_for_non_call_non_attr_rhs() -> None:
    # A list literal falls through every isinstance check and reaches the
    # final ``return False``.
    rhs_node = ast.parse("[1, 2, 3]", mode="eval").body
    lifecycle = _make_single_use_lifecycle("[1, 2, 3]", rhs_node)
    assert should_autofix(lifecycle) is False


def test_should_autofix_allows_zero_arg_call() -> None:
    # Issue #22 gap 2 (now generalized by issue #76 to every pattern, not
    # just SINGLE_USE): a zero-arg call with nothing else evaluating
    # before its use (within the use's statement) has no sibling operand
    # whose order inlining could disturb, so it's safe to inline.
    rhs_node = ast.parse("ForbidVarsCheck()", mode="eval").body
    lifecycle = _make_single_use_lifecycle("ForbidVarsCheck()", rhs_node, var_name="check", preceded_by_call=False)
    assert should_autofix(lifecycle) is True


def test_should_autofix_rejects_zero_arg_call_preceded_by_a_call() -> None:
    # Regression test (P1 caught in review of issue #22's fix): a
    # zero-arg call must not be inlined when a sibling expression
    # evaluates before it within the same statement, or inlining reverses
    # the original execution order. Example: `value = next_value();
    # sink(side_effect(), value)` must not become `sink(side_effect(),
    # next_value())` — that runs next_value() after side_effect() instead
    # of before it.
    rhs_node = ast.parse("next_value()", mode="eval").body
    lifecycle = _make_single_use_lifecycle("next_value()", rhs_node, var_name="value", preceded_by_call=True)
    assert should_autofix(lifecycle) is False


def test_should_autofix_allows_call_with_one_arg() -> None:
    # Issue #76: autofix eligibility is no longer pattern-dependent — a
    # call with a single simple argument used to be rejected whenever the
    # pattern was IMMEDIATE_SINGLE_USE/LITERAL_IDENTITY (only a bare
    # zero-arg carve-out was allowed there); the same ≤2-arg allowance
    # SINGLE_USE already had now applies uniformly.
    rhs_node = ast.parse("make_check(1)", mode="eval").body
    lifecycle = _make_single_use_lifecycle("make_check(1)", rhs_node, var_name="check")
    assert should_autofix(lifecycle) is True


def test_should_autofix_uses_real_use_line_length_when_available() -> None:
    # Issue #22 gap 1: should_autofix's line-length check must reflect the
    # *actual* use line when the caller can supply it, not just the
    # conservative RHS/var-name-based estimate — otherwise a violation can
    # be reported [FIXABLE] and then silently skipped by apply_fixes' own,
    # accurate length check.
    rhs_node = ast.parse("ast.parse(source)", mode="eval").body
    lifecycle = _make_single_use_lifecycle("ast.parse(source)", rhs_node, var_name="tree")

    # Without the real use line, the conservative RHS/var-name estimate
    # says inlining is safe (both are short).
    assert should_autofix(lifecycle) is True

    # _make_single_use_lifecycle fixes the use at line 2 (1-indexed).
    long_use_line = '    violations = check.check(Path("tests/test_something_with_a_long_name.py"), tree, source)'
    source_lines = ["def f():", long_use_line]
    assert should_autofix(lifecycle, source_lines=source_lines) is False


# ---------------------------------------------------------------------------
# _is_generic_call_result_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "expected"),
    [
        ("result", "get_value()", True),
        ("value", "get_value()", True),
        ("x", "get_value()", True),  # Too short (<=2 chars) to carry any domain meaning.
        ("check", "ForbidVarsCheck()", True),  # Restates the callee's own name.
        ("state", "me.state(State)", True),  # Restates the callee's own (attribute) name.
        ("warning", "conn.recv()", False),
        ("ci_headers", "CIMultiDict(headers)", False),
        # Branch coverage: the called function is neither a Name nor an
        # Attribute (e.g. a subscript), so callee_name stays None.
        ("something_descriptive", "funcs[0]()", False),
    ],
    ids=[
        "generic-result",
        "generic-value",
        "short-name",
        "self-referential-name-call",
        "self-referential-name-attribute-call",
        "descriptive-name",
        "descriptive-multipart-name",
        "call-with-subscript-func",
    ],
)
def test_is_generic_call_result_name(var_name: str, rhs_source: str, *, expected: bool) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert isinstance(rhs_node, ast.Call)
    assert _is_generic_call_result_name(var_name, rhs_node) is expected


# ---------------------------------------------------------------------------
# calculate_semantic_value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "minimum"),
    [
        ("x", "a + b", 15),  # BinOp adds 15.
        ("x", "1 if c else 0", 20),  # IfExp adds 20.
        ("has_permission", "check_something()", 50),  # has_ prefix.
        ("formatted_result", "raw_ts", 60),  # transformative verb.
        ("item_count", "len(items)", 40),  # descriptive suffix.
        ("result", "[x for x in items]", 30),  # list comprehension.
        ("result", "-value", 10),  # unary op.
        ("func", "lambda x: x * 2", 25),  # lambda.
        ("x", "a" * 85, 35),  # very long expression (80+ chars).
        ("x", "a" * 65, 25),  # long expression (60+ chars).
        ("x", "some_function_with_exactly_45_characters()", 10),  # medium length (40-60 chars).
        # Multipart name bonus in isolation: identical RHS, only the name
        # differs, and the multipart name scores strictly higher.
        ("user_email_address", "get_email()", 30),
    ],
    ids=[
        "binop",
        "ifexp",
        "descriptive-boolean-prefix",
        "transformative-verb",
        "descriptive-suffix",
        "list-comprehension",
        "unary-operation",
        "lambda-expression",
        "very-long-expression",
        "long-expression-60-plus",
        "medium-length-expression",
        "multipart-name",
    ],
)
def test_calculate_semantic_value_at_least(var_name: str, rhs_source: str, minimum: int) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert calculate_semantic_value(var_name, rhs_source, rhs_node, has_type_annotation=False) >= minimum


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "expected"),
    [
        ("result", "obj[x][y]", 20),  # 2 chains (+20).
        ("my_value", "func()[x][y]", 40),  # 3+ chains (+30) + 2-part name (+10).
        # Name moderately longer than the RHS (ratio between 1.1x and
        # 1.3x) scores +5, distinct from the +15 given to a name that's
        # significantly (>1.3x) longer.
        ("another", '"test"', 5),
    ],
    ids=["two-subscript-chains", "three-plus-chains-with-multipart-name", "name-moderately-longer-than-rhs"],
)
def test_calculate_semantic_value_exact(var_name: str, rhs_source: str, expected: int) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert calculate_semantic_value(var_name, rhs_source, rhs_node, has_type_annotation=False) == expected


def test_calculate_semantic_value_chained_attributes() -> None:
    rhs_source = "obj.foo.bar"
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert calculate_semantic_value("result", rhs_source, rhs_node, has_type_annotation=False) >= 20


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "minimum"),
    [
        # Rule 10 intercepts variables used solely inside comprehensions
        # before they reach calculate_semantic_value, so these need a
        # direct test: multi-part name (+30) + "some" in
        # test_semantic_words (+25) + list bonus (+25).
        ("some_european_airports", '["AES", "BYJ", "BTS"]', 25),
        ("my_mapping", '{"key": "value"}', 25),
        # multi-part name (+30) + no test_semantic_words match (+0) +
        # range bonus (+25).
        ("days_with_routes_in_a_row", "range(70)", 25),
        # Covers the False branch of the test_semantic_words check: no
        # semantic test words present.
        ("flight_count", "42", 0),
    ],
    ids=[
        "test-context-list-literal",
        "test-context-dict-literal",
        "test-context-range-call",
        "test-context-no-semantic-word",
    ],
)
def test_calculate_semantic_value_test_context(var_name: str, rhs_source: str, minimum: int) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert (
        calculate_semantic_value(var_name, rhs_source, rhs_node, has_type_annotation=False, is_test_context=True)
        >= minimum
    )


# ---------------------------------------------------------------------------
# _adds_verbosity_or_context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "expected"),
    [
        ("raw_data", "fetch_data()", True),  # Descriptive prefix.
        ("raw_headers", 'kwargs.get("headers")', True),  # Var contains RHS key, more verbose.
        ("user_email", 'data.get("email")', True),  # .get() with more context.
        ("translations", "orjson.loads(data)", True),  # Generic parse func, descriptive name.
        ("user_config", "json.load(f)", True),  # Generic parse func, multi-part name.
        ("data", "json.loads(data)", False),  # Parse func but generic variable name.
        ("configuration", "loads(data)", True),  # Parse function as a bare Name node.
        ("x", "42", False),  # No verbosity added.
        # Branch coverage: Subscript RHS with a variable (non-constant)
        # slice — rhs_key_or_method stays None.
        ("user_obj", "obj[key]", False),
        # Branch coverage: Call RHS where func is a Subscript, not
        # Name/Attribute.
        ("configuration", 'funcs["load"](data)', False),
        # Branch coverage: Pattern 3 (.get() call) where the key is not in
        # the var name.
        ("x", 'data.get("email")', False),
        # Branch coverage: Pattern 4 parse func where func is a Subscript.
        ("parsed_data", 'parsers["json"](data)', True),
        # Branch coverage: Pattern 4 parse func but var name is generic
        # (in generic_names).
        ("result", "json.loads(data)", False),
    ],
    ids=[
        "descriptive-prefix",
        "contains-rhs-key-more-verbose",
        "get-call-with-context",
        "generic-parse-descriptive-name",
        "generic-parse-multipart-name",
        "generic-parse-generic-name",
        "parse-function-as-name",
        "no-verbosity",
        "subscript-with-variable-slice",
        "call-with-subscript-func",
        "get-call-key-not-in-var",
        "parse-func-with-subscript-func",
        "parse-func-with-generic-var-name",
    ],
)
def test_adds_verbosity_or_context(var_name: str, rhs_source: str, *, expected: bool) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert _adds_verbosity_or_context(var_name, rhs_source, rhs_node) is expected


# ---------------------------------------------------------------------------
# _would_require_parentheses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rhs_source", "expected"),
    [
        ("len(x) + 1", True),
        ("a and b", True),
        ("x == y", True),
        ("len(x)", False),
    ],
    ids=["binop", "boolop", "compare", "simple-call"],
)
def test_would_require_parentheses(rhs_source: str, *, expected: bool) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert _would_require_parentheses(rhs_node) is expected


# ---------------------------------------------------------------------------
# _is_named_constant_pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "expected"),
    [
        ("max_depth", "10", True),  # Multi-part name and number.
        ("line_spacing", "1.2", True),  # Float.
        ("threshold", "42", True),  # Single-part long name.
        ("value", "10", False),  # Single-part short generic name.
        ("num", "10", False),
        ("msg", '"hello"', False),  # Non-numeric.
    ],
    ids=["multipart-int", "float", "single-part-long-name", "generic-value", "generic-num", "non-numeric"],
)
def test_is_named_constant_pattern(var_name: str, rhs_source: str, *, expected: bool) -> None:
    node = ast.parse(rhs_source, mode="eval").body
    assert _is_named_constant_pattern(var_name, node) is expected


# ---------------------------------------------------------------------------
# _is_named_string_constant_pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "expected"),
    [
        ("_GREY", '"rgb(201, 203, 207)"', True),  # Private SCREAMING_SNAKE_CASE.
        ("MAX_RETRIES", '"3"', True),  # SCREAMING_SNAKE_CASE, no leading underscore.
        ("_temp", '"foo"', False),  # Private but not all-uppercase.
        ("_GREY", "10", False),  # Non-string constant RHS.
        ("_GREY", "compute()", False),  # Non-constant RHS.
        ("_", '"foo"', False),  # Nothing left after stripping underscores.
    ],
    ids=[
        "private-screaming-snake-case",
        "screaming-snake-case",
        "private-lowercase",
        "non-string-constant",
        "non-constant",
        "bare-underscore",
    ],
)
def test_is_named_string_constant_pattern(var_name: str, rhs_source: str, *, expected: bool) -> None:
    node = ast.parse(rhs_source, mode="eval").body
    assert _is_named_string_constant_pattern(var_name, node) is expected


# ---------------------------------------------------------------------------
# _is_test_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (Path("tests/test_something.py"), True),
        (Path("tests/utils/test_helpers.py"), True),
        (Path("test/test_foo.py"), True),
        (Path("test_example.py"), True),
        (Path("src/test_module.py"), True),
        (Path("example_test.py"), True),
        (Path("src/module_test.py"), True),
        (Path("src/module.py"), False),
        (Path("main.py"), False),
        (Path("setup.py"), False),
        (None, False),
    ],
    ids=[
        "tests-directory",
        "nested-tests-directory",
        "singular-test-directory",
        "test-prefix",
        "test-prefix-nested",
        "test-suffix",
        "test-suffix-nested",
        "plain-module",
        "main",
        "setup",
        "none",
    ],
)
def test_is_test_file(path: Path | None, *, expected: bool) -> None:
    assert _is_test_file(path) is expected


# ---------------------------------------------------------------------------
# _contains_nondeterministic_call
# ---------------------------------------------------------------------------


def test_contains_nondeterministic_call_with_subscript_func() -> None:
    # Branch coverage: when the called function is accessed via subscript
    # (e.g. ``funcs[0]()``), ``node.func`` is neither Name nor Attribute.
    # The detector must continue visiting child nodes rather than crashing
    # or silently skipping.
    rhs_node = ast.parse("funcs[0]()", mode="eval").body
    assert _contains_nondeterministic_call(rhs_node) is False
