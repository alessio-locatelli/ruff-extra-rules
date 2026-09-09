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
    in_comparison_operand: bool
    in_identity_comparison: bool
    in_membership_test: bool
    purepath_ambiguous: bool
    used_in_string_interpolation: bool


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
            in_comparison_operand=id(raw.call) in scan.comparison_operands,
            in_identity_comparison=id(raw.call) in scan.identity_operands,
            in_membership_test=id(raw.call) in scan.membership_operands,
            purepath_ambiguous=scan.purepath_ambiguous,
            used_in_string_interpolation=id(raw.call) in scan.interpolated,
        )
        for raw in scan.raw_candidates
        if raw.constructor in final_eligible
    ]


_BINDING_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_CAPTURE_PATTERN_TYPES = (ast.MatchAs, ast.MatchStar)


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
    comparison_operands: frozenset[int]
    identity_operands: frozenset[int]
    membership_operands: frozenset[int]
    purepath_ambiguous: bool
    interpolated: frozenset[int]
    raw_candidates: list[_RawCandidate]


def _is_string_literal(value: ast.expr) -> bool:
    return (isinstance(value, ast.Constant) and isinstance(value.value, str)) or isinstance(value, ast.JoinedStr)


def _binding_base_name(target: ast.expr) -> ast.Name | None:
    if isinstance(target, ast.Name):
        return target
    if isinstance(target, (ast.Attribute, ast.Subscript)) and isinstance(target.value, ast.Name):
        return target.value
    return None


def _simple_bindings(node: ast.AST) -> list[tuple[ast.Name, ast.expr]]:
    if isinstance(node, ast.Assign):
        bindings = []
        for target in node.targets:
            base = _binding_base_name(target)
            if base is not None:
                bindings.append((base, node.value))
        return bindings
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        base = _binding_base_name(node.target)
        return [(base, node.value)] if base is not None else []
    if isinstance(node, ast.NamedExpr):
        base = _binding_base_name(node.target)
        return [(base, node.value)] if base is not None else []
    return []


def _alias_closure(start_names: set[str], alias_edges: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    pending = list(start_names)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(alias_edges.get(name, ()))
    return seen


def _collect_bindings(tree: ast.Module) -> tuple[dict[int, set[str]], dict[str, set[str]], frozenset[str]]:
    call_target_names: dict[int, set[str]] = {}
    alias_edges: dict[str, set[str]] = {}
    string_literal_names: set[str] = set()
    for node in ast.walk(tree):
        for target, value in _simple_bindings(node):
            for sub in ast.walk(value):
                if isinstance(sub, ast.Call):
                    call_target_names.setdefault(id(sub), set()).add(target.id)
            if isinstance(value, ast.Name):
                alias_edges.setdefault(value.id, set()).add(target.id)
            if _is_string_literal(value):
                string_literal_names.add(target.id)
    return call_target_names, alias_edges, frozenset(_alias_closure(string_literal_names, alias_edges))


def _looks_like_a_format_string(left: ast.expr, string_literal_names: frozenset[str]) -> bool:
    if _is_string_literal(left):
        return True
    return isinstance(left, ast.Name) and left.id in string_literal_names


def _interpolated_exprs(node: ast.AST, string_literal_names: frozenset[str]) -> list[ast.expr]:
    if isinstance(node, ast.JoinedStr):
        return [value.value for value in node.values if isinstance(value, ast.FormattedValue)]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and _looks_like_a_format_string(node.func.value, string_literal_names)
    ):
        return [*node.args, *(keyword.value for keyword in node.keywords)]
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and _looks_like_a_format_string(node.left, string_literal_names)
    ):
        right = node.right
        if isinstance(right, ast.Tuple):
            return right.elts
        if isinstance(right, ast.Dict):
            return [item for item in (*right.keys, *right.values) if item is not None]
        return [right]
    return []


def _an_alias_is_interpolated(
    target_names: set[str], alias_edges: dict[str, set[str]], interpolated_names: set[str]
) -> bool:
    return bool(_alias_closure(target_names, alias_edges) & interpolated_names)


def _scan(tree: ast.Module, eligible: frozenset[str]) -> _Scan:
    has_wildcard_import = False
    shadowed: set[str] = set()
    purepath_shadowed: set[str] = set()
    len_wrapped: set[int] = set()
    comparison_operands: set[int] = set()
    identity_operands: set[int] = set()
    membership_operands: set[int] = set()
    interpolated_names: set[str] = set()
    interpolated_call_ids: set[int] = set()
    raw_candidates: list[_RawCandidate] = []
    assign_target_names_for_value, alias_edges, string_literal_names = _collect_bindings(tree)

    for node in ast.walk(tree):
        for interpolated_expr in _interpolated_exprs(node, string_literal_names):
            for sub in ast.walk(interpolated_expr):
                if isinstance(sub, ast.Name):
                    interpolated_names.add(sub.id)
                elif isinstance(sub, ast.Call):
                    interpolated_call_ids.add(id(sub))

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
                _mark_call_ids(operands[index], comparison_operands)
                _mark_call_ids(operands[index + 1], comparison_operands)
                if isinstance(op, (ast.Is, ast.IsNot)):
                    _mark_call_ids(operands[index], identity_operands)
                    _mark_call_ids(operands[index + 1], identity_operands)
                if isinstance(op, (ast.In, ast.NotIn)):
                    _mark_call_ids(operands[index], membership_operands)
                    _mark_call_ids(operands[index + 1], membership_operands)

    interpolated = interpolated_call_ids | {
        call_id
        for call_id, target_names in assign_target_names_for_value.items()
        if _an_alias_is_interpolated(target_names, alias_edges, interpolated_names)
    }
    return _Scan(
        has_wildcard_import=has_wildcard_import,
        shadowed=frozenset(shadowed),
        len_wrapped=frozenset() if "len" in shadowed else frozenset(len_wrapped),
        comparison_operands=frozenset(comparison_operands),
        identity_operands=frozenset(identity_operands),
        membership_operands=frozenset(membership_operands),
        purepath_ambiguous=bool(purepath_shadowed & PUREPATH_HOVER_NAMES),
        interpolated=frozenset(interpolated),
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
