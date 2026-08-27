from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from pre_commit_hooks.ast_checks._scope import iter_binding_names, iter_within_scope

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .candidates import Candidate


class ProofLevel(Enum):
    CONSERVATIVE = auto()
    AGGRESSIVE = auto()


@dataclass(frozen=True, slots=True)
class LocalProof:
    candidate: Candidate
    reason: str


@dataclass(slots=True)
class _State:
    aliases: dict[str, str] = field(default_factory=dict)
    dictionaries: dict[str, set[str]] = field(default_factory=dict)
    collections: dict[str, frozenset[str]] = field(default_factory=dict)
    relations: set[tuple[str, str]] = field(default_factory=set)
    present: dict[str, set[str]] = field(default_factory=dict)

    def copy(self) -> _State:
        return _State(
            self.aliases.copy(),
            {key: value.copy() for key, value in self.dictionaries.items()},
            self.collections.copy(),
            self.relations.copy(),
            {key: value.copy() for key, value in self.present.items()},
        )

    def root(self, name: str) -> str | None:
        return self.aliases.get(name)

    def clear(self) -> None:
        self.aliases.clear()
        self.dictionaries.clear()
        self.collections.clear()
        self.relations.clear()
        self.present.clear()

    def drop(self, name: str) -> None:
        root = self.aliases.pop(name, None)
        for keys in self.present.values():
            keys.discard(name)
        if root is None or root in self.aliases.values():
            return
        self.dictionaries.pop(root, None)
        self.collections.pop(root, None)
        self.present.pop(root, None)
        self.relations = {relation for relation in self.relations if root not in relation}

    def bind_dict(self, name: str, keys: set[str]) -> None:
        self.drop(name)
        self.aliases[name] = self.new_root(name)
        self.dictionaries[self.aliases[name]] = keys

    def bind_collection(self, name: str, keys: frozenset[str]) -> None:
        self.drop(name)
        self.aliases[name] = self.new_root(name)
        self.collections[self.aliases[name]] = keys

    def new_root(self, name: str) -> str:
        root = name
        suffix = 0
        while root in self.dictionaries or root in self.collections:
            suffix += 1
            root = f"{name}:{suffix}"
        return root

    def bind_alias(self, name: str, source: str) -> bool:
        root = self.root(source)
        if root is None:
            return False
        self.drop(name)
        self.aliases[name] = root
        return True

    def add_present(self, dictionary: str, variable_name: str) -> None:
        if (root := self.root(dictionary)) in self.dictionaries:
            self.present.setdefault(root, set()).add(variable_name)

    def add_relation(self, collection: str, dictionary: str) -> None:
        collection_root = self.root(collection)
        dictionary_root = self.root(dictionary)
        if collection_root in self.collections and dictionary_root in self.dictionaries:
            self.relations.add((collection_root, dictionary_root))

    def contains(self, dictionary: str, key: str, *, literal: bool) -> bool:
        root = self.root(dictionary)
        if root not in self.dictionaries:
            return False
        return key in (self.dictionaries[root] if literal else self.present.get(root, set()))

    def add_collection_membership(self, collection: str, key: str) -> None:
        collection_root = self.root(collection)
        if collection_root is None:
            return
        for related_collection, dictionary in self.relations:
            if related_collection == collection_root:
                self.present.setdefault(dictionary, set()).add(key)


def find_proofs(
    tree: ast.Module, candidates: Iterable[Candidate], *, level: ProofLevel = ProofLevel.CONSERVATIVE
) -> list[LocalProof]:
    analyzer = _Analyzer(tree, {id(candidate.call): candidate for candidate in candidates}, level)
    analyzer.visit_scope(tree.body, _State())
    return analyzer.proofs


class _Analyzer:
    __slots__ = (
        "_builtin_all_available",
        "_builtin_dict_available",
        "_candidates",
        "_level",
        "_proof_ids",
        "_typed_dicts",
        "proofs",
    )

    def __init__(self, tree: ast.Module, candidates: dict[int, Candidate], level: ProofLevel) -> None:
        self._candidates = candidates
        self._level = level
        self._builtin_all_available = not _binds_name(tree, "all")
        self._builtin_dict_available = not _binds_name(tree, "dict")
        self._proof_ids: set[int] = set()
        self._typed_dicts = _typed_dicts(tree)
        self.proofs: list[LocalProof] = []

    def visit_scope(self, statements: list[ast.stmt], state: _State) -> _State | None:
        for statement in statements:
            next_state = self.visit_statement(statement, state)
            if next_state is None:
                return None
            state = next_state
        return state

    def visit_statement(self, statement: ast.stmt, state: _State) -> _State | None:
        compound = (
            ast.If
            | ast.For
            | ast.AsyncFor
            | ast.While
            | ast.Try
            | ast.TryStar
            | ast.With
            | ast.AsyncWith
            | ast.Match
            | ast.FunctionDef
            | ast.AsyncFunctionDef
            | ast.ClassDef
        )
        if (
            not isinstance(statement, compound)
            and not _has_unknown_call(statement, self._candidates)
            and not _contains_named_expression(statement)
        ):
            self.report_candidates(statement, state)
        if isinstance(statement, ast.If):
            return self.visit_if(statement, state)
        if isinstance(statement, ast.For | ast.AsyncFor):
            return self.visit_for(statement, state)
        if isinstance(statement, ast.While):
            self.visit_scope(statement.body, _State())
            self.visit_scope(statement.orelse, _State())
            state.clear()
            return state
        if isinstance(statement, ast.Try | ast.TryStar):
            return self.visit_try(statement, state)
        if isinstance(statement, ast.With | ast.AsyncWith):
            state.clear()
            self.visit_scope(statement.body, state)
            state.clear()
            return state
        if isinstance(statement, ast.Match):
            return self.visit_match(statement, state)
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            self.visit_scope(statement.body, self.function_state(statement))
            state.clear()
            return state
        if isinstance(statement, ast.ClassDef):
            self.visit_scope(statement.body, _State())
            state.clear()
            return state
        if isinstance(statement, ast.Return | ast.Raise | ast.Break | ast.Continue):
            return None
        if isinstance(statement, ast.Assign | ast.AnnAssign):
            self.bind_assignment(statement, state)
            return state
        if isinstance(statement, ast.AugAssign | ast.Delete):
            self.invalidate_mutation(statement, state)
            return state
        if isinstance(statement, ast.Import | ast.ImportFrom):
            if any(alias.name == "*" for alias in statement.names):
                state.clear()
            else:
                for name in iter_binding_names(statement):
                    state.drop(name)
            return state
        if _contains_named_expression(statement):
            state.clear()
            return state
        if _contains_suspension(statement) or _has_unknown_call(statement, self._candidates):
            state.clear()
        return state

    def visit_if(self, statement: ast.If, state: _State) -> _State | None:
        true_state, false_state = self.condition_states(statement.test, state)
        return _join(
            [
                self.visit_scope(statement.body, true_state),
                self.visit_scope(statement.orelse, false_state) if statement.orelse else false_state,
            ]
        )

    def visit_for(self, statement: ast.For | ast.AsyncFor, state: _State) -> _State:
        entry_state = state.copy()
        if _has_unknown_call(statement.iter, self._candidates) or _contains_suspension(statement.iter):
            entry_state.clear()
        body_state = _State(
            entry_state.aliases.copy(),
            {root: set() for root in entry_state.dictionaries},
            entry_state.collections.copy(),
            entry_state.relations.copy(),
        )
        for target in _target_names(statement.target):
            body_state.drop(target)
        if _loop_body_invalidates_state(statement.body, entry_state, self._candidates):
            body_state.clear()
        if (
            isinstance(statement.iter, ast.Name)
            and isinstance(statement.target, ast.Name)
            and body_state.root(statement.iter.id) in body_state.collections
        ):
            body_state.add_collection_membership(statement.iter.id, statement.target.id)
        else:
            body_state.clear()
        normal_exit = self.visit_scope(statement.body, body_state)
        if _contains_loop_control(statement.body):
            if statement.orelse:
                self.visit_scope(statement.orelse, _State())
            return _State()
        merged = _join([entry_state, normal_exit]) or _State()
        if statement.orelse:
            return self.visit_scope(statement.orelse, merged) or _State()
        return merged

    def visit_match(self, statement: ast.Match, state: _State) -> _State | None:
        fallthrough = state.copy()
        if _has_unknown_call(statement.subject, self._candidates) or _contains_suspension(statement.subject):
            fallthrough.clear()
        paths: list[_State | None] = []
        for case in statement.cases:
            bound_names = _pattern_names(case.pattern)
            matched = fallthrough.copy()
            for name in bound_names:
                matched.drop(name)
            if case.guard is not None:
                matched, guard_fallthrough = self.condition_states(case.guard, matched)
                fallthrough = guard_fallthrough
            elif _is_irrefutable(case.pattern):
                paths.append(self.visit_scope(case.body, matched))
                return _join(paths)
            else:
                for name in bound_names:
                    fallthrough.drop(name)
            paths.append(self.visit_scope(case.body, matched))
        paths.append(fallthrough)
        return _join(paths)

    def visit_try(self, statement: ast.Try | ast.TryStar, state: _State) -> _State | None:
        body_state = self.visit_scope(statement.body, state.copy())
        paths = [body_state]
        paths.extend(self.visit_scope(handler.body, _State()) for handler in statement.handlers)
        if statement.orelse:
            paths.append(self.visit_scope(statement.orelse, body_state.copy()) if body_state is not None else None)
        merged = _join(paths)
        if merged is None:
            return self.visit_scope(statement.finalbody, _State()) if statement.finalbody else None
        return self.visit_scope(statement.finalbody, merged) if statement.finalbody else merged

    def condition_states(self, test: ast.expr, state: _State) -> tuple[_State, _State]:
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            false_state, true_state = self.condition_states(test.operand, state)
            return true_state, false_state
        if isinstance(test, ast.BoolOp):
            if isinstance(test.op, ast.And):
                true_state = state.copy()
                false_paths: list[_State | None] = []
                for value in test.values:
                    true_state, false_state = self.condition_states(value, true_state)
                    false_paths.append(false_state)
                return true_state, _join(false_paths) or _State()
            false_state = state.copy()
            true_paths: list[_State | None] = []
            for value in test.values:
                true_state, false_state = self.condition_states(value, false_state)
                true_paths.append(true_state)
            return _join(true_paths) or _State(), false_state
        true_state = state.copy()
        false_state = state.copy()
        if (membership := _membership(test)) is not None:
            container, key, positive = membership
            present_state = true_state if positive else false_state
            present_state.add_present(container, key)
            present_state.add_collection_membership(container, key)
            return true_state, false_state
        if (relation := _relation(test)) is not None:
            true_state.add_relation(*relation)
            return true_state, false_state
        if self._builtin_all_available and (relation := _all_relation(test)) is not None:
            true_state.add_relation(*relation)
            return true_state, false_state
        if _has_unknown_call(test, self._candidates) or not isinstance(test, ast.Constant):
            true_state.clear()
            false_state.clear()
        return true_state, false_state

    def bind_assignment(self, statement: ast.Assign | ast.AnnAssign, state: _State) -> None:
        targets = _assignment_targets(statement)
        value = statement.value
        mutations = _mutation_targets(statement.targets if isinstance(statement, ast.Assign) else [statement.target])
        if mutations:
            state.clear()
            return
        if len(targets) != 1 or value is None:
            for target in targets:
                state.drop(target)
            if not targets:
                state.clear()
            return
        target = targets[0]
        if any(isinstance(node, ast.NamedExpr) for node in ast.walk(value)):
            state.clear()
            return
        if isinstance(value, ast.Name) and state.bind_alias(target, value.id):
            return
        if (keys := _literal_dict_keys(value)) is not None:
            state.bind_dict(target, keys)
            return
        if (collection_keys := _literal_collection_keys(value)) is not None:
            state.bind_collection(target, collection_keys)
            return
        state.drop(target)
        if _has_unknown_call(value, self._candidates) or _contains_suspension(value):
            state.clear()

    def invalidate_mutation(self, statement: ast.AugAssign | ast.Delete, state: _State) -> None:
        if _mutation_targets(
            statement.targets if isinstance(statement, ast.Delete) else [statement.target]
        ) or isinstance(statement, ast.AugAssign):
            state.clear()
        else:
            for target in statement.targets:
                for name in _target_names(target):
                    state.drop(name)
        if _has_unknown_call(statement, self._candidates) or _contains_suspension(statement):
            state.clear()

    def function_state(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> _State:
        state = _State()
        for argument in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]:
            annotation_name = _annotation_name(argument.annotation)
            if annotation_name is not None and (keys := self._typed_dicts.get(annotation_name)) is not None:
                state.bind_dict(argument.arg, set(keys))
            elif (
                self._level is ProofLevel.AGGRESSIVE
                and self._builtin_dict_available
                and _is_dict_annotation(argument.annotation)
            ):
                state.bind_dict(argument.arg, set())
        return state

    def report_candidates(self, node: ast.AST, state: _State) -> None:
        for call in iter_within_scope(node):
            candidate = self._candidates.get(id(call))
            if candidate is None or id(call) in self._proof_ids:
                continue
            key = candidate.literal_key if candidate.literal_key is not None else candidate.name_key
            assert key is not None
            if state.contains(candidate.receiver, key, literal=candidate.literal_key is not None):
                self._proof_ids.add(id(call))
                reason = (
                    "key is present in a known dict"
                    if candidate.literal_key is not None
                    else "key is present on this control-flow path"
                )
                self.proofs.append(LocalProof(candidate, reason))


def _join(paths: list[_State | None]) -> _State | None:
    available = [path for path in paths if path is not None]
    if not available:
        return None
    aliases = {
        name: root
        for name, root in available[0].aliases.items()
        if all(path.aliases.get(name) == root for path in available[1:])
    }
    roots = set(aliases.values())
    dictionaries = {
        root: set.intersection(*(path.dictionaries[root] for path in available))
        for root in roots
        if all(root in path.dictionaries for path in available)
    }
    collections = {
        root: available[0].collections[root]
        for root in roots
        if root in available[0].collections
        and all(path.collections.get(root) == available[0].collections.get(root) for path in available)
    }
    return _State(
        aliases,
        dictionaries,
        collections,
        set.intersection(*(path.relations for path in available)),
        {root: set.intersection(*(path.present.get(root, set()) for path in available)) for root in dictionaries},
    )


def _membership(test: ast.expr) -> tuple[str, str, bool] | None:
    if (
        not isinstance(test, ast.Compare)
        or len(test.ops) != 1
        or len(test.comparators) != 1
        or not isinstance(test.left, ast.Name)
        or not isinstance(test.comparators[0], ast.Name)
    ):
        return None
    if isinstance(test.ops[0], ast.In):
        return test.comparators[0].id, test.left.id, True
    if isinstance(test.ops[0], ast.NotIn):
        return test.comparators[0].id, test.left.id, False
    return None


def _relation(test: ast.expr) -> tuple[str, str] | None:
    if (
        not isinstance(test, ast.Compare)
        or len(test.ops) != 1
        or not isinstance(test.ops[0], ast.LtE)
        or not isinstance(test.left, ast.Name)
        or len(test.comparators) != 1
    ):
        return None
    right = test.comparators[0]
    if (
        not isinstance(right, ast.Call)
        or right.args
        or right.keywords
        or not isinstance(right.func, ast.Attribute)
        or right.func.attr != "keys"
        or not isinstance(right.func.value, ast.Name)
    ):
        return None
    return test.left.id, right.func.value.id


def _all_relation(test: ast.expr) -> tuple[str, str] | None:
    if (
        not isinstance(test, ast.Call)
        or test.keywords
        or len(test.args) != 1
        or not isinstance(test.func, ast.Name)
        or test.func.id != "all"
        or not isinstance(test.args[0], ast.GeneratorExp)
    ):
        return None
    generator = test.args[0]
    if len(generator.generators) != 1 or generator.generators[0].ifs:
        return None
    clause = generator.generators[0]
    if not isinstance(clause.target, ast.Name) or not isinstance(clause.iter, ast.Name):
        return None
    membership = _membership(generator.elt)
    if membership is None or not membership[2] or membership[1] != clause.target.id:
        return None
    return clause.iter.id, membership[0]


def _literal_dict_keys(value: ast.expr) -> set[str] | None:
    if not isinstance(value, ast.Dict) or any(key is None for key in value.keys):
        return None
    keys = {key.value for key in value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    return keys if len(keys) == len(value.keys) else None


def _literal_collection_keys(value: ast.expr) -> frozenset[str] | None:
    if not isinstance(value, ast.Set | ast.List | ast.Tuple):
        return None
    keys = frozenset(
        element.value for element in value.elts if isinstance(element, ast.Constant) and isinstance(element.value, str)
    )
    return keys if len(keys) == len(value.elts) else None


def _typed_dicts(tree: ast.Module) -> dict[str, frozenset[str]]:
    typing_names = _typing_names(tree)
    typed_dicts: dict[str, frozenset[str]] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef) or not any(
            _is_typing_name(base, "TypedDict", typing_names) for base in statement.bases
        ):
            continue
        total_values = [keyword.value for keyword in statement.keywords if keyword.arg == "total"]
        if total_values and not all(
            isinstance(value, ast.Constant) and isinstance(value.value, bool) for value in total_values
        ):
            continue
        total = not any(isinstance(value, ast.Constant) and value.value is False for value in total_values)
        typed_dicts[statement.name] = frozenset(
            item.target.id
            for item in statement.body
            if isinstance(item, ast.AnnAssign)
            and isinstance(item.target, ast.Name)
            and (
                (total and not _is_typing_name(item.annotation, "NotRequired", typing_names))
                or _is_typing_name(item.annotation, "Required", typing_names)
            )
        )
    return typed_dicts


def _typing_names(tree: ast.Module) -> dict[str, str]:
    names: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for imported in statement.names:
                if imported.name in {"typing", "typing_extensions"}:
                    names[imported.asname or imported.name] = "module"
        elif isinstance(statement, ast.ImportFrom) and statement.module in {"typing", "typing_extensions"}:
            for imported in statement.names:
                if imported.name in {"Required", "NotRequired", "TypedDict"}:
                    names[imported.asname or imported.name] = imported.name
    return names


def _is_typing_name(annotation: ast.expr | None, expected: str, names: dict[str, str]) -> bool:
    if isinstance(annotation, ast.Subscript):
        annotation = annotation.value
    if isinstance(annotation, ast.Name):
        return names.get(annotation.id) == expected
    return (
        isinstance(annotation, ast.Attribute)
        and isinstance(annotation.value, ast.Name)
        and names.get(annotation.value.id) == "module"
        and annotation.attr == expected
    )


def _annotation_name(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    return None


def _is_dict_annotation(annotation: ast.expr | None) -> bool:
    return (isinstance(annotation, ast.Name) and annotation.id == "dict") or (
        isinstance(annotation, ast.Subscript)
        and isinstance(annotation.value, ast.Name)
        and annotation.value.id == "dict"
    )


def _assignment_targets(statement: ast.Assign | ast.AnnAssign) -> list[str]:
    return [
        name
        for target in (statement.targets if isinstance(statement, ast.Assign) else [statement.target])
        for name in _target_names(target)
    ]


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _mutation_targets(targets: Iterable[ast.expr]) -> set[str]:
    return {
        target.value.id
        for target in targets
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
    }


def _has_unknown_call(node: ast.AST, candidates: dict[int, Candidate]) -> bool:
    return any(isinstance(child, ast.Call) and id(child) not in candidates for child in ast.walk(node))


def _contains_named_expression(node: ast.AST) -> bool:
    return any(isinstance(child, ast.NamedExpr) for child in iter_within_scope(node))


def _contains_suspension(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Await | ast.Yield | ast.YieldFrom) for child in ast.walk(node))


def _loop_body_invalidates_state(statements: list[ast.stmt], state: _State, candidates: dict[int, Candidate]) -> bool:
    tracked_names = state.aliases.keys()
    if not tracked_names:
        return False
    for statement in statements:
        for node in [statement, *iter_within_scope(statement)]:
            if isinstance(node, ast.Call) and id(node) not in candidates:
                return True
            if isinstance(node, ast.Await | ast.Yield | ast.YieldFrom):
                return True
            if isinstance(node, ast.AugAssign):
                return True
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del) and node.id in tracked_names:
                return True
            if isinstance(node, ast.Import | ast.ImportFrom) and any(
                name in tracked_names for name in iter_binding_names(node)
            ):
                return True
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name in tracked_names:
                return True
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store | ast.Del)
                and isinstance(node.value, ast.Name)
                and node.value.id in tracked_names
            ):
                return True
    return False


def _contains_loop_control(statements: list[ast.stmt]) -> bool:
    return any(
        isinstance(node, ast.Break | ast.Continue)
        for statement in statements
        for node in [statement, *iter_within_scope(statement)]
    )


def _pattern_names(pattern: ast.pattern) -> set[str]:
    return {name for node in ast.walk(pattern) for name in iter_binding_names(node)}


def _is_irrefutable(pattern: ast.pattern) -> bool:
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _is_irrefutable(pattern.pattern)
    return isinstance(pattern, ast.MatchOr) and any(_is_irrefutable(item) for item in pattern.patterns)


def _binds_name(tree: ast.Module, name: str) -> bool:
    return any(name in iter_binding_names(node) for node in ast.walk(tree))
