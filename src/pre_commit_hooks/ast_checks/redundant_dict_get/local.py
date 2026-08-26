from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pre_commit_hooks.ast_checks._scope import iter_binding_names, iter_within_scope

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .candidates import Candidate


@dataclass(frozen=True, slots=True)
class LocalProof:
    candidate: Candidate
    reason: str


def find_proofs(tree: ast.Module, candidates: Iterable[Candidate]) -> list[LocalProof]:
    candidate_by_call = {id(candidate.call): candidate for candidate in candidates}
    proofs: list[LocalProof] = []
    _visit_scope(tree.body, {}, candidate_by_call, proofs, builtin_dict_available=not _binds_dict(tree))
    return proofs


def _visit_scope(
    statements: list[ast.stmt],
    facts: dict[str, set[str]],
    candidate_by_call: dict[int, Candidate],
    proofs: list[LocalProof],
    *,
    builtin_dict_available: bool,
) -> None:
    for statement in statements:
        if isinstance(statement, ast.If):
            _visit_if(statement, facts, candidate_by_call, proofs, builtin_dict_available=builtin_dict_available)
            continue
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            facts.clear()
            _visit_scope(
                statement.body,
                _dict_parameter_facts(statement, builtin_dict_available=builtin_dict_available),
                candidate_by_call,
                proofs,
                builtin_dict_available=builtin_dict_available,
            )
            continue
        if isinstance(statement, ast.ClassDef):
            facts.clear()
            _visit_scope(statement.body, {}, candidate_by_call, proofs, builtin_dict_available=builtin_dict_available)
            continue
        if isinstance(
            statement, ast.For | ast.AsyncFor | ast.While | ast.Try | ast.TryStar | ast.With | ast.AsyncWith | ast.Match
        ):
            _visit_compound_statement(
                statement, candidate_by_call, proofs, builtin_dict_available=builtin_dict_available
            )
            facts.clear()
            continue
        _visit_statement(statement, facts, candidate_by_call, proofs)


def _visit_if(
    statement: ast.If,
    facts: dict[str, set[str]],
    candidate_by_call: dict[int, Candidate],
    proofs: list[LocalProof],
    *,
    builtin_dict_available: bool,
) -> None:
    membership = _membership(statement.test)
    if membership is None:
        facts.clear()
        _visit_scope(statement.body, {}, candidate_by_call, proofs, builtin_dict_available=builtin_dict_available)
        _visit_scope(statement.orelse, {}, candidate_by_call, proofs, builtin_dict_available=builtin_dict_available)
        return
    body_facts = _copy_facts(facts)
    else_facts = _copy_facts(facts)
    post_guard_facts: set[str] | None = None
    receiver, key, positive = membership
    if positive and _receiver_marker() in facts.get(receiver, set()):
        body_facts.setdefault(receiver, set()).add(_flow_key(key))
    elif not statement.orelse and _receiver_marker() in facts.get(receiver, set()) and _terminates(statement.body):
        post_guard_facts = {*facts[receiver], _flow_key(key)}
    _visit_scope(statement.body, body_facts, candidate_by_call, proofs, builtin_dict_available=builtin_dict_available)
    _visit_scope(statement.orelse, else_facts, candidate_by_call, proofs, builtin_dict_available=builtin_dict_available)
    facts.clear()
    if post_guard_facts is not None:
        facts[receiver] = post_guard_facts


def _visit_statement(
    statement: ast.stmt,
    facts: dict[str, set[str]],
    candidate_by_call: dict[int, Candidate],
    proofs: list[LocalProof],
) -> None:
    _invalidate_statement_bindings(statement, facts)
    _invalidate_escaped_facts(statement, facts, candidate_by_call)
    for call in iter_within_scope(statement):
        candidate = candidate_by_call.get(id(call))
        if (
            candidate is not None
            and _receiver_marker() in facts.get(candidate.receiver, set())
            and _candidate_fact(candidate) in facts.get(candidate.receiver, set())
        ):
            reason = (
                "key is present in this dict literal"
                if candidate.literal_key is not None
                else "key is present on this control-flow path"
            )
            proofs.append(LocalProof(candidate, reason))
    if isinstance(statement, ast.Assign | ast.AnnAssign):
        target_names = _target_names(statement)
        for name in target_names:
            _drop_name(facts, name)
        for name in _mutation_targets(statement):
            _drop_name(facts, name)
        value = statement.value
        if len(target_names) == 1 and (keys := _literal_dict_keys(value)) is not None:
            facts[target_names[0]] = {_receiver_marker(), *keys}
        else:
            _invalidate_assigned_value_facts(value, facts)
        return
    if isinstance(statement, ast.AugAssign | ast.Delete):
        for target in _mutation_targets(statement):
            _drop_name(facts, target)


def _membership(test: ast.expr) -> tuple[str, str, bool] | None:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return None
    if not isinstance(test.left, ast.Name) or not isinstance(test.comparators[0], ast.Name):
        return None
    if isinstance(test.ops[0], ast.In):
        return test.comparators[0].id, test.left.id, True
    if isinstance(test.ops[0], ast.NotIn):
        return test.comparators[0].id, test.left.id, False
    return None


def _terminates(statements: list[ast.stmt]) -> bool:
    return bool(statements) and isinstance(statements[-1], ast.Return | ast.Raise)


def _literal_dict_keys(value: ast.expr | None) -> set[str] | None:
    if not isinstance(value, ast.Dict) or any(key is None for key in value.keys):
        return None
    keys = {
        _literal_key(key.value) for key in value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    return keys if len(keys) == len(value.keys) else None


def _target_names(statement: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    return [name for target in targets for name in _target_names_from(target)]


def _invalidate_escaped_facts(
    statement: ast.stmt,
    facts: dict[str, set[str]],
    candidate_by_call: dict[int, Candidate],
) -> None:
    for call in ast.walk(statement):
        if isinstance(call, ast.Call) and id(call) not in candidate_by_call:
            facts.clear()
            return
    for node in ast.walk(statement):
        if isinstance(node, ast.NamedExpr | ast.Yield | ast.YieldFrom):
            _invalidate_assigned_value_facts(node.value, facts)


def _invalidate_assigned_value_facts(value: ast.expr | None, facts: dict[str, set[str]]) -> None:
    if value is None:
        return
    for name in ast.walk(value):
        if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load):
            _drop_name(facts, name.id)


def _invalidate_statement_bindings(statement: ast.stmt, facts: dict[str, set[str]]) -> None:
    for node in iter_within_scope(statement):
        for name in iter_binding_names(node):
            _drop_name(facts, name)


def _copy_facts(facts: dict[str, set[str]]) -> dict[str, set[str]]:
    return {name: keys.copy() for name, keys in facts.items()}


def _visit_compound_statement(
    statement: ast.For | ast.AsyncFor | ast.While | ast.Try | ast.TryStar | ast.With | ast.AsyncWith | ast.Match,
    candidate_by_call: dict[int, Candidate],
    proofs: list[LocalProof],
    *,
    builtin_dict_available: bool,
) -> None:
    bodies = [case.body for case in statement.cases] if isinstance(statement, ast.Match) else [statement.body]
    if isinstance(statement, ast.For | ast.AsyncFor | ast.While | ast.Try | ast.TryStar):
        bodies.append(statement.orelse)
    if isinstance(statement, ast.Try | ast.TryStar):
        bodies.extend(handler.body for handler in statement.handlers)
        bodies.append(statement.finalbody)
    for body in bodies:
        _visit_scope(body, {}, candidate_by_call, proofs, builtin_dict_available=builtin_dict_available)


def _literal_key(key: str) -> str:
    return f"literal:{key}"


def _receiver_marker() -> str:
    return "receiver"


def _flow_key(key: str) -> str:
    return f"flow:{key}"


def _candidate_fact(candidate: Candidate) -> str:
    if candidate.literal_key is not None:
        return _literal_key(candidate.literal_key)
    assert candidate.name_key is not None
    return _flow_key(candidate.name_key)


def _drop_name(facts: dict[str, set[str]], name: str) -> None:
    facts.pop(name, None)
    for keys in facts.values():
        keys.discard(_flow_key(name))


def _mutation_targets(statement: ast.Assign | ast.AnnAssign | ast.AugAssign | ast.Delete) -> set[str]:
    targets = statement.targets if isinstance(statement, ast.Assign | ast.Delete) else [statement.target]
    return {
        target.value.id
        for target in targets
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
    }


def _target_names_from(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names_from(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _target_names_from(element)]
    return []


def _dict_parameter_facts(
    function: ast.FunctionDef | ast.AsyncFunctionDef, *, builtin_dict_available: bool
) -> dict[str, set[str]]:
    if not builtin_dict_available:
        return {}
    arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    return {
        argument.arg: {_receiver_marker()} for argument in arguments if _is_builtin_dict_annotation(argument.annotation)
    }


def _is_builtin_dict_annotation(annotation: ast.expr | None) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "dict"
    if not isinstance(annotation, ast.Subscript) or not isinstance(annotation.value, ast.Name):
        return False
    return annotation.value.id == "dict"


def _binds_dict(tree: ast.Module) -> bool:
    return any(
        name == "dict"
        for node in ast.walk(tree)
        for name in (
            iter_binding_names(node)
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            else _definition_binding_names(node)
        )
    )


def _definition_binding_names(
    definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[str, ...]:
    arguments = (
        [
            *definition.args.posonlyargs,
            *definition.args.args,
            *definition.args.kwonlyargs,
            *([definition.args.vararg] if definition.args.vararg is not None else []),
            *([definition.args.kwarg] if definition.args.kwarg is not None else []),
        ]
        if isinstance(definition, ast.FunctionDef | ast.AsyncFunctionDef)
        else []
    )
    return (
        definition.name,
        *(argument.arg for argument in arguments),
        *(
            type_parameter.name
            for type_parameter in definition.type_params
            if isinstance(type_parameter, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
        ),
    )
