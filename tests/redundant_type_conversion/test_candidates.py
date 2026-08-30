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
        "y = str(x)\n",
        "return str(x)\n",
        "func(str(x))\n",
        "[str(x)]\n",
        "func(key=str(x))\n",
        "f'{str(x)}'\n",
        "y: str = str(x)\n",
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
        "import str.helpers\n\n\n",
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
        "dotted-import-binds-its-top-level-component",
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
    source = f"{shadowing_statement}func(str(x))\n"
    assert find_candidates(ast.parse(source), ALL_CONSTRUCTORS) == []


def test_an_aliased_dotted_import_shadows_only_its_alias_not_the_top_level_component() -> None:
    source = "import str.helpers as helper\n\n\nfunc(str(x))\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.constructor == "str"


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


@pytest.mark.parametrize(
    "source",
    [
        "a = list(get_items())\n",
        "a = list(get(1, 2))\n",
        "a = str(a or get_default())\n",
        "a = str(root / name)\n",
        "a = str(prefix + suffix)\n",
        "a = str(a if flag else b)\n",
        "a = int(-count)\n",
        "a = bool(not flag)\n",
        "async def f():\n    a = str(await coro)\n",
        "a = str(value := compute)\n",
        "a = str(box.a / box.b)\n",
        "a = str(items[0] + rest[1])\n",
    ],
    ids=[
        "bare-call",
        "call-with-arguments",
        "call-as-tail-of-larger-expression",
        "binary-operator",
        "binary-operator-on-scalars",
        "conditional-expression",
        "unary-minus",
        "not-operator",
        "await",
        "walrus",
        "binary-operator-ending-in-an-attribute",
        "binary-operator-ending-in-a-subscript",
    ],
)
def test_ignores_a_candidate_whose_argument_the_hover_cannot_describe(source: str) -> None:
    assert find_candidates(ast.parse(source), ALL_CONSTRUCTORS) == []


@pytest.mark.parametrize(
    "source",
    [
        "a = list(box.value)\n",
        "a = list(items[0])\n",
        "a = list(rows[start:stop])\n",
        "a = list([1, 2])\n",
        "a = dict({'k': 1})\n",
        "a = tuple((first, second))\n",
        "a = str(f'{name}')\n",
        "a = str('a' 'b')\n",
        "a = list(get_rows().values[0])\n",
    ],
    ids=[
        "attribute",
        "subscript",
        "slice",
        "list-literal",
        "dict-literal",
        "parenthesized-tuple",
        "f-string",
        "implicitly-concatenated-string",
        "attribute-chain-off-a-call",
    ],
)
def test_a_candidate_whose_argument_ends_on_its_own_last_token_is_still_found(source: str) -> None:
    assert find_candidates(ast.parse(source), ALL_CONSTRUCTORS) != []


@pytest.mark.parametrize(
    "source",
    [
        "a = tuple(x for x in y)\n",
        "a = tuple(x for x in y if x > 0)\n",
        "a = tuple(idx for idx, _ in sorted(y)[:3])\n",
        "a = tuple((x for x in y))\n",
    ],
    ids=["plain", "with-a-filter-clause", "nested-subscript-in-the-iterable", "explicitly-parenthesized"],
)
def test_ignores_a_candidate_whose_argument_is_a_generator_expression(source: str) -> None:
    assert find_candidates(ast.parse(source), ALL_CONSTRUCTORS) == []


def test_a_candidate_that_is_lens_sole_argument_is_marked_wrapped_in_len() -> None:
    (candidate,) = find_candidates(ast.parse("len(set(op_ids))\n"), ALL_CONSTRUCTORS)
    assert candidate.wrapped_in_len is True


@pytest.mark.parametrize(
    "source",
    [
        "set(op_ids)\n",
        "len(set(op_ids), 1)\n",
        "len(op_ids, base=set(x))\n",
        "other_len(set(op_ids))\n",
    ],
    ids=["no-len-wrap", "len-with-extra-args", "not-lens-positional-arg", "shadowing-lookalike-name"],
)
def test_a_candidate_is_not_marked_wrapped_in_len_otherwise(source: str) -> None:
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.wrapped_in_len is False


def test_len_shadowed_anywhere_in_the_module_disables_the_len_wrap_marker() -> None:
    source = "def other():\n    len = 5\n\n\nlen(set(op_ids))\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.wrapped_in_len is False


@pytest.mark.parametrize(
    "source",
    [
        "y = matches == str(x)\n",  # bare right-hand operand
        "y = str(x) == matches\n",  # bare left-hand operand
        "y = matches != str(x)\n",  # !=
        "y = str(x) in matches\n",  # in
        "y = str(x) not in matches\n",  # not in
        "y = matches == [str(x)]\n",  # list literal
        "y = matches == (str(x),)\n",  # tuple literal
        "y = matches == {str(x)}\n",  # set literal
        "y = matches == [str(x), other]\n",  # one of several list elements
        "y = a < matches == str(x)\n",  # chained comparison, still an Eq pair
        "y = {str(x): 1} == other\n",  # dict key
        "y = {1: str(x)} == other\n",  # dict value
        "y = {**other, str(x): 1} == thing\n",  # dict key alongside a `**` unpack (a None key)
        "y = matches == [(str(x),)]\n",  # tuple nested inside a list
        "y = matches == {'paths': [str(x)]}\n",  # list nested inside a dict value
        "y = matches == [[str(x)]]\n",  # list nested inside a list
        "y = str(x) is matches\n",  # is
        "y = str(x) is not matches\n",  # is not
    ],
    ids=[
        "eq-rhs",
        "eq-lhs",
        "ne",
        "in",
        "not-in",
        "list-literal",
        "tuple-literal",
        "set-literal",
        "one-of-several-list-elements",
        "chained",
        "dict-key",
        "dict-value",
        "dict-key-alongside-an-unpack",
        "tuple-nested-in-list",
        "list-nested-in-dict-value",
        "list-nested-in-list",
        "is",
        "is-not",
    ],
)
def test_a_candidate_used_as_an_equality_operand_is_marked(source: str) -> None:
    # See ADR-0035's Path-vs-str comparison exclusion.
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.in_equality_comparison is True


@pytest.mark.parametrize(
    "source",
    [
        "y = str(x)\n",  # no comparison at all
        "y = a < str(x)\n",  # not an Eq/NotEq/In/NotIn operator
        "y = [str(x), other]\n",  # list literal, but not itself a comparison operand
    ],
    ids=["no-comparison", "ordering-operator", "list-not-compared"],
)
def test_a_candidate_is_not_marked_as_an_equality_operand_otherwise(source: str) -> None:
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.in_equality_comparison is False


@pytest.mark.parametrize(
    "shadowing_statement",
    [
        "class Path:\n    pass\n\n\n",
        "Path = get_some_unrelated_class()\n\n\n",
        "from some_other_module import Path\n\n\n",
        "from pathlib import PosixPath as Path\n\n\n",
    ],
    ids=["class-def", "reassignment", "imported-from-elsewhere", "a-different-pathlib-class-aliased-to-the-name"],
)
def test_a_locally_shadowed_purepath_name_disables_the_equality_marker(shadowing_statement: str) -> None:
    # `ty`'s hover shows a bare class name with no module info, so a
    # locally defined/rebound "Path" is indistinguishable from
    # `pathlib.Path` by hover text alone -- deliberately scope-blind and
    # conservative, since this marker only ever *suppresses* a report,
    # never adds one.
    source = f"{shadowing_statement}y = matches == [str(x)]\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.in_equality_comparison is False


def test_shadowing_an_unrelated_name_does_not_disable_the_equality_marker() -> None:
    source = "some_other_name = 5\n\n\ny = matches == [str(x)]\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.in_equality_comparison is True


@pytest.mark.parametrize(
    "import_statement",
    ["from pathlib import Path\n\n\n", "from pathlib import Path, PurePath\n\n\n"],
    ids=["single-import", "multiple-purepath-imports"],
)
def test_importing_path_from_pathlib_itself_does_not_disable_the_equality_marker(import_statement: str) -> None:
    # An ordinary, correct `from pathlib import Path` must not
    # be treated as shadowing pathlib's own class -- that's the overwhelmingly
    # common case this whole exclusion exists for.
    source = f"{import_statement}y = matches == [str(x)]\n"
    (candidate,) = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    assert candidate.in_equality_comparison is True
