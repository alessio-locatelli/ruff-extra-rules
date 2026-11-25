from __future__ import annotations

import ast
import builtins
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pre_commit_hooks.ast_checks._scope import (
    can_rebind_builtins,
    class_scope_binding_names,
    iter_binding_names,
    iter_within_scope_from,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .catalog import QualifiedName

type ScopeNode = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
type NestedScopeNode = FunctionNode | ast.ClassDef

_BUILTIN_NAMES = frozenset(dir(builtins))
_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True, slots=True)
class Binding:
    node: ast.AST
    qualified_name: QualifiedName | None


@dataclass(slots=True)
class Scope:
    node: ScopeNode
    parent: Scope | None
    bindings: dict[str, list[Binding]] = field(default_factory=lambda: defaultdict(list))
    wildcard: bool = False

    @property
    def is_class(self) -> bool:
        return isinstance(self.node, ast.ClassDef)


def function_arguments(node: FunctionNode) -> Iterator[ast.arg]:
    arguments = node.args
    yield from arguments.posonlyargs
    yield from arguments.args
    yield from arguments.kwonlyargs
    if arguments.vararg is not None:
        yield arguments.vararg
    if arguments.kwarg is not None:
        yield arguments.kwarg


def type_parameter_names(node: FunctionNode) -> frozenset[str]:
    return frozenset(
        type_parameter.name
        for type_parameter in node.type_params
        if isinstance(type_parameter, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
    )


def _enclosing_binder(scope: Scope, name: str) -> Scope:
    current = scope.parent
    while current is not None and (current.is_class or name not in current.bindings):
        current = current.parent
    assert current is not None
    return current


def _record_declaration(node: ast.Global | ast.Nonlocal, scope: Scope, module_scope: Scope) -> None:
    for name in node.names:
        target = module_scope if isinstance(node, ast.Global) else _enclosing_binder(scope, name)
        target.bindings[name].append(Binding(node, None))


def _import_bindings(node: ast.Import | ast.ImportFrom) -> Iterator[tuple[str, QualifiedName | None]]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname is None:
                first = alias.name.partition(".")[0]
                yield first, (first,)
            else:
                yield alias.asname, tuple(alias.name.split("."))
        return
    module = tuple(node.module.split(".")) if node.module is not None and node.level == 0 else None
    for alias in node.names:
        if alias.name != "*":
            yield alias.asname or alias.name, (*module, alias.name) if module is not None else None


class NameResolver:
    __slots__ = ("_attribute_stores", "_builtins_visible", "_rebound", "_scopes")

    def __init__(self, tree: ast.Module) -> None:
        self._builtins_visible = not can_rebind_builtins(tree)
        self._scopes: dict[ast.AST, Scope] = {}
        self._attribute_stores: list[tuple[ast.Attribute, Scope]] = []
        self._rebound: frozenset[QualifiedName] = frozenset()
        module_scope = Scope(tree, None)
        self._scopes[tree] = module_scope
        self._collect(module_scope, module_scope)
        self._rebound = frozenset(
            name for attribute, scope in self._attribute_stores for name in self.qualified_names(attribute, scope) or ()
        )

    def scope(self, node: ScopeNode) -> Scope:
        return self._scopes[node]

    def scopes(self) -> Iterator[Scope]:
        yield from self._scopes.values()

    def _collect(self, scope: Scope, module_scope: Scope) -> None:
        node = scope.node
        if isinstance(node, ast.ClassDef):
            for name in class_scope_binding_names(node):
                scope.bindings[name].append(Binding(node, None))
            nested = []
            for child in iter_within_scope_from(node.body):
                if isinstance(child, _SCOPE_TYPES):
                    nested.append(child)
                self._record_attribute_store(child, scope)
                if isinstance(child, ast.Global | ast.Nonlocal):
                    _record_declaration(child, scope, module_scope)
        else:
            nested = self._collect_statement_bindings(scope, module_scope)
        for child in nested:
            child_scope = Scope(child, scope)
            self._scopes[child] = child_scope
            self._collect(child_scope, module_scope)

    def _collect_statement_bindings(self, scope: Scope, module_scope: Scope) -> list[NestedScopeNode]:
        node = scope.node
        assert not isinstance(node, ast.ClassDef)
        nested: list[NestedScopeNode] = []
        declared: set[str] = set()
        candidates: list[tuple[str, Binding]] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            candidates.extend((argument.arg, Binding(argument, None)) for argument in function_arguments(node))
            candidates.extend((name, Binding(node, None)) for name in type_parameter_names(node))
        for child in iter_within_scope_from(node.body):
            if isinstance(child, _SCOPE_TYPES):
                nested.append(child)
            self._record_attribute_store(child, scope)
            if isinstance(child, ast.Global | ast.Nonlocal):
                declared.update(child.names)
                _record_declaration(child, scope, module_scope)
            elif isinstance(child, ast.Import | ast.ImportFrom):
                if isinstance(child, ast.ImportFrom) and any(alias.name == "*" for alias in child.names):
                    scope.wildcard = True
                candidates.extend((name, Binding(child, qualified)) for name, qualified in _import_bindings(child))
            else:
                candidates.extend((name, Binding(child, None)) for name in iter_binding_names(child))
        for name, binding in candidates:
            if name not in declared:
                scope.bindings[name].append(binding)
        return nested

    def _record_attribute_store(self, node: ast.AST, scope: Scope) -> None:
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            self._attribute_stores.append((node, scope))

    def _lookup(self, name: str, scope: Scope) -> list[Binding] | None:
        current: Scope | None = scope
        while current is not None:
            if current is scope or not current.is_class:
                bindings = current.bindings.get(name)
                if bindings:
                    return bindings
                if current.wildcard:
                    return None
            current = current.parent
        if self._builtins_visible and name in _BUILTIN_NAMES:
            return [Binding(scope.node, ("builtins", name))]
        return None

    def qualified_names(self, expression: ast.expr, scope: Scope) -> frozenset[QualifiedName] | None:
        if isinstance(expression, ast.Attribute):
            parents = self.qualified_names(expression.value, scope)
            if parents is None:
                return None
            names = frozenset((*parent, expression.attr) for parent in parents)
        elif isinstance(expression, ast.Name):
            bindings = self._lookup(expression.id, scope)
            if bindings is None:
                return None
            qualified = {binding.qualified_name for binding in bindings}
            if None in qualified:
                return None
            names = frozenset(name for name in qualified if name is not None)
        else:
            return None
        if names & self._rebound or (not self._builtins_visible and any(name[0] == "builtins" for name in names)):
            return None
        return names

    def local_class(self, expression: ast.expr, scope: Scope) -> ast.ClassDef | None:
        if not isinstance(expression, ast.Name):
            return None
        bindings = self._lookup(expression.id, scope)
        if bindings is None or len(bindings) != 1:
            return None
        node = bindings[0].node
        return node if isinstance(node, ast.ClassDef) and node.name == expression.id else None
