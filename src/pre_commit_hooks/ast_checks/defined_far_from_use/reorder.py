from __future__ import annotations

import ast
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
        "all",
        "any",
        "bool",
        "callable",
        "chr",
        "dict",
        "divmod",
        "enumerate",
        "float",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "len",
        "list",
        "max",
        "min",
        "ord",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "type",
        "zip",
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


def may_reorder(
    value: ast.expr,
    window: Sequence[ast.stmt],
    level: DefinedFarFromUseLevel,
    *,
    stable_locals: frozenset[str],
    builtin_names: frozenset[str],
) -> bool:
    if level is DefinedFarFromUseLevel.CONSERVATIVE:
        return _is_order_independent(value, stable_locals - rebound_names(window), builtin_names)
    return not _calls_validation(value) and not (_state_read_by(value) & exposed_names(window))


def rebound_names(statements: Iterable[ast.stmt]) -> frozenset[str]:
    return frozenset(
        name for statement in statements for node in ast.walk(statement) for name in iter_binding_names(node)
    )


def exposed_names(statements: Iterable[ast.stmt]) -> frozenset[str]:
    return rebound_names(statements) | frozenset(
        name for statement in statements for node in ast.walk(statement) for name in _mutation_roots(node)
    )


def _mutation_roots(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Attribute | ast.Subscript) and isinstance(node.ctx, ast.Store | ast.Del):
        yield from _root_names((node.value,))
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr not in _READ_ONLY_METHODS:
            yield from _root_names((node.func.value,))
        if not (isinstance(node.func, ast.Name) and node.func.id in _PURE_CALLABLES):
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
    loaded = {node.id for node in ast.walk(value) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    local = {node.id for node in ast.walk(value) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    return frozenset(loaded - local)


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
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(_is_order_independent(element, stable_locals, builtin_names) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None
            and _is_order_independent(key, stable_locals, builtin_names)
            and _is_order_independent(item, stable_locals, builtin_names)
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
