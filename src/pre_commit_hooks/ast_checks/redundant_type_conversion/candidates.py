from __future__ import annotations

import ast
from dataclasses import dataclass

from .confidence import PUREPATH_HOVER_NAMES


@dataclass(slots=True, frozen=True)
class Candidate:
    constructor: str
    call: ast.Call
    arg: ast.expr
    line: int
    call_start_col: int
    call_end_col: int
    arg_start_col: int
    arg_end_col: int
    wrapped_in_len: bool
    in_equality_comparison: bool


def find_candidates(tree: ast.Module, eligible: frozenset[str]) -> list[Candidate]:
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
