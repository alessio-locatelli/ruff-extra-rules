"""Tests for the shared _scope traversal utility."""

from __future__ import annotations

import ast

from pre_commit_hooks.ast_checks._scope import collect_scope_names, iter_within_scope


def test_iter_within_scope_yields_direct_children() -> None:
    tree = ast.parse("x = 1\ny = 2\n")

    yielded = list(iter_within_scope(tree))

    assert tree.body[0] in yielded
    assert tree.body[1] in yielded


def test_iter_within_scope_descends_into_non_scope_nodes() -> None:
    tree = ast.parse("if True:\n    x = 1\n")

    names = [n.id for n in iter_within_scope(tree) if isinstance(n, ast.Name)]

    assert names == ["x"]


def test_iter_within_scope_yields_but_does_not_descend_into_function() -> None:
    tree = ast.parse("def outer():\n    inner_var = 1\n")

    func_defs = [n for n in iter_within_scope(tree) if isinstance(n, ast.FunctionDef)]
    names = [n.id for n in iter_within_scope(tree) if isinstance(n, ast.Name)]

    assert len(func_defs) == 1
    assert names == []


def test_iter_within_scope_does_not_descend_into_class() -> None:
    tree = ast.parse("class Foo:\n    attr = 1\n    def method(self):\n        pass\n")

    class_defs = [n for n in iter_within_scope(tree) if isinstance(n, ast.ClassDef)]
    names = [n.id for n in iter_within_scope(tree) if isinstance(n, ast.Name)]

    assert len(class_defs) == 1
    assert names == []


def test_iter_within_scope_does_not_descend_into_lambda() -> None:
    tree = ast.parse("callback = lambda item: item + 1\n")

    names = [n.id for n in iter_within_scope(tree) if isinstance(n, ast.Name)]

    assert names == ["callback"]


def test_iter_within_scope_does_not_descend_into_comprehension() -> None:
    tree = ast.parse("squares = [value * value for value in range(10)]\n")

    names = [n.id for n in iter_within_scope(tree) if isinstance(n, ast.Name)]

    assert names == ["squares"]


def test_iter_within_scope_starts_inside_a_function_scope() -> None:
    tree = ast.parse(
        "def outer():\n    local_var = 1\n    return local_var\n\nmodule_var = 2\n"
    )
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)

    names = {n.id for n in iter_within_scope(func_node) if isinstance(n, ast.Name)}

    assert names == {"local_var"}


def test_collect_scope_names_module_level_excludes_function_locals() -> None:
    tree = ast.parse("module_var = 1\n\ndef foo():\n    local_var = 2\n")

    assert collect_scope_names(tree) == {"module_var"}


def test_collect_scope_names_function_level_excludes_nested_function() -> None:
    tree = ast.parse(
        "def outer():\n"
        "    local_var = 1\n"
        "    def inner():\n"
        "        inner_var = 2\n"
        "    return local_var\n"
    )
    outer = tree.body[0]
    assert isinstance(outer, ast.FunctionDef)

    assert collect_scope_names(outer) == {"local_var"}


def test_collect_scope_names_excludes_class_body_and_methods() -> None:
    tree = ast.parse(
        "def outer():\n"
        "    class Nested:\n"
        "        attr = 1\n"
        "        def method(self):\n"
        "            method_var = 2\n"
        "    return Nested\n"
    )
    outer = tree.body[0]
    assert isinstance(outer, ast.FunctionDef)

    assert collect_scope_names(outer) == {"Nested"}


def test_collect_scope_names_excludes_comprehension_loop_variable() -> None:
    tree = ast.parse(
        "def outer():\n    values = [1, 2, 3]\n    return [item for item in values]\n"
    )
    outer = tree.body[0]
    assert isinstance(outer, ast.FunctionDef)

    names = collect_scope_names(outer)

    assert "values" in names
    assert "item" not in names
