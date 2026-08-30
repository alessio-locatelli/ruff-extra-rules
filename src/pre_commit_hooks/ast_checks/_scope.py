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

_WALRUS_BOUNDARY = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walrus_targets(node: ast.AST) -> Iterator[ast.Name]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.NamedExpr):
            yield child.target
        if not isinstance(child, _WALRUS_BOUNDARY):
            yield from _walrus_targets(child)


def iter_within_scope(node: ast.AST) -> Iterator[ast.AST]:
    yield from iter_within_scope_from(ast.iter_child_nodes(node))


def iter_within_scope_from(children: Iterable[ast.AST]) -> Iterator[ast.AST]:
    for child in children:
        yield child
        if isinstance(child, _COMPREHENSION_NODES):
            yield from _walrus_targets(child)
        elif not isinstance(child, SCOPE_NODES):
            yield from iter_within_scope(child)


def collect_scope_names(scope: ast.AST) -> set[str]:
    return {node.id for node in iter_within_scope(scope) if isinstance(node, ast.Name)}


def iter_binding_names(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
        yield node.id
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        yield node.name
    elif isinstance(node, ast.Import):
        yield from (alias.asname or alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        yield from (alias.asname or alias.name for alias in node.names if alias.name != "*")
    elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name is not None:
        yield node.name
    elif isinstance(node, ast.MatchMapping) and node.rest is not None:
        yield node.rest


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

        def visit_Lambda(self, child: ast.Lambda) -> None:
            for default in [*child.args.defaults, *child.args.kw_defaults]:
                if default is not None:
                    self.visit(default)

        def visit_ListComp(self, _child: ast.ListComp) -> None:
            return

        def visit_SetComp(self, _child: ast.SetComp) -> None:
            return

        def visit_DictComp(self, _child: ast.DictComp) -> None:
            return

        def visit_GeneratorExp(self, _child: ast.GeneratorExp) -> None:
            return

        def visit_Name(self, child: ast.Name) -> None:
            names.update(iter_binding_names(child))

        def visit_Import(self, child: ast.Import) -> None:
            names.update(iter_binding_names(child))

        def visit_ImportFrom(self, child: ast.ImportFrom) -> None:
            names.update(iter_binding_names(child))

        def visit_ExceptHandler(self, child: ast.ExceptHandler) -> None:
            names.update(iter_binding_names(child))
            self.generic_visit(child)

        def visit_MatchAs(self, child: ast.MatchAs) -> None:
            names.update(iter_binding_names(child))
            self.generic_visit(child)

        def visit_MatchStar(self, child: ast.MatchStar) -> None:
            names.update(iter_binding_names(child))
            self.generic_visit(child)

        def visit_MatchMapping(self, child: ast.MatchMapping) -> None:
            names.update(iter_binding_names(child))
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
