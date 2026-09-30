from __future__ import annotations

import ast
import builtins
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pre_commit_hooks.ast_checks._scope import iter_binding_names, iter_within_scope

from .reorder import DefinedFarFromUseLevel, exposed_names, may_reorder

if TYPE_CHECKING:
    from collections.abc import Iterator

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_NESTED_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_LOOP_NODES = (ast.For, ast.AsyncFor, ast.While)
_TRY_NODES = (ast.Try, ast.TryStar)
_TERMINAL_NODES = (ast.Return, ast.Raise, ast.Continue, ast.Break)
_DYNAMIC_SCOPE_BUILTINS = frozenset({"locals", "vars", "exec", "eval"})
_REORDER_SENSITIVE_NODES = (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr)
_UBIQUITOUS_RECEIVERS = frozenset({"self", "cls"})


@dataclass(frozen=True, slots=True)
class Finding:
    target: ast.Name
    message: str


@dataclass(frozen=True, slots=True)
class _Options:
    level: DefinedFarFromUseLevel
    max_distance: int
    builtin_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class _FunctionFacts:
    occurrences: Counter[str]
    captured: frozenset[str]
    stable_locals: frozenset[str]
    parameters: frozenset[str]
    redeclared: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Block:
    statements: list[ast.stmt]
    names: list[Counter[str]]


def find_findings(tree: ast.Module, level: DefinedFarFromUseLevel, max_distance: int) -> list[Finding]:
    options = _Options(level, max_distance, _unshadowed_builtins(tree))
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, _FUNCTION_NODES):
            findings.extend(_function_findings(node, options))
    return findings


def _unshadowed_builtins(tree: ast.Module) -> frozenset[str]:
    bound = {name for node in ast.walk(tree) for name in iter_binding_names(node)}
    bound.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    return frozenset(dir(builtins)) - bound


def _function_findings(function: ast.FunctionDef | ast.AsyncFunctionDef, options: _Options) -> Iterator[Finding]:
    facts = _function_facts(function)
    if facts is None:
        return
    for statements, bound_before_block in _iter_blocks(function.body, facts.parameters):
        block: _Block | None = None
        bound = set(bound_before_block)
        for index, statement in enumerate(statements):
            target = _assignment_target(statement)
            if target is not None and target.id not in facts.redeclared | facts.captured:
                block = block or _Block(statements, [_name_counts(item) for item in statements])
                finding = _candidate_finding(block, index, target, facts, options, definitely_bound=frozenset(bound))
                if finding is not None:
                    yield finding
            _update_definitely_bound(bound, statement)


def _function_facts(function: ast.FunctionDef | ast.AsyncFunctionDef) -> _FunctionFacts | None:
    occurrences: Counter[str] = Counter()
    redeclared: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Name):
            occurrences[node.id] += 1
        elif isinstance(node, ast.Global | ast.Nonlocal):
            redeclared.update(node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _DYNAMIC_SCOPE_BUILTINS:
            return None
    arguments = function.args
    parameters = {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            arguments.vararg,
            arguments.kwarg,
        )
        if argument is not None
    }
    bound = {name for node in iter_within_scope(function) for name in iter_binding_names(node)}
    captured = {
        node.id
        for scope in ast.walk(function)
        if scope is not function and isinstance(scope, _NESTED_SCOPE_NODES)
        for node in ast.walk(scope)
        if isinstance(node, ast.Name)
    }
    return _FunctionFacts(
        occurrences,
        frozenset(captured),
        frozenset((parameters | bound) - redeclared),
        frozenset(parameters - redeclared),
        frozenset(redeclared),
    )


def _iter_blocks(
    statements: list[ast.stmt], bound_before_block: frozenset[str]
) -> Iterator[tuple[list[ast.stmt], frozenset[str]]]:
    yield statements, bound_before_block
    bound = set(bound_before_block)
    for statement in statements:
        if not isinstance(statement, _NESTED_SCOPE_NODES):
            for child in _child_blocks(statement):
                yield from _iter_blocks(child, frozenset(bound))
        _update_definitely_bound(bound, statement)


def _update_definitely_bound(bound: set[str], statement: ast.stmt) -> None:
    if isinstance(statement, ast.Assign | ast.AnnAssign | ast.AugAssign):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not isinstance(statement, ast.AnnAssign) or statement.value is not None:
            bound.update(
                node.id
                for target in targets
                for node in ast.walk(target)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            )
    elif isinstance(statement, (ast.Import, ast.ImportFrom, *_FUNCTION_NODES, ast.ClassDef)):
        bound.update(iter_binding_names(statement))
    bound.difference_update(
        name
        for node in ast.walk(statement)
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Del)) or isinstance(node, ast.ExceptHandler)
        for name in iter_binding_names(node)
    )


def _child_blocks(statement: ast.stmt) -> Iterator[list[ast.stmt]]:
    for field in ("body", "orelse", "finalbody"):
        child = getattr(statement, field, None)
        if isinstance(child, list) and child and isinstance(child[0], ast.stmt):
            yield child
    if isinstance(statement, _TRY_NODES):
        for handler in statement.handlers:
            yield handler.body
    elif isinstance(statement, ast.Match):
        for case in statement.cases:
            yield case.body


def _assignment_target(statement: ast.stmt) -> ast.Name | None:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        target = statement.target
    else:
        return None
    return target if isinstance(target, ast.Name) else None


def _name_counts(statement: ast.stmt) -> Counter[str]:
    return Counter(node.id for node in ast.walk(statement) if isinstance(node, ast.Name))


def _candidate_finding(
    block: _Block,
    index: int,
    target: ast.Name,
    facts: _FunctionFacts,
    options: _Options,
    *,
    definitely_bound: frozenset[str],
) -> Finding | None:
    name = target.id
    statements = block.statements
    use_index = next((position for position in range(index + 1, len(statements)) if block.names[position][name]), None)
    if (
        use_index is None
        or sum(counts[name] for counts in block.names[use_index:]) != facts.occurrences[name] - 1
        or not _reads_before_rebinding(statements[use_index], name)
    ):
        return None
    value = _assigned_value(statements[index])
    between = statements[index + 1 : use_index]
    if any(isinstance(node, _REORDER_SENSITIVE_NODES) for node in ast.walk(value)) or any(
        isinstance(statement, _TERMINAL_NODES) for statement in between
    ):
        return None
    exit_index = next(
        (position for position in range(use_index - 1, index, -1) if _contains_exit(statements[position])), None
    )
    if exit_index is not None:
        if not _may_reorder(value, statements[index + 1 : exit_index + 1], definitely_bound, facts, options):
            return None
        return Finding(
            target,
            f"`{name}` is assigned before an early exit that does not use it; "
            f"move the assignment below line {statements[exit_index].end_lineno}",
        )
    use = statements[use_index]
    distance = _unrelated_statement_count(between, use, name)
    if distance <= options.max_distance or not _may_reorder(value, between, definitely_bound, facts, options):
        return None
    use_line = min(node.lineno for node in ast.walk(use) if isinstance(node, ast.Name) and node.id == name)
    return Finding(
        target,
        f"`{name}` is assigned {distance} unrelated statements before its first use; "
        f"move the assignment closer to line {use_line}",
    )


def _assigned_value(statement: ast.stmt) -> ast.expr:
    assert isinstance(statement, ast.Assign | ast.AnnAssign)
    assert statement.value is not None
    return statement.value


def _reads_before_rebinding(statement: ast.stmt, name: str) -> bool:
    augmented = {id(node.target) for node in ast.walk(statement) if isinstance(node, ast.AugAssign)}
    return not any(name in iter_binding_names(node) for node in ast.walk(statement) if id(node) not in augmented)


def _may_reorder(
    value: ast.expr,
    window: list[ast.stmt],
    definitely_bound: frozenset[str],
    facts: _FunctionFacts,
    options: _Options,
) -> bool:
    return may_reorder(
        value,
        window,
        options.level,
        stable_locals=facts.stable_locals & definitely_bound,
        builtin_names=options.builtin_names,
    )


def _contains_exit(node: ast.AST, *, in_loop: bool = False, in_guarded_try: bool = False) -> bool:
    if isinstance(node, ast.Return):
        return True
    if isinstance(node, ast.Raise):
        return not in_guarded_try
    if isinstance(node, ast.Continue | ast.Break):
        return not in_loop
    if isinstance(node, (*_NESTED_SCOPE_NODES, ast.expr)):
        return False
    if isinstance(node, _LOOP_NODES):
        return any(_contains_exit(child, in_loop=True, in_guarded_try=in_guarded_try) for child in node.body) or any(
            _contains_exit(child, in_loop=in_loop, in_guarded_try=in_guarded_try) for child in node.orelse
        )
    if isinstance(node, _TRY_NODES):
        guarded = in_guarded_try or bool(node.handlers)
        return any(_contains_exit(child, in_loop=in_loop, in_guarded_try=guarded) for child in node.body) or any(
            _contains_exit(child, in_loop=in_loop, in_guarded_try=in_guarded_try)
            for child in (*node.handlers, *node.orelse, *node.finalbody)
        )
    return any(
        _contains_exit(child, in_loop=in_loop, in_guarded_try=in_guarded_try) for child in ast.iter_child_nodes(node)
    )


def _unrelated_statement_count(between: list[ast.stmt], use: ast.stmt, name: str) -> int:
    use_inputs = (
        {node.id for node in ast.walk(use) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        - _UBIQUITOUS_RECEIVERS
        - {name}
    )
    return sum(
        _statement_count(statement) for statement in between if use_inputs.isdisjoint(exposed_names((statement,)))
    )


def _statement_count(statement: ast.stmt) -> int:
    if isinstance(statement, _NESTED_SCOPE_NODES):
        return 1
    return 1 + sum(_statement_count(child) for block in _child_blocks(statement) for child in block)
