"""Shared Python lexical-scope traversal for AST-based checks.

Multiple checks need to walk an AST subtree without crossing into a nested
scope's own bindings — a name bound inside a nested function, lambda,
comprehension, or class body doesn't affect the enclosing scope's name
resolution. Each check used to hand-roll its own `ast.NodeVisitor` for this;
this module is the one shared implementation of that traversal.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

SCOPE_NODES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.ClassDef,
)

_COMPREHENSION_NODES: tuple[type[ast.AST], ...] = (
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

# Comprehensions are transparent here (unlike in SCOPE_NODES): a walrus
# (`:=`) target inside one binds to the nearest *enclosing* non-comprehension
# scope per PEP 572, not to the comprehension's own scope, so hunting for one
# must still look inside nested comprehensions. It must not cross into a
# nested function/lambda/class though — that binds its own walrus targets
# locally, not to whatever scope contains the comprehension.
_WALRUS_BOUNDARY = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walrus_targets(node: ast.AST) -> Iterator[ast.Name]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.NamedExpr):
            yield child.target
        if not isinstance(child, _WALRUS_BOUNDARY):
            yield from _walrus_targets(child)


def iter_within_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants of `node` without crossing into a nested scope.

    A nested function/lambda/comprehension/class is itself yielded (so
    callers can still inspect e.g. its name or parameters), but traversal
    does not continue into its body, since it introduces independent Python
    scoping — a binding inside it doesn't affect `node`'s own scope. The one
    exception is a walrus (`:=`) target inside a comprehension, which PEP
    572 binds to `node`'s own scope rather than the comprehension's.
    """
    yield from iter_within_scope_from(ast.iter_child_nodes(node))


def iter_within_scope_from(children: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Like `iter_within_scope`, but starting from an explicit, caller-chosen
    set of `children` instead of `ast.iter_child_nodes(node)` — for a caller
    that already has its own, non-default notion of which of a node's
    fields belong to a given scope (e.g. `meaningless_vars._own_scope_children`,
    which excludes a function's decorators/defaults/annotations: those
    actually run in the *enclosing* scope, not the function's own).

    Each child is still checked against `SCOPE_NODES`/`_COMPREHENSION_NODES`
    itself before recursing into *its* descendants — a child that is itself
    a nested function/lambda/comprehension is yielded but not descended
    into, exactly as in `iter_within_scope`, so a caller can safely pass
    children that happen to include one without wrongly crossing into its
    own, separate scope.
    """
    for child in children:
        yield child
        if isinstance(child, _COMPREHENSION_NODES):
            yield from _walrus_targets(child)
        elif not isinstance(child, SCOPE_NODES):
            yield from iter_within_scope(child)


def collect_scope_names(scope: ast.AST) -> set[str]:
    """Collect every `Name` identifier bound or read directly within `scope`.

    Excludes names from nested functions/lambdas/comprehensions/classes,
    matching Python's own scoping rules.
    """
    return {node.id for node in iter_within_scope(scope) if isinstance(node, ast.Name)}


def class_scope_binding_names(node: ast.ClassDef) -> set[str]:
    names = {
        type_param.name
        for type_param in node.type_params
        if isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
    }

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            names.add(child.name)
            self._visit_function_header(child)

        def visit_AsyncFunctionDef(self, child: ast.AsyncFunctionDef) -> None:
            names.add(child.name)
            self._visit_function_header(child)

        def visit_ClassDef(self, child: ast.ClassDef) -> None:
            names.add(child.name)
            for decorator in child.decorator_list:
                self.visit(decorator)
            for base in child.bases:
                self.visit(base)
            for keyword in child.keywords:
                self.visit(keyword.value)
            for type_param in child.type_params:
                self.visit(type_param)

        def _visit_function_header(self, child: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            for decorator in child.decorator_list:
                self.visit(decorator)
            for default in [*child.args.defaults, *child.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
            for argument in [
                *child.args.posonlyargs,
                *child.args.args,
                *child.args.kwonlyargs,
                *([child.args.vararg] if child.args.vararg else []),
                *([child.args.kwarg] if child.args.kwarg else []),
            ]:
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            if child.returns is not None:
                self.visit(child.returns)
            for type_param in child.type_params:
                self.visit(type_param)

        def visit_Lambda(self, _child: ast.Lambda) -> None:
            return

        def visit_ListComp(self, _child: ast.ListComp) -> None:
            return

        def visit_SetComp(self, _child: ast.SetComp) -> None:
            return

        def visit_DictComp(self, _child: ast.DictComp) -> None:
            return

        def visit_GeneratorExp(self, _child: ast.GeneratorExp) -> None:
            return

        def visit_Name(self, child: ast.Name) -> None:
            if isinstance(child.ctx, ast.Store | ast.Del):
                names.add(child.id)

        def visit_Import(self, child: ast.Import) -> None:
            names.update(alias.asname or alias.name.split(".")[0] for alias in child.names)

        def visit_ImportFrom(self, child: ast.ImportFrom) -> None:
            names.update(alias.asname or alias.name for alias in child.names)

        def visit_ExceptHandler(self, child: ast.ExceptHandler) -> None:
            if child.name is not None:
                names.add(child.name)
            self.generic_visit(child)

        def visit_MatchAs(self, child: ast.MatchAs) -> None:
            if child.name is not None:
                names.add(child.name)
            self.generic_visit(child)

        def visit_MatchStar(self, child: ast.MatchStar) -> None:
            if child.name is not None:
                names.add(child.name)
            self.generic_visit(child)

        def visit_MatchMapping(self, child: ast.MatchMapping) -> None:
            if child.rest is not None:
                names.add(child.rest)
            self.generic_visit(child)

    visitor = Visitor()
    for statement in node.body:
        visitor.visit(statement)
    return names


def class_scope_global_or_nonlocal_names(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, _child: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, _child: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, _child: ast.ClassDef) -> None:
            return

        def visit_Global(self, child: ast.Global) -> None:
            names.update(child.names)

        def visit_Nonlocal(self, child: ast.Nonlocal) -> None:
            names.update(child.names)

    visitor = Visitor()
    for statement in node.body:
        visitor.visit(statement)
    return names
