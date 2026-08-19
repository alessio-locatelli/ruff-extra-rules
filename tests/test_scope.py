from __future__ import annotations

import ast

import pytest

from pre_commit_hooks.ast_checks._scope import (
    class_scope_binding_names,
    class_scope_global_or_nonlocal_names,
    collect_scope_names,
    iter_within_scope,
)


def test_iter_within_scope_yields_direct_children() -> None:
    tree = ast.parse("x = 1\ny = 2\n")

    yielded = list(iter_within_scope(tree))

    assert tree.body[0] in yielded
    assert tree.body[1] in yielded


@pytest.mark.parametrize(
    ("source", "node_type", "names"),
    [
        ("if True:\n    x = 1\n", None, ["x"]),
        ("def outer():\n    inner_var = 1\n", ast.FunctionDef, []),
        ("class Foo:\n    attr = 1\n    def method(self):\n        pass\n", ast.ClassDef, []),
        ("callback = lambda item: item + 1\n", None, ["callback"]),
        ("squares = [value * value for value in range(10)]\n", None, ["squares"]),
    ],
    ids=[
        "descends-into-if",
        "yields-but-not-into-function",
        "yields-but-not-into-class",
        "not-into-lambda",
        "not-into-comprehension",
    ],
)
def test_iter_within_scope_descent_rules(source: str, node_type: type[ast.AST] | None, names: list[str]) -> None:
    nodes = list(iter_within_scope(ast.parse(source)))

    assert [n.id for n in nodes if isinstance(n, ast.Name)] == names
    if node_type is not None:
        assert len([n for n in nodes if isinstance(n, node_type)]) == 1


def test_iter_within_scope_starts_inside_a_function_scope() -> None:
    tree = ast.parse("def outer():\n    local_var = 1\n    return local_var\n\nmodule_var = 2\n")
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)

    names = {n.id for n in iter_within_scope(func_node) if isinstance(n, ast.Name)}

    assert names == {"local_var"}


@pytest.mark.parametrize(
    ("source", "use_module_root", "names"),
    [
        ("module_var = 1\n\ndef foo():\n    local_var = 2\n", True, {"module_var"}),
        (
            "def outer():\n    local_var = 1\n    def inner():\n        inner_var = 2\n    return local_var\n",
            False,
            {"local_var"},
        ),
        (
            (
                "def outer():\n"
                "    class Nested:\n"
                "        attr = 1\n"
                "        def method(self):\n"
                "            method_var = 2\n"
                "    return Nested\n"
            ),
            False,
            {"Nested"},
        ),
        (
            "def outer():\n    values = [1, 2, 3]\n    return [item for item in values]\n",
            False,
            {"values"},
        ),
        # PEP 572: a `:=` target inside a comprehension binds to the scope
        # enclosing the comprehension, not the comprehension's own scope —
        # unlike the comprehension's `for`-loop variable, which stays hidden.
        (
            "def outer():\n    return [y for x in range(3) if (found := x) and found.bit_length()]\n",
            False,
            {"found"},
        ),
        (
            "def outer():\n    return [[z for z in range(3) if (deep := z)] for y in range(3)]\n",
            False,
            {"deep"},
        ),
        # A walrus inside a lambda binds to the lambda's own scope, even if
        # the lambda sits inside a comprehension — it must not bubble up
        # further, unlike a walrus directly inside the comprehension.
        (
            "def outer():\n    return [(lambda: (local := 1))() for item in range(3)]\n",
            False,
            set(),
        ),
    ],
    ids=[
        "module-level-excludes-function-locals",
        "function-level-excludes-nested-function",
        "excludes-class-body-and-methods",
        "excludes-comprehension-loop-variable",
        "walrus-in-comprehension-bubbles-up",
        "walrus-in-nested-comprehension-bubbles-up",
        "walrus-in-lambda-stays-in-lambda-scope",
    ],
)
def test_collect_scope_names(source: str, names: set[str], *, use_module_root: bool) -> None:
    tree = ast.parse(source)
    root: ast.AST = tree
    if not use_module_root:
        root = tree.body[0]
        assert isinstance(root, ast.FunctionDef)

    assert collect_scope_names(root) == names


def test_class_scope_binding_names_excludes_nested_scopes_and_comprehensions() -> None:
    node = ast.parse(
        "class Container:\n"
        "    value = 1\n"
        "    def method():\n"
        "        pass\n"
        "    def defaulted(value=(default_value := 1)):\n"
        "        pass\n"
        "    def required_keyword(*, required):\n"
        "        pass\n"
        "    @(method_decorator := decorator)\n"
        "    def decorated():\n"
        "        pass\n"
        "    def annotated(value: annotation_value) -> return_annotation:\n"
        "        pass\n"
        "    def generic[T: type_param_bound]():\n"
        "        pass\n"
        "    async def async_method():\n"
        "        pass\n"
        "    class Nested:\n"
        "        pass\n"
        "    @(class_decorator := decorator)\n"
        "    class Decorated:\n"
        "        pass\n"
        "    class NestedWithHeader((nested_header := object)):\n"
        "        pass\n"
        "    class Keyworded(key=(class_keyword := value)):\n"
        "        pass\n"
        "    class Generic[T: type_bound]:\n"
        "        pass\n"
        "    thunk = lambda value=(lambda_default := 1): (lambda_local := 1)\n"
        "    required_thunk = lambda *, required: None\n"
        "    listed = [item for item in ()]\n"
        "    setted = {item for item in ()}\n"
        "    mapped = {item: item for item in ()}\n"
        "    generated = (item for item in ())\n"
        "    import package.module as imported\n"
        "    from package import member as imported_member\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"
        "        pass\n"
        "    except TypeError as caught:\n"
        "        pass\n"
        "    match value:\n"
        "        case _:\n"
        "            pass\n"
        "    match value:\n"
        "        case captured:\n"
        "            pass\n"
        "    match value:\n"
        "        case [*_]:\n"
        "            pass\n"
        "    match value:\n"
        "        case [*rest]:\n"
        "            pass\n"
        "    match value:\n"
        "        case {'item': _}:\n"
        "            pass\n"
        "    match value:\n"
        "        case {**remaining}:\n"
        "            pass\n"
    ).body[0]
    assert isinstance(node, ast.ClassDef)

    assert class_scope_binding_names(node) == {
        "value",
        "method",
        "defaulted",
        "default_value",
        "required_keyword",
        "decorated",
        "method_decorator",
        "annotated",
        "generic",
        "async_method",
        "Nested",
        "Decorated",
        "class_decorator",
        "NestedWithHeader",
        "nested_header",
        "Keyworded",
        "class_keyword",
        "Generic",
        "thunk",
        "lambda_default",
        "required_thunk",
        "listed",
        "setted",
        "mapped",
        "generated",
        "imported",
        "imported_member",
        "caught",
        "captured",
        "rest",
        "remaining",
    }


def test_class_scope_global_or_nonlocal_names_excludes_nested_scopes() -> None:
    node = ast.parse(
        "class Container:\n"
        "    global module_name\n"
        "    nonlocal enclosing_name\n"
        "    def method():\n"
        "        global method_name\n"
        "    async def async_method():\n"
        "        nonlocal async_name\n"
        "    class Nested:\n"
        "        global nested_name\n"
    ).body[0]
    assert isinstance(node, ast.ClassDef)

    assert class_scope_global_or_nonlocal_names(node) == {"module_name", "enclosing_name"}
