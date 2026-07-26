"""AST-level candidate detection for TRI006: every syntactic call shape
this check might flag, before any `ty` session is involved at all.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(slots=True, frozen=True)
class Candidate:
    """One `constructor(argument)` call syntactically eligible to be
    flagged — whether it actually *is* redundant is decided later, by
    `ty`'s own synthetic-rewrite-and-recheck (see `session.py`), never by
    this module.

    `call`/`arg` are kept only for callers that want the original AST
    nodes (e.g. building a `Violation`'s message from `arg`'s own source);
    every position field below is already extracted as a plain int so
    `ty_check()` doesn't have to re-derive them.
    """

    constructor: str
    call: ast.Call
    arg: ast.expr
    line: int  # 1-indexed, matching Violation.line
    call_start_col: int  # 0-indexed UTF-8 byte offset (ast.col_offset)
    call_end_col: int
    arg_start_col: int
    arg_end_col: int


def find_candidates(tree: ast.Module, eligible: frozenset[str]) -> list[Candidate]:
    """Every `Call` node anywhere in `tree` (any expression position —
    assignment RHS, call argument, return value, ... — `ast.walk` reaches
    all of them uniformly) shaped like `name(single_positional_arg)` where
    `name` is one of `eligible`'s builtin constructor names.

    Deliberately excludes:
    - Any call with keyword arguments, zero, or more than one positional
      argument (`int(x, base=2)`, `dict(**kwargs)`, `frozenset()`, ...) —
      none of these are the "wrap a single value" shape this check targets.
    - A starred single argument (`list(*x)`) — unpacking, not wrapping.
    - A call whose own `(` and `)` land on different physical lines. The
      synthetic rewrite this check performs (see `session.py`) splices out
      the constructor's own call syntax on a single line, preserving every
      other line's numbering exactly, which is what lets a before/after
      diagnostic comparison trust that a position match means the same
      diagnostic. Splicing across a newline could shift later lines'
      numbering instead, so a multi-line-wrapped call is silently left
      out of this check's coverage rather than risking a wrong redundancy
      verdict from a shifted line number.
    - A call whose own name is shadowed anywhere in the module (a local
      `def str(...):`, a `class str:`, an import bound to that name, a
      function/lambda parameter with that name, an `except ... as name`
      handler, a pattern-match capture, or any other assignment target
      with that name) — see `_shadowed_names()`.
    """
    return list(_iter_candidates(tree, eligible - _shadowed_names(tree)))


_BINDING_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_CAPTURE_PATTERN_TYPES = (ast.MatchAs, ast.MatchStar)


def _shadowed_names(tree: ast.Module) -> frozenset[str]:
    """Every name bound anywhere in `tree`, by any means: a function/class
    definition; an import (respecting `as`); a plain assignment target
    (covers `for`/`with`/augmented-assignment/comprehension/walrus targets
    too, since each compiles to a `Name` node with `Store` context); a
    function or lambda parameter (`ast.arg`, a distinct node type from
    `ast.Name` — a parameter named `str` in `def f(str): return str(x)`
    shadows the builtin just as much as a local variable of that name
    would, and is otherwise invisible to this scan); an `except ... as
    name:` handler; or a `match`/`case` capture pattern (`case str:` or
    `case [*str]`/`case {**str}`).

    Deliberately whole-module and scope-blind rather than resolving which
    binding is actually in scope at a given call site: `int`/`str`/etc.
    shadowed only in some unrelated function elsewhere in the file makes
    every same-named candidate in the whole module ineligible, not just
    the ones actually inside that shadowing scope. This can miss a real
    violation a precise, scope-aware analysis would still catch, but never
    the reverse — treating a user-defined `str`/`list`/etc. as if it were
    the builtin would report removing a call as safe when it actually
    changes behavior (the exact failure mode this function exists to
    prevent), which is far worse than an occasional missed detection.
    """
    shadowed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            shadowed.update(alias.asname or alias.name for alias in node.names)
            continue
        name = _bound_name(node)
        if name is not None:
            shadowed.add(name)
    return frozenset(shadowed)


def _bound_name(node: ast.AST) -> str | None:
    """The single name `node` itself binds, or `None` if it doesn't bind
    one at all (most AST node types) — see `_shadowed_names()` for why
    each of these node types counts as a binding.
    """
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
