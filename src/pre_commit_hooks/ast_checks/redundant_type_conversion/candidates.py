from __future__ import annotations

import ast
from dataclasses import dataclass

from .confidence import PUREPATH_HOVER_NAMES


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
    argument the hover can't describe as a whole
    (`_hover_would_miss_the_argument()`). All scanned in
    one `_scan()` pass over `tree` -- `_scan()` already collects every
    structurally-eligible call (`raw_candidates` below) while it computes
    `shadowed`, so this never needs its own second pass over `tree` just
    to re-find them.
    """
    scan = _scan(tree, eligible)
    if scan.has_wildcard_import:
        return []
    final_eligible = eligible - scan.shadowed
    return [
        Candidate(
            constructor=raw.constructor,
            call=raw.call,
            arg=raw.arg,
            line=raw.line,
            call_start_col=raw.call_start_col,
            call_end_col=raw.call_end_col,
            arg_start_col=raw.arg_start_col,
            arg_end_col=raw.arg_end_col,
            wrapped_in_len=id(raw.call) in scan.len_wrapped,
            in_equality_comparison=id(raw.call) in scan.equality_compared,
        )
        for raw in scan.raw_candidates
        if raw.constructor in final_eligible
    ]


_BINDING_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_CAPTURE_PATTERN_TYPES = (ast.MatchAs, ast.MatchStar)
_EQUALITY_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot)


@dataclass(slots=True, frozen=True)
class _RawCandidate:
    """A `name(single_positional_arg)` call found during `_scan()`'s own walk, already past every *local*
    structural filter (arg shape, single-line span, an argument the hover can describe as a whole) --
    everything about it that
    doesn't depend on whole-module shadowing, which isn't known until the scan finishes. `find_candidates()`
    applies the final `eligible - shadowed` filter against this already-narrow list instead of re-walking `tree`
    a second time just to re-find the same calls.
    """

    constructor: str
    call: ast.Call
    arg: ast.expr
    line: int
    call_start_col: int
    call_end_col: int
    arg_start_col: int
    arg_end_col: int


@dataclass(slots=True, frozen=True)
class _Scan:
    has_wildcard_import: bool
    shadowed: frozenset[str]
    len_wrapped: frozenset[int]
    equality_compared: frozenset[int]
    raw_candidates: list[_RawCandidate]


def _scan(tree: ast.Module, eligible: frozenset[str]) -> _Scan:
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

    `raw_candidates`: pre-filtered by `node.func.id in eligible` (the raw
    set, before subtracting `shadowed`) -- cheap, and safe to do this
    early: `eligible - shadowed` can only ever shrink `eligible`, so
    nothing this filters out here could have passed the final filter
    either, and it skips the pricier remaining checks (span, hover anchor)
    for a call whose name was never eligible to begin with.
    """
    has_wildcard_import = False
    shadowed: set[str] = set()
    purepath_shadowed: set[str] = set()
    len_wrapped: set[int] = set()
    equality_compared: set[int] = set()
    raw_candidates: list[_RawCandidate] = []

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

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords and len(node.args) == 1:
            (only_arg,) = node.args
            if node.func.id == "len" and isinstance(only_arg, ast.Call):
                len_wrapped.add(id(only_arg))

            if (
                node.func.id in eligible
                and not isinstance(only_arg, (ast.Starred, ast.GeneratorExp))
                and node.end_lineno is not None
                and node.end_col_offset is not None
                and node.lineno == node.end_lineno
                and only_arg.end_lineno is not None
                and only_arg.end_col_offset is not None
                and not _hover_would_miss_the_argument(only_arg)
            ):
                raw_candidates.append(
                    _RawCandidate(
                        constructor=node.func.id,
                        call=node,
                        arg=only_arg,
                        line=node.lineno,
                        call_start_col=node.col_offset,
                        call_end_col=node.end_col_offset,
                        arg_start_col=only_arg.col_offset,
                        arg_end_col=only_arg.end_col_offset,
                    )
                )

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
        raw_candidates=raw_candidates,
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


def _hover_would_miss_the_argument(arg: ast.expr) -> bool:
    # See ADR-0035's "Detection method" for why neither shape can be hovered reliably.
    if isinstance(arg, ast.Call):
        return True
    return any(
        node is not arg
        and isinstance(node, ast.expr)
        and node.end_lineno == arg.end_lineno
        and node.end_col_offset == arg.end_col_offset
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
