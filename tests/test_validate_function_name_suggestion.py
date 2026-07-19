"""Tests for validate_function_name.suggestion (naming heuristics)."""

from __future__ import annotations

import ast

import pytest

from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
    analyze_function,
    derive_entity_from_name,
    extract_first_verb,
    first_docstring_line,
    suggest_name_for,
)


def _func(source: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(source)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


@pytest.mark.parametrize(
    ("func_name", "entity"),
    [
        ("process_data", "process_data"),
        ("get_data", "data"),
    ],
    ids=["no-get-prefix", "get-prefix-stripped"],
)
def test_derive_entity_from_name(func_name: str, entity: str) -> None:
    assert derive_entity_from_name(func_name) == entity


@pytest.mark.parametrize(
    ("docstring_line", "verb"),
    [
        ("", None),
        ("   ", None),
        ("The", None),
        ("The value is set.", "value"),
        ("Combine the parameters.", "combine"),
    ],
    ids=["empty-string", "whitespace-only", "article-alone", "article-then-word", "leading-verb"],
)
def test_extract_first_verb(docstring_line: str, verb: str | None) -> None:
    assert extract_first_verb(docstring_line) == verb


@pytest.mark.parametrize(
    ("source", "func_name", "suggested_name", "reason"),
    [
        # collect_suggestions only ever calls suggest_name_for with
        # get_-prefixed names (test_ and get_ prefixes are mutually
        # exclusive), so the test_-prefix guard is exercised directly here.
        (
            "def test_something():\n    pass\n",
            "test_something",
            "test_something",
            "function looks like a test",
        ),
        # Likewise, collect_suggestions already filters out
        # override/abstractmethod-decorated functions before calling
        # suggest_name_for, so that guard is exercised directly here too.
        (
            "@override\ndef get_data():\n    pass\n",
            "get_data",
            "get_data",
            "skip: decorated with @override or @abstractmethod",
        ),
        (
            "def get_data(text):\n    items = []\n    items.append(json.loads(text))\n    return items\n",
            "get_data",
            "parse_data",
            "parses/collects structured data from a source",
        ),
        (
            "def get_status(x):\n    print(x)\n    return x\n",
            "get_status",
            "print_status",
            "outputs data to stdout/log",
        ),
        (
            "def get_valid(form):\n    return form.is_valid()\n",
            "get_valid",
            "validate_valid",
            "performs validation and returns errors",
        ),
        (
            "def get_data(items):\n    return items.transform()\n",
            "get_data",
            "transform_data",
            "performs a transformation",
        ),
    ],
    ids=[
        "test-prefixed-untouched",
        "decorated-untouched",
        "parses-and-collects-prefers-parse",
        "outputs-only-suggests-print",
        "validates-only-suggests-validate",
        "transforms-only-suggests-transform",
    ],
)
def test_suggest_name_for(source: str, func_name: str, suggested_name: str, reason: str) -> None:
    func_node = _func(source, func_name)
    analysis = analyze_function(func_node)

    suggested, actual_reason = suggest_name_for(func_node, analysis)

    assert suggested == suggested_name
    assert actual_reason == reason


def test_first_docstring_line_returns_first_stripped_line() -> None:
    func_node = _func(
        'def get_data():\n    """First line.\n    Second line.\n    """\n    return 1\n',
        "get_data",
    )
    assert first_docstring_line(func_node) == "First line."
