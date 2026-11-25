from __future__ import annotations

import ast
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_COMPREHENSION_NODES: tuple[type[ast.AST], ...] = (
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

_DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walrus_targets(nodes: Iterable[ast.AST]) -> Iterator[ast.Name]:
    for node in nodes:
        if isinstance(node, ast.NamedExpr):
            yield node.target
        if isinstance(node, _DEFINITION_NODES):
            yield from _walrus_targets(definition_header(node))
        else:
            yield from _walrus_targets(ast.iter_child_nodes(node))


def iter_within_scope(node: ast.AST) -> Iterator[ast.AST]:
    yield from iter_within_scope_from(ast.iter_child_nodes(node))


def definition_header(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef) -> list[ast.expr]:
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]
    decorators = [] if isinstance(node, ast.Lambda) else node.decorator_list
    keyword_defaults = [default for default in node.args.kw_defaults if default is not None]
    return [*decorators, *node.args.defaults, *keyword_defaults]


def iter_within_scope_from(children: Iterable[ast.AST]) -> Iterator[ast.AST]:
    for child in children:
        yield child
        if isinstance(child, _COMPREHENSION_NODES):
            yield from _walrus_targets(ast.iter_child_nodes(child))
        elif isinstance(child, _DEFINITION_NODES):
            yield from iter_within_scope_from(definition_header(child))
        else:
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
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            names.add(node.name)
            self._visit_function_header(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            names.add(node.name)
            self._visit_function_header(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            names.add(node.name)
            for child in [*definition_header(node), *node.type_params]:
                self.visit(child)

        def _visit_function_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            for child in definition_header(node):
                self.visit(child)
            for argument in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                *([node.args.vararg] if node.args.vararg else []),
                *([node.args.kwarg] if node.args.kwarg else []),
            ]:
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            if node.returns is not None:
                self.visit(node.returns)
            for type_param in node.type_params:
                self.visit(type_param)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            for child in definition_header(node):
                self.visit(child)

        def visit_ListComp(self, _node: ast.ListComp) -> None:
            return

        def visit_SetComp(self, _node: ast.SetComp) -> None:
            return

        def visit_DictComp(self, _node: ast.DictComp) -> None:
            return

        def visit_GeneratorExp(self, _node: ast.GeneratorExp) -> None:
            return

        def visit_Name(self, node: ast.Name) -> None:
            names.update(iter_binding_names(node))

        def visit_Import(self, node: ast.Import) -> None:
            names.update(iter_binding_names(node))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            names.update(iter_binding_names(node))

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            names.update(iter_binding_names(node))
            self.generic_visit(node)

        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            names.update(iter_binding_names(node))
            self.generic_visit(node)

        def visit_MatchStar(self, node: ast.MatchStar) -> None:
            names.update(iter_binding_names(node))
            self.generic_visit(node)

        def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
            names.update(iter_binding_names(node))
            self.generic_visit(node)

    visitor = Visitor()
    for statement in node.body:
        visitor.visit(statement)
    return names


def class_scope_global_or_nonlocal_names(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, _node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, _node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, _node: ast.ClassDef) -> None:
            return

        def visit_Global(self, node: ast.Global) -> None:
            names.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            names.update(node.names)

    visitor = Visitor()
    for statement in node.body:
        visitor.visit(statement)
    return names
