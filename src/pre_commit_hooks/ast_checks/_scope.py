"""Shared Python lexical-scope traversal for AST-based checks.

Multiple checks need to walk an AST subtree without crossing into a nested
scope's own bindings — a name bound inside a nested function, lambda,
comprehension, or class body doesn't affect the enclosing scope's name
resolution. Each check used to hand-roll its own `ast.NodeVisitor` for this;
this module is the one shared implementation of that traversal.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

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


def iter_within_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants of `node` without crossing into a nested scope.

    A nested function/lambda/comprehension/class is itself yielded (so
    callers can still inspect e.g. its name or parameters), but traversal
    does not continue into its body, since it introduces independent Python
    scoping — a binding inside it doesn't affect `node`'s own scope.
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, SCOPE_NODES):
            yield from iter_within_scope(child)


def collect_scope_names(scope: ast.AST) -> set[str]:
    """Collect every `Name` identifier bound or read directly within `scope`.

    Excludes names from nested functions/lambdas/comprehensions/classes,
    matching Python's own scoping rules.
    """
    return {node.id for node in iter_within_scope(scope) if isinstance(node, ast.Name)}
