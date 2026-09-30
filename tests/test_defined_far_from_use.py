from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest

from pre_commit_hooks.ast_checks._base import CheckResult, FixOutcome
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks.defined_far_from_use import DefinedFarFromUseCheck
from pre_commit_hooks.ast_checks.defined_far_from_use.reorder import DefinedFarFromUseLevel

CONSERVATIVE = DefinedFarFromUseLevel.CONSERVATIVE
AGGRESSIVE = DefinedFarFromUseLevel.AGGRESSIVE


def _check(source: str, level: DefinedFarFromUseLevel = CONSERVATIVE, max_distance: int = 5) -> CheckResult:
    source = dedent(source)
    return DefinedFarFromUseCheck(level=level, max_distance=max_distance).check(
        Path("test.py"), ast.parse(source), source
    )


def _reported(source: str, level: DefinedFarFromUseLevel = CONSERVATIVE, max_distance: int = 5) -> list[str]:
    lines = dedent(source).splitlines()
    return [
        lines[violation.line - 1][violation.col :].split(" ")[0].rstrip(":")
        for violation in _check(source, level, max_distance)
    ]


@pytest.mark.parametrize(
    "source",
    [
        """
        def total(items):
            result = 0
            if not items:
                return 0
            for item in items:
                result += item
            return result
        """,
        """
        def f(a):
            result = a
            if ready():
                return
            use(result)
        """,
        """
        def f(a):
            result = [a, "x", (1, 2.5), {"k": a}, {1, -2, ("x", 3)}]
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = set()
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = -60 * 5 + 1.5
            if ready():
                return
            use(result)
        """,
        """
        def f(value):
            result = None
            if not value:
                raise ValueError(value)
            use(result)
        """,
        """
        def f(items):
            for item in items:
                result = None
                if item:
                    break
                use(result)
        """,
        """
        def f(items):
            for item in items:
                result = None
                if item:
                    continue
                use(result)
        """,
        """
        def f(command):
            result = 0
            match command:
                case "stop":
                    return
            use(result)
        """,
        """
        def f(path):
            result = 0
            with open(path) as handle:
                if not handle.read():
                    return
            use(result)
        """,
        """
        def f():
            result: int = 0
            try:
                ready()
            except OSError:
                return
            use(result)
        """,
        """
        async def f():
            result = 0
            if await ready():
                return
            use(result)
        """,
        """
        def f():
            result = 0
            for item in items():
                if item:
                    return
            use(result)
        """,
        """
        def f():
            seed = 1
            result = seed
            if ready():
                return
            use(result)
        """,
        """
        def f(items):
            seed = 1
            for item in items:
                use(item)
            result = seed
            if ready():
                return
            use(result)
        """,
    ],
    ids=[
        "constant",
        "parameter",
        "containers",
        "empty-builtin-constructor",
        "arithmetic",
        "raise",
        "break",
        "continue",
        "match",
        "with",
        "annotated-with-exit-in-handler",
        "async",
        "return-inside-loop",
        "local-bound-earlier",
        "local-bound-before-a-loop",
    ],
)
def test_reports_order_independent_assignment_before_an_early_exit(source: str) -> None:
    violations = _check(source)

    assert _reported(source) == ["result"]
    assert "early exit that does not use it" in violations[0].message
    assert f"'# pytriage: {violations[0].error_code}'" in violations[0].message


def test_early_exit_message_names_the_line_to_move_below() -> None:
    source = """
    def f(value):
        result = 0
        if not value:
            return
        use(result)
    """

    [violation] = _check(source)

    assert (violation.line, violation.col) == (3, 4)
    assert "move the assignment below line 5" in violation.message


@pytest.mark.parametrize(
    "source",
    [
        """
        def f():
            result = get_value()
            if condition():
                return
            use(result)
        """,
        """
        def f(item):
            result = item.value
            if condition():
                return
            use(result)
        """,
        """
        def f(mapping):
            result = mapping.get("a")
            if mapping.get("b"):
                return
            use(result)
        """,
        """
        def f(items):
            result = compute(items)
            if len(items) > 3:
                return
            use(result)
        """,
        """
        def f():
            result = GLOBAL
            if condition():
                return
            use(result)
        """,
        """
        def f():
            result = handlers[0]()
            if condition():
                return
            use(result)
        """,
    ],
    ids=[
        "call",
        "attribute",
        "read-only-method-on-shared-root",
        "pure-builtin-on-shared-root",
        "global-name",
        "computed-callee",
    ],
)
def test_aggressive_level_also_reports_expressions_that_may_have_side_effects(source: str) -> None:
    assert _reported(source, CONSERVATIVE) == []
    assert _reported(source, AGGRESSIVE) == ["result"]


@pytest.mark.parametrize(
    "source",
    [
        """
        def f(self):
            before = len(self.failures)
            self.process()
            if self.aborted:
                return
            use(before)
        """,
        """
        def f(items):
            for item in items:
                value = get_value(item)
                if condition(item):
                    continue
                use(value)
        """,
        """
        def f(raw):
            value = validate_config(raw)
            if not enabled():
                return
            use(value)
        """,
        """
        def f(state):
            old = state.current
            state.current = 1
            if done():
                return
            use(old)
        """,
        """
        def f(state):
            old = state.current
            del state.items[0]
            if done():
                return
            use(old)
        """,
        """
        def f(state):
            old = state.current
            state = other()
            if done():
                return
            use(old)
        """,
        """
        def f(items):
            first = items[0]
            items.append(1)
            if done():
                return
            use(first)
        """,
        """
        def f(items):
            first = items[0]
            record(*items)
            if done():
                return
            use(first)
        """,
        """
        def f(items):
            first = items[0]
            record(target=items)
            if done():
                return
            use(first)
        """,
        """
        def f(first, second):
            fn = first
            value = fn()
            fn = second
            if done():
                return
            use(value, fn)
        """,
        """
        def f():
            value = helper()
            helper.cache_clear()
            if done():
                return
            use(value)
        """,
        """
        async def f():
            value = await fetch()
            if done():
                return
            use(value)
        """,
        """
        def f():
            value = (cached := fetch())
            if done():
                return
            use(value, cached)
        """,
    ],
    ids=[
        "snapshot-before-a-method-call",
        "argument-shared-with-the-guard",
        "validation-call",
        "attribute-store",
        "subscript-delete",
        "rebound-root",
        "mutating-method",
        "starred-argument",
        "keyword-argument",
        "rebound-callee",
        "mutated-callee",
        "await",
        "walrus",
    ],
)
def test_aggressive_level_skips_reorders_that_may_change_behavior(source: str) -> None:
    assert _reported(source, AGGRESSIVE) == []


@pytest.mark.parametrize(
    "source",
    [
        """
        def f(value):
            result = 0
            if result > value:
                return
            use(result)
        """,
        """
        def f(value):
            result = 0
            use(result)
            if value:
                return
            use(result)
        """,
        """
        def f():
            try:
                result = 0
                if ready():
                    return
                use(result)
            finally:
                log(result)
        """,
        """
        def f(items):
            for item in items:
                result = 0
                if item:
                    continue
                use(result)
            return result
        """,
        """
        def f(items):
            result = None
            for item in items:
                use(result)
                result = item
                if item:
                    continue
                use(result)
        """,
        """
        def f():
            global result
            result = 0
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = 0

            def inner():
                nonlocal result
                result = 1

            if ready():
                return
            use(result, inner)
        """,
        """
        def f():
            result = 0
            if ready():
                return
            use(result, locals())
        """,
        """
        def f():
            def inner():
                return result

            result = 0
            if ready():
                return
            use(result, inner)
        """,
        """
        def f():
            result = 0

            def inner():
                return

            use(result, inner)
        """,
        """
        def f(items):
            result = 0
            for item in items:
                if item:
                    break
            use(result)
        """,
        """
        def f():
            result = 0
            try:
                raise ValueError
            except ValueError:
                pass
            use(result)
        """,
        """
        def f():
            result = 0
            return
            use(result)
        """,
        """
        def f(pair):
            first, second = pair
            if ready():
                return
            use(first, second)
        """,
        """
        def f(obj):
            obj.value = 0
            if ready():
                return
            use(obj.value)
        """,
        """
        def f():
            first = second = 0
            if ready():
                return
            use(first, second)
        """,
        """
        def f():
            result = 0
            if ready():
                return
        """,
        """
        result = 0
        if ready():
            raise SystemExit
        use(result)
        """,
        """
        class Settings:
            result = 0
            if ready():
                raise SystemExit
            use(result)
        """,
        """
        def f():
            result = helper()
            if ready():
                return
            use(result)
        """,
        """
        def f(a):
            result = a
            a = 1
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = a.b
            if ready():
                return
            use(result)
        """,
        """
        set = frozenset


        def f():
            result = set()
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = list(range(3))
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = 1 / 3
            if ready():
                return
            use(result)
        """,
        """
        def f(item):
            result = {item}
            if ready():
                return
            use(result)
        """,
        """
        def f(item):
            result = {item: 1}
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = {**defaults}
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = later
            if ready():
                return
            later = 1
            use(result)
        """,
        """
        def f(flag):
            if flag:
                seed = 1
            result = seed
            if ready():
                return
            use(result)
        """,
        """
        def f():
            seed: int
            result = seed
            if ready():
                return
            use(result)
        """,
        """
        def f():
            seed = 1
            del seed
            result = seed
            if ready():
                return
            use(result)
        """,
        """
        def f():
            seed = 1
            try:
                ready()
            except OSError as seed:
                log(seed)
            result = seed
            if ready():
                return
            use(result)
        """,
        """
        def f():
            result = 0
            if ready():
                return
            result = 1
            use(result)
        """,
        """
        def f():
            result = 0
            if ready():
                return
            for result in items():
                use(result)
        """,
    ],
    ids=[
        "guard-uses-the-variable",
        "used-before-the-exit",
        "read-in-finally",
        "read-after-the-loop",
        "read-in-the-next-iteration",
        "global",
        "nonlocal",
        "locals",
        "closure-before-the-assignment",
        "exit-in-a-nested-function",
        "break-in-a-nested-loop",
        "raise-in-a-guarded-try",
        "unreachable-use",
        "tuple-target",
        "attribute-target",
        "chained-target",
        "unused",
        "module-level",
        "class-level",
        "conservative-call",
        "conservative-rebound-local",
        "conservative-attribute",
        "conservative-shadowed-builtin",
        "conservative-builtin-with-arguments",
        "conservative-division",
        "conservative-set-of-names",
        "conservative-dict-with-name-keys",
        "conservative-dict-unpacking",
        "conservative-local-bound-later",
        "conservative-local-bound-conditionally",
        "conservative-local-only-annotated",
        "conservative-local-deleted",
        "conservative-local-unbound-by-except",
        "first-use-overwrites",
        "first-use-rebinds-as-loop-target",
    ],
)
def test_skips_assignments_that_cannot_be_moved_past_the_exit(source: str) -> None:
    assert _reported(source) == []


@pytest.mark.parametrize(
    ("statements", "max_distance", "expected"),
    [
        (6, 5, ["result"]),
        (5, 5, []),
        (3, 2, ["result"]),
    ],
    ids=["above-the-limit", "at-the-limit", "custom-limit"],
)
def test_reports_assignment_far_from_its_first_use(statements: int, max_distance: int, expected: list[str]) -> None:
    body = "".join(f"    step_{index}()\n" for index in range(statements))
    source = f"def f():\n    result = 0\n{body}    use(result)\n"

    assert _reported(source, max_distance=max_distance) == expected


def test_distance_counts_nested_statements_and_names_the_use_line() -> None:
    source = """
    def f(flag):
        result = 0
        if flag:
            first()
            second()
        else:
            third()
        fourth()

        def helper():
            return 1

        for item in items():
            use(result)
    """

    [violation] = _check(source)

    assert "assigned 6 unrelated statements before its first use" in violation.message
    assert "closer to line 15" in violation.message


def test_distance_ignores_statements_preparing_other_inputs_of_the_use() -> None:
    source = """
    def f():
        result = 0
        first = one()
        second = two()
        third = three()
        collected = []
        collected.append(four())
        unrelated()
        build(result, first, second, third, collected)
    """

    assert _reported(source, max_distance=1) == []
    assert _reported(source.replace("collected)", ")"), max_distance=1) == ["result"]


def test_distance_skips_a_value_overwritten_before_it_is_read() -> None:
    body = "".join(f"    step_{index}()\n" for index in range(6))
    source = f"def f():\n    result = 0\n{body}    result = 1\n    use(result)\n"

    assert _reported(source) == []


def test_distance_skips_a_variable_read_in_a_closure() -> None:
    body = "".join(f"    step_{index}()\n" for index in range(6))
    source = f"def f():\n    result = 0\n{body}    def inner():\n        return result\n    use(inner)\n"

    assert _reported(source) == []
    assert _reported(source.replace("def inner():\n        return result", "inner = lambda: result")) == []


@pytest.mark.parametrize(
    ("shadowing", "expected"),
    [
        ("def other():\n    set = frozenset\n    return set\n\n\n", ["result"]),
        ("class Other:\n    set = frozenset\n\n\n", ["result"]),
        ("set = frozenset\n\n\n", []),
        ("def other():\n    global set\n    set = frozenset\n\n\n", []),
    ],
    ids=["unrelated-function", "class-attribute", "module", "global-declaration"],
)
def test_builtin_shadowing_is_resolved_per_scope(shadowing: str, expected: list[str]) -> None:
    source = f"{shadowing}def f():\n    result = set()\n    if ready():\n        return\n    use(result)\n"

    assert _reported(source) == expected


def test_builtin_shadowed_by_an_enclosing_function_is_not_trusted() -> None:
    source = """
    def outer(set):
        def f():
            result = set()
            if ready():
                return
            use(result)

        return f
    """

    assert _reported(source) == []


def test_prefers_the_early_exit_message_when_both_triggers_apply() -> None:
    body = "".join(f"    step_{index}()\n" for index in range(6))
    source = f"def f():\n    result = 0\n{body}    if ready():\n        return\n    use(result)\n"

    [violation] = _check(source)

    assert "early exit" in violation.message


def test_reports_in_nested_functions_independently() -> None:
    source = """
    def outer():
        def inner():
            result = 0
            if ready():
                return
            use(result)

        return inner
    """

    assert _reported(source) == ["result"]


def test_suppression_comment_is_honored_and_tracked() -> None:
    source = dedent(
        """
        def f():
            result = 0  # pytriage: TR11
            if ready():
                return
            use(result)
        """
    )
    check = DefinedFarFromUseCheck()

    assert check.check(Path("test.py"), ast.parse(source), source) == []
    tracked = check.check_with_suppression_tracking(Path("test.py"), ast.parse(source), source)
    assert [(usage.error_code, usage.line) for usage in tracked.suppression_usages] == [("TR11", 3)]


def test_identity_prefilter_and_fix_contract() -> None:
    check = DefinedFarFromUseCheck()
    source = "def f():\n    result = 0\n    if ready():\n        return\n    use(result)\n"
    violations = check.check(Path("test.py"), ast.parse(source), source)

    assert (check.check_id, check.error_code, check.default_enabled) == ("defined-far-from-use", "TR11", True)
    assert check.get_prefilter_pattern() is None
    assert all(not violation.fixable for violation in violations)
    assert check.fix(Path("test.py"), violations, source, ast.parse(source)).outcomes == (FixOutcome.DECLINED,)


@pytest.mark.parametrize(
    ("argv", "expected_exit"),
    [
        ([], 0),
        (["--defined-far-from-use-level", "aggressive"], 0),
        (["--defined-far-from-use-max-distance", "1"], 0),
        (["--defined-far-from-use-level", "aggressive", "--defined-far-from-use-max-distance", "1"], 1),
    ],
    ids=["default", "aggressive", "max-distance", "both"],
)
def test_cli_options_reach_the_check(tmp_path: Path, argv: list[str], expected_exit: int) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("def f():\n    result = get()\n    first()\n    second()\n    use(result)\n")

    assert main(["--isolated", "--select", "defined-far-from-use", *argv, filepath.as_posix()]) == expected_exit
