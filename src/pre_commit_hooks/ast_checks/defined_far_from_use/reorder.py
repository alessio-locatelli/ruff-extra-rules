from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

from pre_commit_hooks.ast_checks._scope import iter_binding_names

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

_EXACT_ARITHMETIC_OPERATORS = (ast.Add, ast.Sub, ast.Mult)
_EXACT_UNARY_OPERATORS = (ast.UAdd, ast.USub)
_EMPTY_CONSTRUCTORS = frozenset({"dict", "frozenset", "list", "set", "tuple"})
_VALIDATION_WORDS = frozenset({"assert", "check", "coerce", "ensure", "require", "validate", "verify"})
_PURE_CALLABLES = frozenset(
    {
        "abs",
        "bool",
        "callable",
        "chr",
        "divmod",
        "float",
        "format",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "len",
        "ord",
        "range",
        "repr",
        "reversed",
        "round",
        "str",
        "type",
    }
)
_READ_ONLY_METHODS = frozenset(
    {
        "as_posix",
        "as_uri",
        "casefold",
        "count",
        "decode",
        "encode",
        "endswith",
        "exists",
        "find",
        "format",
        "get",
        "index",
        "is_dir",
        "is_file",
        "isdisjoint",
        "issubset",
        "issuperset",
        "items",
        "join",
        "joinpath",
        "keys",
        "lower",
        "lstrip",
        "relative_to",
        "replace",
        "rfind",
        "rsplit",
        "rstrip",
        "split",
        "splitlines",
        "startswith",
        "strip",
        "upper",
        "values",
        "with_name",
        "with_suffix",
    }
)


class DefinedFarFromUseLevel(Enum):
    CONSERVATIVE = auto()
    AGGRESSIVE = auto()


@dataclass(frozen=True, slots=True)
class StatementEffects:
    rebound: frozenset[str]
    exposed: frozenset[str]
    read: frozenset[str]
    calls: bool


def statement_effects(statement: ast.stmt, builtin_names: frozenset[str]) -> StatementEffects:
    nodes = list(ast.walk(statement))
    rebound = frozenset(name for node in nodes for name in iter_binding_names(node))
    mutated = frozenset(name for node in nodes for name in _mutation_roots(node, builtin_names))
    read = frozenset(node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load))
    return StatementEffects(rebound, rebound | mutated, read, any(isinstance(node, ast.Call) for node in nodes))


def may_reorder(
    value: ast.expr,
    window: Sequence[StatementEffects],
    level: DefinedFarFromUseLevel,
    *,
    stable_locals: frozenset[str],
    builtin_names: frozenset[str],
    rebindable_by_calls: frozenset[str],
) -> bool:
    if level is DefinedFarFromUseLevel.CONSERVATIVE:
        rebound = frozenset().union(*(effects.rebound for effects in window))
        return _is_order_independent(value, stable_locals - rebound, builtin_names)
    exposed = frozenset().union(*(effects.exposed for effects in window))
    if any(effects.calls for effects in window):
        exposed |= rebindable_by_calls
    value_nodes = list(ast.walk(value))
    mutated_by_value = frozenset(name for node in value_nodes for name in _mutation_roots(node, builtin_names))
    if any(isinstance(node, ast.Call) for node in value_nodes):
        mutated_by_value |= rebindable_by_calls
    read_by_window = frozenset().union(*(effects.read for effects in window))
    return (
        not _calls_validation(value)
        and not (_state_read_by(value) & exposed)
        and not (mutated_by_value & read_by_window)
    )


def _mutation_roots(node: ast.AST, builtin_names: frozenset[str]) -> Iterator[str]:
    if isinstance(node, ast.Attribute | ast.Subscript) and isinstance(node.ctx, ast.Store | ast.Del):
        yield from _root_names((node.value,))
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr not in _READ_ONLY_METHODS:
            yield from _root_names((node.func.value,))
        if not (isinstance(node.func, ast.Name) and node.func.id in _PURE_CALLABLES & builtin_names):
            arguments = (*node.args, *(keyword.value for keyword in node.keywords))
            yield from _root_names(
                argument.value if isinstance(argument, ast.Starred) else argument for argument in arguments
            )


def _root_names(expressions: Iterable[ast.expr]) -> Iterator[str]:
    for expression in expressions:
        node = expression
        while isinstance(node, ast.Attribute | ast.Subscript):
            node = node.value
        if isinstance(node, ast.Name):
            yield node.id


def _state_read_by(value: ast.expr) -> frozenset[str]:
    return frozenset(
        node.id for node in ast.walk(value) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )


def _calls_validation(value: ast.expr) -> bool:
    return any(
        isinstance(node, ast.Call) and not _VALIDATION_WORDS.isdisjoint(_callee_name(node.func).lower().split("_"))
        for node in ast.walk(value)
    )


def _callee_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_order_independent(node: ast.expr, stable_locals: frozenset[str], builtin_names: frozenset[str]) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id in stable_locals
    if isinstance(node, ast.Tuple | ast.List):
        return all(_is_order_independent(element, stable_locals, builtin_names) for element in node.elts)
    if isinstance(node, ast.Set):
        return all(_is_literal_key(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_literal_key(key) and _is_order_independent(item, stable_locals, builtin_names)
            for key, item in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id in builtin_names & _EMPTY_CONSTRUCTORS
            and not node.args
            and not node.keywords
        )
    return _is_exact_arithmetic(node)


def _is_literal_key(node: ast.expr) -> bool:
    if isinstance(node, ast.Tuple):
        return all(_is_literal_key(element) for element in node.elts)
    return isinstance(node, ast.Constant) or _is_exact_arithmetic(node)


def _is_exact_arithmetic(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) in {int, float}
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, _EXACT_UNARY_OPERATORS) and _is_exact_arithmetic(node.operand)
    if isinstance(node, ast.BinOp):
        return (
            isinstance(node.op, _EXACT_ARITHMETIC_OPERATORS)
            and _is_exact_arithmetic(node.left)
            and _is_exact_arithmetic(node.right)
        )
    return False
