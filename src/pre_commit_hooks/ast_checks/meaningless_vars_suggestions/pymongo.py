from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from .syntax import arguments, position, qname, unwrap_nullable_annotation

if TYPE_CHECKING:
    from .analysis import ScopeInfo

_DOCUMENT_METHOD_NAMES = {
    "find_one",
    "find_one_and_delete",
    "find_one_and_replace",
    "find_one_and_update",
}
_UNINFORMATIVE_FINDER_TAILS = frozenset(name.removeprefix("find_") for name in _DOCUMENT_METHOD_NAMES)


def collection_attributes(node: ast.ClassDef, scope: ScopeInfo) -> frozenset[str]:
    initializer = next(
        (
            statement
            for statement in node.body
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef) and statement.name == "__init__"
        ),
        None,
    )
    if initializer is None:
        return frozenset()

    names = _argument_kinds(initializer, scope)
    attributes: dict[str, str] = {}
    receiver_name = _receiver_name(initializer)
    for statement in initializer.body:
        _record_assignment(statement, names, attributes, receiver_name, scope)
    return frozenset(name for name, kind in attributes.items() if kind == "collection")


def call_candidate(
    node: ast.Call,
    scope: ScopeInfo,
    target: ast.Name,
) -> dict[str, set[str]] | None:
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in _DOCUMENT_METHOD_NAMES:
        return None
    if _expression_kind(node.func.value, scope, target) != "collection":
        return None
    return {"document": {"pymongo_collection", "pymongo_find_one"}}


def suppresses_producer_tail(name: str) -> bool:
    return name in _UNINFORMATIVE_FINDER_TAILS


def _expression_kind(node: ast.expr, scope: ScopeInfo, target: ast.Name) -> str | None:
    scope_node = scope.node
    names = _argument_kinds(scope_node, scope) if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef) else {}
    attributes = dict.fromkeys(scope.mongodb_collection_attributes, "collection")
    receiver_name = (
        _receiver_name(scope_node) if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef) else None
    )
    for statement in scope_node.body:
        if position(statement) >= position(target):
            break
        _record_assignment(statement, names, attributes, receiver_name, scope)
    return _value_kind(node, names, attributes, receiver_name, scope)


def _argument_kinds(node: ast.FunctionDef | ast.AsyncFunctionDef, scope: ScopeInfo) -> dict[str, str]:
    return {
        argument.arg: "client" for argument in arguments(node.args) if _is_client_annotation(argument.annotation, scope)
    }


def _record_assignment(
    statement: ast.stmt,
    names: dict[str, str],
    attributes: dict[str, str],
    receiver_name: str | None,
    scope: ScopeInfo,
) -> None:
    if isinstance(statement, ast.Assign):
        targets = statement.targets
        value = statement.value
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        targets = [statement.target]
        value = statement.value
    else:
        return

    kind = _value_kind(value, names, attributes, receiver_name, scope)
    for target in targets:
        if kind is None:
            _clear_names(target, names)
            attribute_name = _receiver_attribute_name(target, receiver_name)
            if attribute_name is not None:
                attributes.pop(attribute_name, None)
            continue
        if isinstance(target, ast.Name):
            names[target.id] = kind
        else:
            attribute_name = _receiver_attribute_name(target, receiver_name)
            if attribute_name is None:
                continue
            attributes[attribute_name] = kind


def _clear_names(target: ast.expr, names: dict[str, str]) -> None:
    if isinstance(target, ast.Name):
        names.pop(target.id, None)
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            _clear_names(element, names)
    elif isinstance(target, ast.Starred):
        _clear_names(target.value, names)


def _value_kind(
    node: ast.expr,
    names: dict[str, str],
    attributes: dict[str, str],
    receiver_name: str | None,
    scope: ScopeInfo,
) -> str | None:
    if isinstance(node, ast.Name):
        return names.get(node.id)
    attribute_name = _receiver_attribute_name(node, receiver_name)
    if attribute_name is not None:
        return attributes.get(attribute_name)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        kinds = {_value_kind(value, names, attributes, receiver_name, scope) for value in node.values}
        return kinds.pop() if len(kinds) == 1 else None
    if isinstance(node, ast.Subscript):
        container_kind = _value_kind(node.value, names, attributes, receiver_name, scope)
        return {"client": "database", "database": "collection"}.get(container_kind) if container_kind else None
    if not isinstance(node, ast.Call):
        return None
    if _is_client_call(node, scope):
        return "client"
    if not isinstance(node.func, ast.Attribute):
        return None
    receiver_kind = _value_kind(node.func.value, names, attributes, receiver_name, scope)
    if node.func.attr == "get_database" and receiver_kind == "client":
        return "database"
    if node.func.attr == "get_collection" and receiver_kind == "database":
        return "collection"
    return None


def _is_client_annotation(annotation: ast.expr | None, scope: ScopeInfo) -> bool:
    unwrapped = unwrap_nullable_annotation(annotation)
    return unwrapped is not None and _is_client_qname(qname(unwrapped, scope))


def _is_client_call(node: ast.Call, scope: ScopeInfo) -> bool:
    return _is_client_qname(qname(node.func, scope))


def _is_client_qname(qname: tuple[str, ...] | None) -> bool:
    return qname is not None and qname[0] == "pymongo" and qname[-1] == "MongoClient"


def _receiver_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    positional_arguments = [*node.args.posonlyargs, *node.args.args]
    return positional_arguments[0].arg if positional_arguments else None


def _receiver_attribute_name(node: ast.expr, receiver_name: str | None) -> str | None:
    if (
        receiver_name is not None
        and isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == receiver_name
    ):
        return node.attr
    return None
