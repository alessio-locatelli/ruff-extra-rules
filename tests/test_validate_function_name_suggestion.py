"""Tests for validate_function_name.suggestion (naming heuristics)."""

from __future__ import annotations

import ast

from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
    analyze_function,
)
from pre_commit_hooks.ast_checks.validate_function_name.suggestion import (
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def test_derive_entity_from_name_without_get_prefix_returns_name_unchanged() -> None:
    assert derive_entity_from_name("process_data") == "process_data"


def test_extract_first_verb_empty_string_returns_none() -> None:
    assert extract_first_verb("") is None


def test_extract_first_verb_whitespace_only_returns_none() -> None:
    """A docstring line that's non-empty but has no actual words."""
    assert extract_first_verb("   ") is None


def test_extract_first_verb_article_alone_returns_none() -> None:
    """An article with nothing following it has no verb to extract."""
    assert extract_first_verb("The") is None


def test_suggest_name_for_test_prefixed_function_is_untouched() -> None:
    """suggest_name_for's own defensive check for test_-prefixed names,
    exercised directly since collect_suggestions only ever calls it with
    get_-prefixed names (test_ and get_ prefixes are mutually exclusive).
    """
    func_node = _func("def test_something():\n    pass\n", "test_something")
    analysis = analyze_function(func_node)
    suggested, reason = suggest_name_for(func_node, analysis)
    assert suggested == "test_something"
    assert reason == "function looks like a test"


def test_suggest_name_for_decorated_function_is_untouched() -> None:
    """suggest_name_for's own defensive check for override/abstractmethod,
    exercised directly since collect_suggestions already filters these out
    before ever calling suggest_name_for.
    """
    func_node = _func("@override\ndef get_data():\n    pass\n", "get_data")
    analysis = analyze_function(func_node)
    suggested, reason = suggest_name_for(func_node, analysis)
    assert suggested == "get_data"
    assert reason == "skip: decorated with @override or @abstractmethod"


def test_suggest_name_for_collects_and_parses_prefers_parse() -> None:
    func_node = _func(
        "def get_data(text):\n"
        "    items = []\n"
        "    items.append(json.loads(text))\n"
        "    return items\n",
        "get_data",
    )
    analysis = analyze_function(func_node)
    assert analysis["collects"] is True
    assert analysis["parses"] is True

    suggested, reason = suggest_name_for(func_node, analysis)
    assert suggested == "parse_data"
    assert reason == "parses/collects structured data from a source"


def test_suggest_name_for_outputs_only_suggests_print() -> None:
    func_node = _func("def get_status(x):\n    print(x)\n    return x\n", "get_status")
    analysis = analyze_function(func_node)
    suggested, reason = suggest_name_for(func_node, analysis)
    assert suggested == "print_status"
    assert reason == "outputs data to stdout/log"


def test_suggest_name_for_validates_only_suggests_validate() -> None:
    func_node = _func("def get_valid(form):\n    return form.is_valid()\n", "get_valid")
    analysis = analyze_function(func_node)
    suggested, reason = suggest_name_for(func_node, analysis)
    assert suggested == "validate_valid"
    assert reason == "performs validation and returns errors"


def test_suggest_name_for_transforms_only_suggests_transform() -> None:
    func_node = _func(
        "def get_data(items):\n    return items.transform()\n", "get_data"
    )
    analysis = analyze_function(func_node)
    suggested, reason = suggest_name_for(func_node, analysis)
    assert suggested == "transform_data"
    assert reason == "performs a transformation"


def test_first_docstring_line_returns_first_stripped_line() -> None:
    func_node = _func(
        "def get_data():\n"
        '    """First line.\n'
        "    Second line.\n"
        '    """\n'
        "    return 1\n",
        "get_data",
    )
    assert first_docstring_line(func_node) == "First line."
