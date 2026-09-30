from __future__ import annotations

import ast
import builtins
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pre_commit_hooks.ast_checks._scope import iter_binding_names, iter_within_scope

from .reorder import DefinedFarFromUseLevel, StatementEffects, may_reorder, statement_effects

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Set as AbstractSet

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


@dataclass(frozen=True, slots=True)
class _FunctionFacts:
    occurrences: Counter[str]
    captured: frozenset[str]
    stable_locals: frozenset[str]
    parameters: frozenset[str]
    redeclared: frozenset[str]
    builtin_names: frozenset[str]


class _Block:
    __slots__ = ("_builtin_names", "_effects", "exits", "name_positions", "statements", "terminals")

    def __init__(self, statements: list[ast.stmt], builtin_names: frozenset[str]) -> None:
        self.statements = statements
        self._builtin_names = builtin_names
        self.name_positions: dict[str, list[int]] = {}
        for position, statement in enumerate(statements):
            for node in ast.walk(statement):
                if isinstance(node, ast.Name):
                    self.name_positions.setdefault(node.id, []).append(position)
        self.exits = [
            position for position, statement in enumerate(statements) if _contains_exit(statement, builtin_names)
        ]
        self.terminals = [
            position for position, statement in enumerate(statements) if isinstance(statement, _TERMINAL_NODES)
        ]
        self._effects: dict[int, tuple[StatementEffects, int]] = {}

    def effects(self, start: int, stop: int) -> list[tuple[StatementEffects, int]]:
        return [self._statement_effects(position) for position in range(start, stop)]

    def _statement_effects(self, position: int) -> tuple[StatementEffects, int]:
        if position not in self._effects:
            statement = self.statements[position]
            self._effects[position] = (statement_effects(statement, self._builtin_names), _statement_count(statement))
        return self._effects[position]


def find_findings(tree: ast.Module, level: DefinedFarFromUseLevel, max_distance: int) -> list[Finding]:
    module_bound = {name for node in iter_within_scope(tree) for name in iter_binding_names(node)}
    module_bound.update(name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names)
    visible_builtins = frozenset() if _can_rebind_builtins(tree) else frozenset(dir(builtins)) - module_bound
    findings: list[Finding] = []
    _collect_findings(tree, visible_builtins, _Options(level, max_distance), findings)
    return findings


def _can_rebind_builtins(tree: ast.Module) -> bool:
    return any(
        (isinstance(node, ast.Import) and any(alias.name == "builtins" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "builtins")
        or (isinstance(node, ast.Name) and node.id in {"__builtins__", "globals"})
        or (
            isinstance(node, ast.Attribute)
            and node.attr == "modules"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        )
        for node in ast.walk(tree)
    )


def _collect_findings(
    node: ast.AST, visible_builtins: frozenset[str], options: _Options, findings: list[Finding]
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _FUNCTION_NODES):
            child_builtins = (
                visible_builtins
                - _parameters(child)
                - {name for scope_node in iter_within_scope(child) for name in iter_binding_names(scope_node)}
            )
            findings.extend(_function_findings(child, child_builtins, options))
            _collect_findings(child, child_builtins, options, findings)
        else:
            _collect_findings(child, visible_builtins, options, findings)


def _function_findings(
    function: ast.FunctionDef | ast.AsyncFunctionDef, builtin_names: frozenset[str], options: _Options
) -> Iterator[Finding]:
    facts = _function_facts(function, builtin_names)
    if facts is None:
        return
    for statements, bound_before_block in _iter_blocks(function.body, facts.parameters):
        block: _Block | None = None
        bound = set(bound_before_block)
        for index, statement in enumerate(statements):
            target = _assignment_target(statement)
            if target is not None and target.id not in facts.redeclared | facts.captured:
                block = block or _Block(statements, facts.builtin_names)
                finding = _candidate_finding(block, index, target, facts, options, definitely_bound=bound)
                if finding is not None:
                    yield finding
            _update_definitely_bound(bound, statement)


def _function_facts(
    function: ast.FunctionDef | ast.AsyncFunctionDef, builtin_names: frozenset[str]
) -> _FunctionFacts | None:
    occurrences: Counter[str] = Counter()
    redeclared: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Name):
            occurrences[node.id] += 1
        elif isinstance(node, ast.Global | ast.Nonlocal):
            redeclared.update(node.names)
    if any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _DYNAMIC_SCOPE_BUILTINS
        for node in _iter_own_scope(function)
    ):
        return None
    parameters = _parameters(function)
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
        builtin_names,
    )


def _iter_own_scope(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, ast.ClassDef):
            pending.extend((*node.decorator_list, *node.bases, *node.keywords))
        elif isinstance(node, (*_FUNCTION_NODES, ast.Lambda)):
            decorators = node.decorator_list if isinstance(node, _FUNCTION_NODES) else []
            defaults = [default for default in node.args.kw_defaults if default is not None]
            pending.extend((*decorators, *node.args.defaults, *defaults))
        else:
            pending.extend(ast.iter_child_nodes(node))


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    arguments = function.args
    return {
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


def _candidate_finding(
    block: _Block,
    index: int,
    target: ast.Name,
    facts: _FunctionFacts,
    options: _Options,
    *,
    definitely_bound: AbstractSet[str],
) -> Finding | None:
    name = target.id
    statements = block.statements
    positions = block.name_positions[name]
    first_later = bisect_right(positions, index)
    if (
        first_later == len(positions)
        or len(positions) - first_later != facts.occurrences[name] - 1
        or not _reads_before_rebinding(statements[use_index := positions[first_later]], name)
    ):
        return None
    value = _assigned_value(statements[index])
    if any(isinstance(node, _REORDER_SENSITIVE_NODES) for node in ast.walk(value)) or _any_between(
        block.terminals, index, use_index
    ):
        return None
    last_exit = bisect_left(block.exits, use_index) - 1
    if last_exit >= 0 and block.exits[last_exit] > index:
        exit_index = block.exits[last_exit]
        if not _may_reorder(value, block.effects(index + 1, exit_index + 1), definitely_bound, facts, options):
            return None
        return Finding(
            target,
            f"`{name}` is assigned before an early exit that does not use it; "
            f"move the assignment below line {statements[exit_index].end_lineno}",
        )
    use = statements[use_index]
    window = block.effects(index + 1, use_index)
    distance = _unrelated_statement_count(window, use, name)
    if distance <= options.max_distance or not _may_reorder(value, window, definitely_bound, facts, options):
        return None
    use_line = min(node.lineno for node in ast.walk(use) if isinstance(node, ast.Name) and node.id == name)
    return Finding(
        target,
        f"`{name}` is assigned {distance} unrelated statements before its first use; "
        f"move the assignment closer to line {use_line}",
    )


def _any_between(positions: list[int], start: int, stop: int) -> bool:
    candidate = bisect_right(positions, start)
    return candidate < len(positions) and positions[candidate] < stop


def _assigned_value(statement: ast.stmt) -> ast.expr:
    assert isinstance(statement, ast.Assign | ast.AnnAssign)
    assert statement.value is not None
    return statement.value


def _reads_before_rebinding(statement: ast.stmt, name: str) -> bool:
    reading_targets = {id(node.target) for node in ast.walk(statement) if isinstance(node, ast.AugAssign)}
    reading_targets.update(
        id(target)
        for node in ast.walk(statement)
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None and _loads(node.value, name)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    )
    return not any(name in iter_binding_names(node) for node in ast.walk(statement) if id(node) not in reading_targets)


def _loads(expression: ast.expr, name: str) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
        for node in ast.walk(expression)
    )


def _may_reorder(
    value: ast.expr,
    window: list[tuple[StatementEffects, int]],
    definitely_bound: AbstractSet[str],
    facts: _FunctionFacts,
    options: _Options,
) -> bool:
    return may_reorder(
        value,
        [effects for effects, _count in window],
        options.level,
        stable_locals=facts.stable_locals & definitely_bound,
        builtin_names=facts.builtin_names,
        rebindable_by_calls=facts.redeclared,
    )


def _contains_exit(
    node: ast.AST,
    builtin_names: frozenset[str],
    *,
    in_loop: bool = False,
    guards: tuple[list[ast.ExceptHandler], ...] = (),
) -> bool:
    if isinstance(node, ast.Return):
        return True
    if isinstance(node, ast.Raise):
        return _escapes(node, guards, builtin_names)
    if isinstance(node, ast.Continue | ast.Break):
        return not in_loop
    if isinstance(node, (*_NESTED_SCOPE_NODES, ast.expr)):
        return False
    if isinstance(node, _LOOP_NODES):
        return any(_contains_exit(child, builtin_names, in_loop=True, guards=guards) for child in node.body) or any(
            _contains_exit(child, builtin_names, in_loop=in_loop, guards=guards) for child in node.orelse
        )
    if isinstance(node, _TRY_NODES):
        body_guards = (*guards, node.handlers) if node.handlers else guards
        return any(
            _contains_exit(child, builtin_names, in_loop=in_loop, guards=body_guards) for child in node.body
        ) or any(
            _contains_exit(child, builtin_names, in_loop=in_loop, guards=guards)
            for child in (*node.handlers, *node.orelse, *node.finalbody)
        )
    return any(
        _contains_exit(child, builtin_names, in_loop=in_loop, guards=guards) for child in ast.iter_child_nodes(node)
    )


def _escapes(node: ast.Raise, guards: tuple[list[ast.ExceptHandler], ...], builtin_names: frozenset[str]) -> bool:
    if not guards:
        return True
    raised = _builtin_exception(node.exc.func if isinstance(node.exc, ast.Call) else node.exc, builtin_names)
    return raised is not None and not any(
        _may_catch(handler, raised, builtin_names) for handlers in guards for handler in handlers
    )


def _may_catch(handler: ast.ExceptHandler, raised: type[BaseException], builtin_names: frozenset[str]) -> bool:
    if handler.type is None:
        return True
    caught_types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for caught_type in caught_types:
        caught = _builtin_exception(caught_type, builtin_names)
        if caught is None or issubclass(raised, caught):
            return True
    return False


def _builtin_exception(node: ast.expr | None, builtin_names: frozenset[str]) -> type[BaseException] | None:
    if not isinstance(node, ast.Name) or node.id not in builtin_names:
        return None
    candidate = getattr(builtins, node.id, None)
    return candidate if isinstance(candidate, type) and issubclass(candidate, BaseException) else None


def _unrelated_statement_count(window: list[tuple[StatementEffects, int]], use: ast.stmt, name: str) -> int:
    use_inputs = (
        {node.id for node in ast.walk(use) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        - _UBIQUITOUS_RECEIVERS
        - {name}
    )
    return sum(count for effects, count in window if use_inputs.isdisjoint(effects.exposed))


def _statement_count(statement: ast.stmt) -> int:
    if isinstance(statement, _NESTED_SCOPE_NODES):
        return 1
    return 1 + sum(_statement_count(child) for block in _child_blocks(statement) for child in block)
