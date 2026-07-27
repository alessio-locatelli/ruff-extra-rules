"""AST-level candidate detection for TRI006, before any `ty` session is involved. See ADR-0035."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(slots=True, frozen=True)
class Candidate:
    """One `constructor(argument)` call eligible to be flagged; redundancy is decided later, in `session.py`."""

    constructor: str
    call: ast.Call
    arg: ast.expr
    line: int  # 1-indexed, matching Violation.line
    call_start_col: int  # 0-indexed UTF-8 byte offset (ast.col_offset)
    call_end_col: int
    arg_start_col: int
    arg_end_col: int


def find_candidates(tree: ast.Module, eligible: frozenset[str]) -> list[Candidate]:
    """Every `name(single_positional_arg)` call anywhere in `tree` where `name` is one of `eligible`'s constructors.

    Excludes: keyword/zero/multi-argument calls, a starred argument, a
    call spanning multiple physical lines (see ADR-0035's "Detection
    method"), a shadowed constructor name (`_shadowed_names()`), every
    candidate in a module with a wildcard import
    (`_has_wildcard_import()`), and an argument ending in a nested call
    (`_ends_in_call()`).
    """
    if _has_wildcard_import(tree):
        return []
    return list(_iter_candidates(tree, eligible - _shadowed_names(tree)))


def _has_wildcard_import(tree: ast.Module) -> bool:
    # `from module import *` can bind any name, including a constructor's
    # own name, without _shadowed_names() ever seeing what name that was --
    # resolving the target module's real exports is out of scope here, so
    # every constructor is treated as potentially shadowed instead.
    return any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names) for node in ast.walk(tree)
    )


_BINDING_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_CAPTURE_PATTERN_TYPES = (ast.MatchAs, ast.MatchStar)


def _shadowed_names(tree: ast.Module) -> frozenset[str]:
    """Every name bound anywhere in `tree`: def/class, import (`as`-aware;
    a bare `import a.b.c` binds only its own top-level `a`), assignment
    target (also covers for/with/augmented-assign/comprehension/walrus),
    function/lambda parameter, `except ... as name`, match-case capture.

    Deliberately whole-module and scope-blind, not just the enclosing
    scope of a given call: false negatives (missing a real violation) are
    preferred over false positives (treating a user-defined `str`/`list`/
    etc. as the builtin, and reporting removing a call as safe when it
    actually changes behavior).
    """
    shadowed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            shadowed.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
            continue
        if isinstance(node, ast.ImportFrom):
            shadowed.update(alias.asname or alias.name for alias in node.names)
            continue
        name = _bound_name(node)
        if name is not None:
            shadowed.add(name)
    return frozenset(shadowed)


def _ends_in_call(arg: ast.expr) -> bool:
    # See ADR-0035's "Detection method" for why this can't be hovered reliably.
    return any(
        isinstance(node, ast.Call) and node.end_lineno == arg.end_lineno and node.end_col_offset == arg.end_col_offset
        for node in ast.walk(arg)
    )


def _bound_name(node: ast.AST) -> str | None:
    if isinstance(node, _BINDING_DEF_TYPES):
        return node.name
    if isinstance(node, ast.arg):
        return node.arg
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return node.id
    if isinstance(node, ast.ExceptHandler):
        return node.name
    if isinstance(node, _CAPTURE_PATTERN_TYPES):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


def _iter_candidates(tree: ast.Module, eligible: frozenset[str]) -> Iterator[Candidate]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id not in eligible:
            continue
        if node.keywords or len(node.args) != 1:
            continue
        (arg,) = node.args
        if isinstance(arg, ast.Starred):
            continue
        if node.end_lineno is None or node.end_col_offset is None:
            continue
        if node.lineno != node.end_lineno:
            continue
        if arg.end_lineno is None or arg.end_col_offset is None:
            continue
        if _ends_in_call(arg):
            continue

        yield Candidate(
            constructor=node.func.id,
            call=node,
            arg=arg,
            line=node.lineno,
            call_start_col=node.col_offset,
            call_end_col=node.end_col_offset,
            arg_start_col=arg.col_offset,
            arg_end_col=arg.end_col_offset,
        )
