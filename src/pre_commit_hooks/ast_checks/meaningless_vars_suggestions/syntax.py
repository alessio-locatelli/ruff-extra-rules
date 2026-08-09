from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


class ScopeWithImports(Protocol):
    @property
    def parent(self) -> ScopeWithImports | None: ...

    @property
    def bindings(self) -> Mapping[str, list[ast.AST]]: ...

    @property
    def imports(self) -> Mapping[str, tuple[str, ...]]: ...


def arguments(arguments: ast.arguments) -> tuple[ast.arg, ...]:
    variadic_arguments = tuple(argument for argument in (arguments.vararg, arguments.kwarg) if argument is not None)
    return (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, *variadic_arguments)


def position(node: ast.expr | ast.stmt) -> tuple[int, int]:
    return node.lineno, node.col_offset


def qname(node: ast.expr, scope: ScopeWithImports) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return import_qname(scope, node.id)
    if isinstance(node, ast.Attribute):
        parent = qname(node.value, scope)
        return (*parent, node.attr) if parent is not None else None
    return None


def import_qname(scope: ScopeWithImports, name: str) -> tuple[str, ...] | None:
    current: ScopeWithImports | None = scope
    while current is not None:
        if name in current.imports:
            return current.imports[name]
        if name in current.bindings:
            return None
        current = current.parent
    return None


def unwrap_nullable_annotation(annotation: ast.expr | None) -> ast.expr | None:
    if annotation is None:
        return None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        members = _union_members(annotation)
        non_none_members = [member for member in members if not _is_none_annotation(member)]
        return (
            non_none_members[0] if len(non_none_members) == 1 and len(non_none_members) != len(members) else annotation
        )
    if isinstance(annotation, ast.Subscript) and _annotation_terminal(annotation.value) == "Optional":
        members = _subscript_members(annotation.slice)
        return members[0] if len(members) == 1 else annotation
    if isinstance(annotation, ast.Subscript) and _annotation_terminal(annotation.value) == "Union":
        members = _subscript_members(annotation.slice)
        non_none_members = [member for member in members if not _is_none_annotation(member)]
        return (
            non_none_members[0] if len(non_none_members) == 1 and len(non_none_members) != len(members) else annotation
        )
    return annotation


def _union_members(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*_union_members(node.left), *_union_members(node.right)]
    return [node]


def _subscript_members(node: ast.expr) -> list[ast.expr]:
    return node.elts if isinstance(node, ast.Tuple) else [node]


def _is_none_annotation(node: ast.expr) -> bool:
    return (isinstance(node, ast.Constant) and node.value is None) or _annotation_terminal(node) in {"None", "NoneType"}


def _annotation_terminal(annotation: ast.expr | None) -> str:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        return _annotation_terminal(annotation.value)
    return ""
