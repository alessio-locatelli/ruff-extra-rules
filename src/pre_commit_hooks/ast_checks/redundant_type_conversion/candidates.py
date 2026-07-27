"""AST-level candidate detection for TRI006, before any `ty` session is involved. See ADR-0035."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .confidence import PUREPATH_HOVER_NAMES

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
    wrapped_in_len: bool  # See ADR-0035's `len()` sink exclusion.
    in_equality_comparison: bool  # See ADR-0035's Path-vs-str comparison exclusion.


def find_candidates(tree: ast.Module, eligible: frozenset[str]) -> list[Candidate]:
    """Every `name(single_positional_arg)` call anywhere in `tree` where `name` is one of `eligible`'s constructors.

    Excludes: keyword/zero/multi-argument calls, a starred argument, a
    generator-expression argument, a call spanning multiple physical
    lines (see ADR-0035's "Detection method"), a shadowed constructor
    name, every candidate in a module with a wildcard import, and an
    argument ending in a nested call (`_ends_in_call()`). All scanned in
    one `_scan()` pass over `tree`.
    """
    scan = _scan(tree)
    if scan.has_wildcard_import:
        return []
    return list(_iter_candidates(tree, eligible - scan.shadowed, scan.len_wrapped, scan.equality_compared))


_BINDING_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_CAPTURE_PATTERN_TYPES = (ast.MatchAs, ast.MatchStar)
_EQUALITY_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot)


@dataclass(slots=True, frozen=True)
class _Scan:
    has_wildcard_import: bool
    shadowed: frozenset[str]
    len_wrapped: frozenset[int]
    equality_compared: frozenset[int]


def _scan(tree: ast.Module) -> _Scan:
    """One walk over `tree` computing every whole-module signal `find_candidates()` needs.

    `shadowed`: every name bound anywhere in `tree` -- def/class, import
    (`as`-aware; a bare `import a.b.c` binds only its own top-level `a`),
    assignment target (also covers for/with/augmented-assign/
    comprehension/walrus), function/lambda parameter, `except ... as
    name`, match-case capture. Deliberately whole-module and scope-blind,
    not just the enclosing scope of a given call: false negatives
    (missing a real violation) are preferred over false positives
    (treating a user-defined `str`/`list`/etc. as the builtin, and
    reporting removing a call as safe when it actually changes behavior).
    A wildcard import (`from module import *`) can bind any name at all,
    including a constructor's own, without ever appearing in `shadowed`
    itself -- `has_wildcard_import` catches that instead.

    `len_wrapped`/`equality_compared`: see ADR-0035's `len()` sink
    exclusion and Path-vs-str comparison exclusion, respectively. The
    latter is cleared entirely (not just for the specific name involved)
    if a `pathlib` path class name is bound to anything other than an
    ordinary `from pathlib import <Name>` anywhere in `tree`, the same
    scope-blind bias as `shadowed` itself.
    """
    has_wildcard_import = False
    shadowed: set[str] = set()
    purepath_shadowed: set[str] = set()
    len_wrapped: set[int] = set()
    equality_compared: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                has_wildcard_import = True
            bound = {alias.asname or alias.name for alias in node.names}
            shadowed.update(bound)
            if node.module == "pathlib" and node.level == 0:
                purepath_shadowed.update(
                    alias.asname for alias in node.names if alias.asname and alias.asname != alias.name
                )
            else:
                purepath_shadowed.update(bound)
            continue
        if isinstance(node, ast.Import):
            bound = {alias.asname or alias.name.split(".", 1)[0] for alias in node.names}
            shadowed.update(bound)
            purepath_shadowed.update(bound)
            continue

        name = _bound_name(node)
        if name is not None:
            shadowed.add(name)
            purepath_shadowed.add(name)

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "len"
            and not node.keywords
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Call)
        ):
            len_wrapped.add(id(node.args[0]))

        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for index, op in enumerate(node.ops):
                if isinstance(op, _EQUALITY_OPS):
                    _mark_call_ids(operands[index], equality_compared)
                    _mark_call_ids(operands[index + 1], equality_compared)

    return _Scan(
        has_wildcard_import=has_wildcard_import,
        shadowed=frozenset(shadowed),
        len_wrapped=frozenset() if "len" in shadowed else frozenset(len_wrapped),
        equality_compared=(frozenset() if purepath_shadowed & PUREPATH_HOVER_NAMES else frozenset(equality_compared)),
    )


def _mark_call_ids(operand: ast.expr, ids: set[int]) -> None:
    if isinstance(operand, ast.Call):
        ids.add(id(operand))
    elif isinstance(operand, (ast.List, ast.Tuple, ast.Set)):
        for elt in operand.elts:
            _mark_call_ids(elt, ids)
    elif isinstance(operand, ast.Dict):
        for key, value in zip(operand.keys, operand.values, strict=True):
            if key is not None:
                _mark_call_ids(key, ids)
            _mark_call_ids(value, ids)


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


def _iter_candidates(
    tree: ast.Module, eligible: frozenset[str], len_wrapped: frozenset[int], equality_compared: frozenset[int]
) -> Iterator[Candidate]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id not in eligible:
            continue
        if node.keywords or len(node.args) != 1:
            continue
        (arg,) = node.args
        if isinstance(arg, (ast.Starred, ast.GeneratorExp)):
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
            wrapped_in_len=id(node) in len_wrapped,
            in_equality_comparison=id(node) in equality_compared,
        )
