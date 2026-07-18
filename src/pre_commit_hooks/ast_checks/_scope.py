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
    for child in ast.iter_child_nodes(node):
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
