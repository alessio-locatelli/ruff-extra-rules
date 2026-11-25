from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from ._scope import class_scope_binding_names, definition_header, iter_binding_names, iter_within_scope_from

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

type _ComprehensionNode = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
type Replacement = tuple[int, int, str, str]


_CROSSABLE_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _binds_name_in_nested_scope(scope_node: ast.AST, name: str) -> bool:
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = scope_node.args
        all_args = [
            *args.args,
            *args.posonlyargs,
            *args.kwonlyargs,
            *([args.vararg] if args.vararg else []),
            *([args.kwarg] if args.kwarg else []),
        ]
        if any(arg.arg == name for arg in all_args):
            return True
        if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple) and type_param.name == name
            for type_param in scope_node.type_params
        ):
            return True

        for child in _iter_own_scope_descendants(scope_node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and child.name == name:
                return True
            if name in iter_binding_names(child):
                return True
        return False

    assert isinstance(scope_node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp)
    return any(
        isinstance(target, ast.Name) and target.id == name
        for generator in scope_node.generators
        for target in ast.walk(generator.target)
    )


def has_future_annotations_import(tree: ast.Module) -> bool:
    return any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "__future__"
        and any(a.name == "annotations" for a in stmt.names)
        for stmt in tree.body
    )


def _signature_annotations(args: ast.arguments) -> Iterator[ast.expr]:
    all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        all_args.append(args.vararg)
    if args.kwarg:
        all_args.append(args.kwarg)
    for arg in all_args:
        if arg.annotation is not None:
            yield arg.annotation


def _type_param_defaults_and_bounds(type_params: list[ast.type_param]) -> Iterator[ast.expr]:
    for type_param in type_params:
        if isinstance(type_param, ast.TypeVar):
            if type_param.bound is not None:
                yield type_param.bound
            if type_param.default_value is not None:
                yield type_param.default_value
        elif isinstance(type_param, ast.ParamSpec | ast.TypeVarTuple) and type_param.default_value is not None:
            yield type_param.default_value


def _peer_filtered_replace_names(type_params: list[ast.type_param], replace_names: dict[str, str]) -> dict[str, str]:
    peer_type_param_names = {
        type_param.name
        for type_param in type_params
        if isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
    }
    return {name: new for name, new in replace_names.items() if name not in peer_type_param_names}


def _outer_scope_children(
    scope_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | _ComprehensionNode,
    *,
    has_future_annotations: bool,
) -> Iterator[ast.AST]:
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef):
        yield from definition_header(scope_node)
        if not scope_node.type_params and not has_future_annotations:
            yield from _signature_annotations(scope_node.args)
            if scope_node.returns is not None:
                yield scope_node.returns
    elif isinstance(scope_node, ast.Lambda):
        yield from definition_header(scope_node)
    else:
        yield scope_node.generators[0].iter


def _own_scope_children(
    scope_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | _ComprehensionNode,
) -> Iterator[ast.AST]:
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef):
        yield from scope_node.body
    elif isinstance(scope_node, ast.Lambda):
        yield scope_node.body
    elif isinstance(scope_node, ast.DictComp):
        yield scope_node.key
        yield scope_node.value
        yield from _comprehension_own_scope_generators(scope_node.generators)
    else:
        yield scope_node.elt
        yield from _comprehension_own_scope_generators(scope_node.generators)


def _comprehension_own_scope_generators(generators: list[ast.comprehension]) -> Iterator[ast.AST]:
    for index, generator in enumerate(generators):
        if index > 0:
            yield generator.iter
        yield generator.target
        yield from generator.ifs


def _iter_own_scope_descendants(
    scope_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | _ComprehensionNode,
) -> Iterator[ast.AST]:
    yield from iter_within_scope_from(_own_scope_children(scope_node))


def collect_replacements(
    node: ast.AST,
    replace_names: dict[str, str],
    *,
    outer_replace_names: dict[str, str] | None = None,
    has_future_annotations: bool,
) -> list[Replacement]:
    if outer_replace_names is None:
        outer_replace_names = replace_names

    if isinstance(node, ast.Name):
        if node.id in replace_names:
            return [(node.lineno, node.col_offset, node.id, replace_names[node.id])]
        return []

    if isinstance(node, _CROSSABLE_SCOPE_NODES):
        replacements: list[Replacement] = []
        for outer_child in _outer_scope_children(node, has_future_annotations=has_future_annotations):
            replacements.extend(
                collect_replacements(
                    outer_child,
                    replace_names,
                    outer_replace_names=outer_replace_names,
                    has_future_annotations=has_future_annotations,
                )
            )

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.type_params:
            bound_default_names = _peer_filtered_replace_names(node.type_params, replace_names)
            if bound_default_names:
                for expr in _type_param_defaults_and_bounds(node.type_params):
                    replacements.extend(
                        collect_replacements(
                            expr,
                            bound_default_names,
                            outer_replace_names=bound_default_names,
                            has_future_annotations=has_future_annotations,
                        )
                    )

            if not has_future_annotations and bound_default_names:
                for expr in _signature_annotations(node.args):
                    replacements.extend(
                        collect_replacements(
                            expr,
                            bound_default_names,
                            outer_replace_names=bound_default_names,
                            has_future_annotations=has_future_annotations,
                        )
                    )
                if node.returns is not None:
                    replacements.extend(
                        collect_replacements(
                            node.returns,
                            bound_default_names,
                            outer_replace_names=bound_default_names,
                            has_future_annotations=has_future_annotations,
                        )
                    )

        nested_names = {
            name: new for name, new in outer_replace_names.items() if not _binds_name_in_nested_scope(node, name)
        }
        if nested_names:
            for own_child in _own_scope_children(node):
                replacements.extend(
                    collect_replacements(
                        own_child,
                        nested_names,
                        outer_replace_names=nested_names,
                        has_future_annotations=has_future_annotations,
                    )
                )
        return replacements

    if isinstance(node, ast.TypeAlias):
        replacements = collect_replacements(
            node.name,
            replace_names,
            outer_replace_names=outer_replace_names,
            has_future_annotations=has_future_annotations,
        )
        filtered_names = _peer_filtered_replace_names(node.type_params, replace_names)
        if filtered_names:
            for expr in _type_param_defaults_and_bounds(node.type_params):
                replacements.extend(
                    collect_replacements(
                        expr,
                        filtered_names,
                        outer_replace_names=filtered_names,
                        has_future_annotations=has_future_annotations,
                    )
                )
            replacements.extend(
                collect_replacements(
                    node.value,
                    filtered_names,
                    outer_replace_names=filtered_names,
                    has_future_annotations=has_future_annotations,
                )
            )
        return replacements

    if isinstance(node, ast.ClassDef):
        class_replacements: list[Replacement] = []

        def collect(
            target: ast.AST,
            replacements: dict[str, str],
            *,
            outer_names: dict[str, str],
        ) -> None:
            class_replacements.extend(
                collect_replacements(
                    target,
                    replacements,
                    outer_replace_names=outer_names,
                    has_future_annotations=has_future_annotations,
                )
            )

        class_names = class_scope_binding_names(node)
        class_replace_names = {name: new for name, new in outer_replace_names.items() if name not in class_names}
        if replace_names:
            for decorator in node.decorator_list:
                collect(decorator, replace_names, outer_names=outer_replace_names)
        header_names = _peer_filtered_replace_names(node.type_params, replace_names)
        if header_names:
            for base in node.bases:
                collect(base, header_names, outer_names=header_names)
            for keyword in node.keywords:
                collect(keyword.value, header_names, outer_names=header_names)
            for expression in _type_param_defaults_and_bounds(node.type_params):
                collect(expression, header_names, outer_names=header_names)
        for child in node.body:
            collect(child, class_replace_names, outer_names=outer_replace_names)
        return class_replacements

    return [
        replacement
        for child in ast.iter_child_nodes(node)
        for replacement in collect_replacements(
            child,
            replace_names,
            outer_replace_names=outer_replace_names,
            has_future_annotations=has_future_annotations,
        )
    ]


_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _class_scope_loads(class_node: ast.ClassDef) -> set[str]:
    loads: set[str] = set()
    pending: list[ast.AST] = list(class_node.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads.add(node.id)
        elif isinstance(node, _COMPREHENSIONS):
            pending.append(node.generators[0].iter)
        elif isinstance(node, _DEFINITIONS):
            pending.extend(definition_header(node))
            if not isinstance(node, ast.Lambda):
                pending.extend(_type_param_defaults_and_bounds(node.type_params))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                pending.extend(_signature_annotations(node.args))
                if node.returns is not None:
                    pending.append(node.returns)
        else:
            pending.extend(ast.iter_child_nodes(node))
    return loads


def class_bound_and_read_names(scope: ast.AST, names: Iterable[str]) -> set[str]:
    candidates = set(names)
    ambiguous: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.ClassDef):
            continue
        type_parameters = {
            parameter.name
            for parameter in node.type_params
            if isinstance(parameter, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
        }
        bound = (class_scope_binding_names(node) - type_parameters) & candidates
        if bound:
            ambiguous.update(bound & _class_scope_loads(node))
    return ambiguous


def collect_scope_replacements(
    scope: ast.AST, replace_names: dict[str, str], *, has_future_annotations: bool
) -> list[Replacement]:
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        children: Iterator[ast.AST] = _own_scope_children(scope)
    else:
        children = ast.iter_child_nodes(scope)
    return [
        replacement
        for child in children
        for replacement in collect_replacements(child, replace_names, has_future_annotations=has_future_annotations)
    ]
