from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks.redundant_type_conversion.candidates import find_candidates
from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import ALL_CONSTRUCTORS

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize("constructor", sorted(ALL_CONSTRUCTORS))
def test_finds_every_constructor_as_a_call_argument(constructor: str) -> None:
    source = f"func({constructor}(x))\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.constructor == constructor


@pytest.mark.parametrize(
    "source",
    [
        "y = str(x)\n",  # assignment RHS
        "return str(x)\n",  # return statement (module-level is invalid, wrapped below)
        "func(str(x))\n",  # call argument
        "[str(x)]\n",  # list element
        "func(key=str(x))\n",  # keyword argument value
        "f'{str(x)}'\n",  # f-string interpolation
        "y: str = str(x)\n",  # annotated assignment RHS
    ],
    ids=["assign-rhs", "return", "call-arg", "list-element", "keyword-value", "fstring", "annotated-assign"],
)
def test_finds_a_candidate_in_any_expression_position(source: str) -> None:
    if source.startswith("return"):
        source = f"def f():\n    {source}"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.constructor == "str"


@pytest.mark.parametrize(
    ("source", "eligible"),
    [
        ("int(x, base=2)\n", ALL_CONSTRUCTORS),
        ("dict(x, y)\n", ALL_CONSTRUCTORS),
        ("frozenset()\n", ALL_CONSTRUCTORS),
        ("list(*x)\n", ALL_CONSTRUCTORS),
        ("list(\n    x\n)\n", ALL_CONSTRUCTORS),
        ("list(x)\n", frozenset({"str"})),
        ("not_a_builtin(x)\n", ALL_CONSTRUCTORS),
        # `module.str(x)` isn't a call to the builtin -- `node.func` is an
        # Attribute, not a bare Name.
        ("module.str(x)\n", ALL_CONSTRUCTORS),
    ],
    ids=[
        "keyword-arguments",
        "more-than-one-positional-argument",
        "zero-arguments",
        "starred-single-argument",
        "parens-span-multiple-lines",
        "constructor-not-in-eligible-set",
        "unrelated-function",
        "call-through-an-attribute",
    ],
)
def test_ignores_a_non_candidate_call(source: str, eligible: frozenset[str]) -> None:
    assert find_candidates(ast.parse(source), eligible) == []


@pytest.mark.parametrize(
    "null_out",
    [
        lambda call: setattr(call, "end_lineno", None),
        lambda call: setattr(call.args[0], "end_lineno", None),
    ],
    ids=["call-missing-end-position", "argument-missing-end-position"],
)
def test_ignores_a_call_missing_its_own_end_position(null_out: Callable[[ast.Call], None]) -> None:
    # Mirrors _base.py's fast_get_source_segment: a real ast.parse() call
    # always has end position info, but a synthetically constructed/edited
    # node might not -- this guards find_candidates() against indexing a
    # missing end_lineno/end_col_offset rather than assuming it's always set.
    tree = ast.parse("list(x)\n")
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
    null_out(call)

    assert find_candidates(tree, ALL_CONSTRUCTORS) == []


@pytest.mark.parametrize(
    "shadowing_statement",
    [
        "def str(v):\n    return v\n\n\n",
        "class str:\n    pass\n\n\n",
        "import re as str\n\n\n",
        "from os import path as str\n\n\n",
        # Deliberately scope-blind (see _shadowed_names): a binding inside
        # an unrelated function elsewhere in the module still disables
        # every same-named candidate module-wide.
        "def other():\n    str = 5\n\n\n",
        "for str in range(3):\n    pass\n\n\n",
        "with open('f') as str:\n    pass\n\n\n",
        "if (str := 5):\n    pass\n\n\n",
        "def other(str):\n    return str\n\n\n",
        "other = lambda str: str\n\n\n",
        "try:\n    pass\nexcept ValueError as str:\n    pass\n\n\n",
        "match 1:\n    case str:\n        pass\n\n\n",
        "match [1]:\n    case [*str]:\n        pass\n\n\n",
        "match {}:\n    case {**str}:\n        pass\n\n\n",
    ],
    ids=[
        "function-def",
        "class-def",
        "import-as",
        "import-from-as",
        "assignment-in-an-unrelated-scope",
        "for-loop-target",
        "with-as-target",
        "walrus-target",
        "function-parameter",
        "lambda-parameter",
        "except-as-handler",
        "match-case-capture",
        "match-case-star-capture",
        "match-case-mapping-rest-capture",
    ],
)
def test_ignores_a_call_whose_constructor_name_is_shadowed(shadowing_statement: str) -> None:
    # Regression: a bare `str(x)` call was previously always treated as
    # the builtin constructor, even when `str` itself was rebound
    # somewhere in the module -- removing the "conversion" in that case
    # calls a *different*, user-defined callable, which can change
    # behavior rather than being a safe no-op.
    source = f"{shadowing_statement}func(str(x))\n"
    assert find_candidates(ast.parse(source), ALL_CONSTRUCTORS) == []


def test_shadowing_one_constructor_does_not_affect_an_unrelated_one() -> None:
    source = "list = []\n\n\nfunc(str(x))\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.constructor == "str"


@pytest.mark.parametrize(
    "source",
    [
        "from some_module import *\nfunc(str(x))\n",
        "func(str(x))\nfrom some_module import *\n",
        "def f():\n    from some_module import *\n\n\nfunc(str(x))\n",
    ],
    ids=["before-the-candidate", "after-the-candidate", "nested-in-an-unrelated-function"],
)
def test_ignores_every_candidate_when_a_wildcard_import_is_present(source: str) -> None:
    # Regression: `from module import *` can bind any name at all,
    # including a builtin constructor's own name (e.g. a compatibility
    # shim exporting its own `str`), without _shadowed_names() ever seeing
    # that specific binding -- a wildcard import records only the literal
    # string "*", not the names it actually introduces. Treating the whole
    # module as unsafe once any wildcard import is present, regardless of
    # where it sits relative to a candidate, is the same conservative
    # trade-off _shadowed_names() itself makes for every other shadowing
    # shape.
    assert find_candidates(ast.parse(source), ALL_CONSTRUCTORS) == []


def test_a_non_wildcard_import_from_does_not_disable_every_candidate() -> None:
    source = "from some_module import something\nfunc(str(x))\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.constructor == "str"


def test_candidate_positions_match_the_real_source_bytes() -> None:
    source = "takes_list(list(bar))\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)

    line_bytes = source.encode("utf-8")
    assert line_bytes[candidate.call_start_col : candidate.call_end_col] == b"list(bar)"
    assert line_bytes[candidate.arg_start_col : candidate.arg_end_col] == b"bar"
    assert candidate.line == 1


def test_finds_multiple_independent_candidates_on_different_lines() -> None:
    source = "a = str(x)\nb = int(y)\n"
    candidates = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert [(c.constructor, c.line) for c in candidates] == [("str", 1), ("int", 2)]
