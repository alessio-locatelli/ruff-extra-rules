from __future__ import annotations

import ast
import builtins
import keyword
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


type TargetKey = tuple[int, int]
type ScopeNode = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef


class Confidence(StrEnum):
    AUTO_FIX = "auto_fix"
    SUGGESTION_ONLY = "suggestion_only"


@dataclass(frozen=True, slots=True)
class RenameProposal:
    name: str
    confidence: Confidence
    evidence: frozenset[str]


@dataclass(slots=True)
class ScopeInfo:
    node: ScopeNode
    parent: ScopeInfo | None
    bindings: dict[str, list[ast.AST]] = field(default_factory=lambda: defaultdict(list))
    imports: dict[str, tuple[str, ...]] = field(default_factory=dict)
    explicit_modules: set[tuple[str, ...]] = field(default_factory=set)
    function_returns: dict[str, ast.expr | None] = field(default_factory=dict)
    candidates: list[Assignment] = field(default_factory=list)
    attributes: dict[str, list[ast.Attribute]] = field(default_factory=lambda: defaultdict(list))
    calls: dict[str, list[CallArgument]] = field(default_factory=lambda: defaultdict(list))
    conditions: dict[str, list[ast.stmt]] = field(default_factory=lambda: defaultdict(list))
    loops: dict[str, list[tuple[str, ast.Name]]] = field(default_factory=lambda: defaultdict(list))
    collection_uses: dict[str, list[ast.expr]] = field(default_factory=lambda: defaultdict(list))
    global_or_nonlocal: set[str] = field(default_factory=set)
    children: list[ScopeInfo] = field(default_factory=list)
    has_reflection: bool = False
    reachable_names: set[str] | None = None
    class_references: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class Assignment:
    target: ast.Name
    value: ast.expr
    annotation: ast.expr | None
    scope: ScopeInfo


@dataclass(frozen=True, slots=True)
class CallArgument:
    call: ast.Call
    position: int | str


class _Index:
    __slots__ = ("root",)

    def __init__(self, tree: ast.Module) -> None:
        self.root = ScopeInfo(tree, None)
        self._build_scope(self.root, tree.body)

    def _build_scope(self, scope: ScopeInfo, body: list[ast.stmt]) -> None:
        if isinstance(scope.node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in _arguments(scope.node.args):
                scope.bindings[arg.arg].append(arg)
            for type_param in scope.node.type_params:
                if isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple):
                    scope.bindings[type_param.name].append(type_param)
        visitor = _ScopeVisitor(self, scope)
        for statement in body:
            visitor.visit(statement)
        scope.has_reflection = visitor.has_reflection or any(child.has_reflection for child in scope.children)

    def add_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        parent: ScopeInfo,
        *,
        register_parent: bool = True,
    ) -> None:
        if register_parent:
            parent.bindings[node.name].append(node)
            parent.function_returns[node.name] = node.returns
        child = ScopeInfo(node, parent)
        parent.children.append(child)
        self._build_scope(child, node.body)

    def add_class(self, node: ast.ClassDef, parent: ScopeInfo) -> None:
        parent.bindings[node.name].append(node)
        parent.class_references.update(
            child.id for child in ast.walk(node) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
        )
        visitor = _ClassVisitor(self, parent)
        for statement in node.body:
            visitor.visit(statement)


class _ScopeVisitor(ast.NodeVisitor):
    def __init__(self, index: _Index, scope: ScopeInfo) -> None:
        self.index = index
        self.scope = scope
        self.has_reflection = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.index.add_function(node, self.scope)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.index.add_function(node, self.scope)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.index.add_class(node, self.scope)

    def visit_Lambda(self, _node: ast.Lambda) -> None:
        return

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._bind_named_expressions(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._bind_named_expressions(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._bind_named_expressions(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._bind_named_expressions(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".")[0]
            self.scope.bindings[bound_name].append(node)
            self.scope.imports[bound_name] = tuple(alias.name.split(".")) if alias.asname else (bound_name,)
            self.scope.explicit_modules.add(tuple(alias.name.split(".")))

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self.scope.candidates.append(Assignment(node.targets[0], node.value, None, self.scope))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self.scope.candidates.append(Assignment(node.target, node.value, node.annotation, self.scope))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        module = tuple(node.module.split("."))
        self.scope.explicit_modules.add(module)
        for alias in node.names:
            if alias.name == "*":
                continue
            bound_name = alias.asname or alias.name
            self.scope.bindings[bound_name].append(node)
            self.scope.imports[bound_name] = (*module, alias.name)
            if module == ("urllib",) and alias.name == "request":
                self.scope.explicit_modules.add((*module, alias.name))

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.global_or_nonlocal.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.global_or_nonlocal.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.scope.bindings[node.name].append(node)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.scope.bindings[node.name].append(node)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.scope.bindings[node.name].append(node)
        self.generic_visit(node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.scope.bindings[node.rest].append(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.scope.bindings[node.id].append(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and isinstance(node.value.ctx, ast.Load):
            self.scope.attributes[node.value.id].append(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_reflection_call(node):
            self.has_reflection = True
        for position, argument in enumerate(node.args):
            if isinstance(argument, ast.Name) and isinstance(argument.ctx, ast.Load):
                self.scope.calls[argument.id].append(CallArgument(node, position))
        for keyword_argument in node.keywords:
            if (
                keyword_argument.arg is not None
                and isinstance(keyword_argument.value, ast.Name)
                and isinstance(keyword_argument.value.ctx, ast.Load)
            ):
                self.scope.calls[keyword_argument.value.id].append(CallArgument(node, keyword_argument.arg))
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._record_condition(node.test, node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._record_condition(node.test, node)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._record_condition(node.test, node)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._record_loop(node.iter, node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._record_loop(node.iter, node.target)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) == 1 and isinstance(node.ops[0], ast.In) and len(node.comparators) == 1:
            comparator = node.comparators[0]
            if isinstance(comparator, ast.Name) and isinstance(comparator.ctx, ast.Load):
                self.scope.collection_uses[comparator.id].append(node)
        self.generic_visit(node)

    def _bind_named_expressions(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                self.scope.bindings[child.target.id].append(child.target)

    def _record_condition(self, expression: ast.expr, node: ast.stmt) -> None:
        name = _condition_name(expression)
        if name is not None:
            self.scope.conditions[name].append(node)

    def _record_loop(self, iterable: ast.expr, target: ast.expr) -> None:
        if isinstance(iterable, ast.Name) and isinstance(iterable.ctx, ast.Load) and isinstance(target, ast.Name):
            self.scope.loops[iterable.id].append((target.id, target))


class _ClassVisitor(ast.NodeVisitor):
    def __init__(self, index: _Index, parent: ScopeInfo) -> None:
        self.index = index
        self.parent = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.index.add_function(node, self.parent, register_parent=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.index.add_function(node, self.parent, register_parent=False)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.index.add_class(node, self.parent)


def plan_suggestions(
    tree: ast.Module,
    forbidden_names: set[str],
    ignored_lines: set[int],
) -> dict[TargetKey, RenameProposal]:
    index = _Index(tree)
    proposals = [
        proposal
        for scope in _iter_scopes(index.root)
        for proposal in _scope_proposals(scope, forbidden_names, ignored_lines)
    ]
    plan = _remove_collisions(proposals, forbidden_names)
    plan.update(_parametrize_result_proposals(index, forbidden_names, ignored_lines))
    plan.update(_verb_parameter_proposals(index, forbidden_names, ignored_lines))
    return plan


@dataclass(frozen=True, slots=True)
class _PlannedProposal:
    assignment: Assignment
    proposal: RenameProposal


def _scope_proposals(
    scope: ScopeInfo,
    forbidden_names: set[str],
    ignored_lines: set[int],
) -> Iterable[_PlannedProposal]:
    if isinstance(scope.node, ast.Module):
        return
    for assignment in scope.candidates:
        if assignment.target.lineno in ignored_lines or not _eligible(assignment):
            continue
        proposal = _proposal_for(assignment, forbidden_names)
        if proposal is not None:
            yield _PlannedProposal(assignment, proposal)


def _proposal_for(assignment: Assignment, forbidden_names: set[str]) -> RenameProposal | None:
    candidates: dict[str, set[str]] = defaultdict(set)
    constraints = _annotation_constraints(assignment.annotation)
    annotation_name = _annotation_name(assignment.annotation)
    if annotation_name is not None:
        candidates[annotation_name].add("annotation")

    for name, evidence in _expression_candidates(assignment.value, assignment.scope).items():
        candidates[name].update(evidence)

    if isinstance(assignment.value, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        comprehension_name = _comprehension_name(assignment.value)
        if comprehension_name is not None:
            candidates[comprehension_name].update({"comprehension_element", "comprehension_iterable"})

    _add_use_candidates(candidates, constraints, assignment)
    _refine_parser_candidates(candidates, constraints)

    if len(candidates) != 1:
        return None

    name, evidence = next(iter(candidates.items()))
    if not _is_valid_name(name, forbidden_names):
        return None
    if name in _reachable_names(assignment.scope):
        return None

    confidence = Confidence.AUTO_FIX if "annotation" in evidence or len(evidence) >= 2 else Confidence.SUGGESTION_ONLY
    if confidence is Confidence.AUTO_FIX and assignment.scope.has_reflection:
        confidence = Confidence.SUGGESTION_ONLY
    return RenameProposal(name, confidence, frozenset(evidence))


def _add_use_candidates(
    candidates: dict[str, set[str]],
    constraints: set[str],
    assignment: Assignment,
) -> None:
    target = assignment.target
    scope = assignment.scope
    name = target.id
    position = _position(target)
    for attribute in scope.attributes[name]:
        if _position(attribute) <= position:
            continue
        role = _attribute_role(attribute.attr)
        if role is not None:
            if role not in candidates and any("registry" in evidence for evidence in candidates.values()):
                continue
            candidates[role].add("access")

    parser_roles: set[str] = set()
    command_shape = _is_command_shape(assignment.value)
    for call_argument in scope.calls[name]:
        if _position(call_argument.call) <= position:
            continue
        qname = _qname(call_argument.call.func, scope)
        if _is_parser_argument(qname, call_argument.position, "json"):
            parser_roles.add("json_text")
        if _is_parser_argument(qname, call_argument.position, "toml"):
            parser_roles.add("toml_text")
        if _is_subprocess_argument(qname, call_argument.position):
            candidates["command"].add("consumer_command")
            if command_shape:
                candidates["command"].add("literal_shape")
        if _is_len_call(call_argument.call, call_argument.position):
            _confirm_collection_candidates(candidates)
        if _is_http_body_argument(qname, call_argument.position):
            candidates["content"].add("consumer_http_body")

    for parser_role in parser_roles:
        if parser_role == "json_text" and "bytes" in constraints:
            continue
        candidates[parser_role].add("parser")
        if "text" in constraints or "text" in candidates:
            candidates[parser_role].add("text_producer")

    predicate_names = {candidate for candidate, evidence in candidates.items() if "predicate" in evidence}
    has_boolean_use = "bool" in constraints or any(_position(node) > position for node in scope.conditions[name])
    if predicate_names and has_boolean_use:
        for predicate_name in predicate_names:
            candidates[predicate_name].add("boolean_use")

    loop_targets = [loop_target for loop_target, loop_node in scope.loops[name] if _position(loop_node) > position]
    if loop_targets:
        for candidate, evidence in candidates.items():
            if _singularize(candidate) in loop_targets:
                evidence.add("iteration")
    if any(_position(node) > position for node in scope.collection_uses[name]):
        _confirm_collection_candidates(candidates)


def _refine_parser_candidates(candidates: dict[str, set[str]], constraints: set[str]) -> None:
    parser_roles = {name for name, evidence in candidates.items() if "parser" in evidence}
    if not parser_roles:
        return
    candidates.pop("text", None)
    if "bytes" in constraints:
        candidates.pop("json_text", None)


def _expression_candidates(value: ast.expr, scope: ScopeInfo) -> dict[str, set[str]]:
    if isinstance(value, ast.Await):
        return _expression_candidates(value.value, scope)
    if isinstance(value, ast.Call):
        return _call_candidates(value, scope)
    if isinstance(value, ast.Attribute) and _is_public_attribute(value.attr):
        return {value.attr: {"attribute"}}
    return {}


def _call_candidates(node: ast.Call, scope: ScopeInfo) -> dict[str, set[str]]:
    if isinstance(node.func, ast.Attribute) and node.func.attr == "json" and isinstance(node.func.value, ast.Call):
        nested = _call_candidates(node.func.value, scope)
        if "response" in nested:
            return {"payload": {"http_json"}}

    qname = _qname(node.func, scope)
    registry_name = _registry_name(qname, node, scope)
    if registry_name is not None:
        return {registry_name: {"registry"}}

    if _is_path_open(node, scope):
        return {"file_handle": {"registry"}}

    name = _call_terminal_name(node.func)
    if name is None:
        return {}

    candidates: dict[str, set[str]] = {}
    deserialized_name = _deserialized_argument_name(qname, node)
    if deserialized_name is not None:
        candidates[deserialized_name] = {"deserializer"}
    for prefix in ("get", "fetch", "load", "read", "parse", "create", "build", "make", "find"):
        marker = f"{prefix}_"
        if name.startswith(marker) and len(name) > len(marker):
            candidates[name.removeprefix(marker)] = {"producer"}
            break
    if name.startswith(("is_", "has_", "can_", "should_")):
        candidates[name] = {"predicate"}

    constructor_name = _constructor_name(node.func)
    if constructor_name is not None:
        candidates[constructor_name] = {"constructor"}

    if isinstance(node.func, ast.Name):
        return_annotation = _function_return_annotation(scope, node.func.id)
        return_name = _annotation_name(return_annotation)
        if return_name is not None:
            candidates.setdefault(return_name, set()).add("return_annotation")
    return candidates


def _deserialized_argument_name(qname: tuple[str, ...] | None, node: ast.Call) -> str | None:
    if qname not in {("json", "loads"), ("tomllib", "loads")}:
        return None
    if not node.args or not isinstance(node.args[0], ast.Name):
        return None
    return f"deserialized_{node.args[0].id}"


def _registry_name(qname: tuple[str, ...] | None, node: ast.Call, scope: ScopeInfo) -> str | None:
    if qname is None:
        return "file_handle" if _is_builtin_open(node.func, scope) else None
    if qname == ("subprocess", "run"):
        return "completed_process"
    if (
        len(qname) == 2
        and qname[0] in {"requests", "httpx"}
        and qname[1]
        in {
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "head",
            "options",
            "request",
        }
    ):
        return "response"
    if qname == ("urllib", "request", "urlopen") and _has_explicit_module(scope, ("urllib", "request")):
        return "response"
    if qname in {("re", "search"), ("re", "match"), ("re", "fullmatch")}:
        return "match"
    if qname in {("re", "findall"), ("re", "finditer")}:
        return "matches"
    if qname == ("sys", "exc_info"):
        return "exception_information"
    return None


def _eligible(assignment: Assignment) -> bool:
    scope = assignment.scope
    name = assignment.target.id
    if name in scope.global_or_nonlocal or name in scope.class_references or _declared_in_descendant(scope, name):
        return False
    return len(scope.bindings[name]) == 1 and scope.bindings[name][0] is assignment.target


def _declared_in_descendant(scope: ScopeInfo, name: str) -> bool:
    return any(name in child.global_or_nonlocal or _declared_in_descendant(child, name) for child in scope.children)


def _remove_collisions(
    planned: list[_PlannedProposal],
    forbidden_names: set[str],
) -> dict[TargetKey, RenameProposal]:
    rejected: set[int] = set()
    proposals_by_scope: dict[int, list[int]] = defaultdict(list)
    scopes: dict[int, ScopeInfo] = {}
    for index, planned_proposal in enumerate(planned):
        scope = planned_proposal.assignment.scope
        scope_id = id(scope)
        proposals_by_scope[scope_id].append(index)
        scopes[scope_id] = scope

    def visit(scope: ScopeInfo, active: dict[str, list[int]]) -> None:
        scope_id = id(scope)
        added: list[str] = []
        for index in proposals_by_scope.get(scope_id, []):
            name = planned[index].proposal.name
            if name in active:
                rejected.update(active[name])
                rejected.add(index)
            active.setdefault(name, []).append(index)
            added.append(name)
        for child in scope.children:
            visit(child, active)
        for name in reversed(added):
            active[name].pop()
            if not active[name]:
                del active[name]

    root_scopes: dict[int, ScopeInfo] = {}
    for scope in scopes.values():
        root = scope
        while root.parent is not None:
            root = root.parent
        root_scopes[id(root)] = root
    for root_scope in root_scopes.values():
        visit(root_scope, {})
    return {
        _target_key(planned_proposal.assignment.target): planned_proposal.proposal
        for index, planned_proposal in enumerate(planned)
        if index not in rejected and _is_valid_name(planned_proposal.proposal.name, forbidden_names)
    }


def _parametrize_result_proposals(
    index: _Index,
    forbidden_names: set[str],
    ignored_lines: set[int],
) -> Iterable[tuple[TargetKey, RenameProposal]]:
    if "result" not in forbidden_names:
        return
    for scope in _iter_scopes(index.root):
        node = scope.node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or not _has_parametrize_argname(node, "result"):
            continue
        parameter = next((arg for arg in _arguments(node.args) if arg.arg == "result"), None)
        if parameter is None or parameter.lineno in ignored_lines:
            continue
        if "expected_result" in _reachable_names(scope) or not _compares_name_for_equality(node, "result"):
            continue
        yield (
            (parameter.lineno, parameter.col_offset),
            RenameProposal("expected_result", Confidence.SUGGESTION_ONLY, frozenset({"parametrize_result"})),
        )


def _has_parametrize_argname(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    return any(name in _decorator_parametrize_argnames(decorator) for decorator in node.decorator_list)


def _decorator_parametrize_argnames(decorator: ast.expr) -> frozenset[str]:
    if not isinstance(decorator, ast.Call) or not _is_parametrize_call(decorator.func) or not decorator.args:
        return frozenset()
    return _parse_argnames(decorator.args[0])


def _is_parametrize_call(func: ast.expr) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "parametrize"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "mark"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "pytest"
    )


def _parse_argnames(node: ast.expr) -> frozenset[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset(part.strip() for part in node.value.split(","))
    if isinstance(node, ast.List | ast.Tuple):
        return frozenset(
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
    return frozenset()


def _compares_name_for_equality(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Compare)
        and len(child.ops) == 1
        and isinstance(child.ops[0], ast.Eq)
        and len(child.comparators) == 1
        and (_is_load_name(child.left, name) or _is_load_name(child.comparators[0], name))
        for child in ast.walk(node)
    )


def _is_load_name(expression: ast.expr, name: str) -> bool:
    return isinstance(expression, ast.Name) and isinstance(expression.ctx, ast.Load) and expression.id == name


_VERB_PARAMETER_NAMES = {"compress": "uncompressed", "decompress": "compressed"}


def _verb_parameter_proposals(
    index: _Index,
    forbidden_names: set[str],
    ignored_lines: set[int],
) -> Iterable[tuple[TargetKey, RenameProposal]]:
    if "data" not in forbidden_names:
        return
    for scope in _iter_scopes(index.root):
        node = scope.node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        proposed_name = _VERB_PARAMETER_NAMES.get(node.name)
        if proposed_name is None:
            continue
        parameter = next((arg for arg in _arguments(node.args) if arg.arg == "data"), None)
        if parameter is None or parameter.lineno in ignored_lines or proposed_name in _reachable_names(scope):
            continue
        yield (
            (parameter.lineno, parameter.col_offset),
            RenameProposal(proposed_name, Confidence.SUGGESTION_ONLY, frozenset({"verb_parameter"})),
        )


def _annotation_name(annotation: ast.expr | None) -> str | None:
    if annotation is None:
        return None
    if isinstance(annotation, ast.Subscript):
        base = _annotation_terminal(annotation.value)
        if base in {"list", "set", "frozenset"}:
            element_name = _annotation_name(_slice_value(annotation.slice))
            return _pluralize(element_name) if element_name is not None else None
        if base == "tuple" and isinstance(annotation.slice, ast.Tuple) and len(annotation.slice.elts) == 2:
            element_annotation, ellipsis = annotation.slice.elts
            if isinstance(ellipsis, ast.Constant) and ellipsis.value is Ellipsis:
                element_name = _annotation_name(element_annotation)
                return _pluralize(element_name) if element_name is not None else None
        if base not in _GENERIC_TYPE_NAMES:
            return _type_name(base)
        return None
    return _type_name(_annotation_terminal(annotation))


def _annotation_constraints(annotation: ast.expr | None) -> set[str]:
    if _annotation_terminal(annotation) == "bool":
        return {"bool"}
    if _annotation_terminal(annotation) == "str":
        return {"text"}
    if _annotation_terminal(annotation) in {"bytes", "bytearray"}:
        return {"bytes"}
    return set()


def _annotation_terminal(annotation: ast.expr | None) -> str:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        return _annotation_terminal(annotation.value)
    return ""


def _type_name(name: str) -> str | None:
    if not name or name in _GENERIC_TYPE_NAMES or not name[0].isupper():
        return None
    return _to_snake_case(name)


def _comprehension_name(node: ast.ListComp | ast.SetComp | ast.GeneratorExp) -> str | None:
    if len(node.generators) != 1:
        return None
    generator = node.generators[0]
    if generator.ifs or not isinstance(generator.target, ast.Name) or not isinstance(node.elt, ast.Attribute):
        return None
    if not isinstance(node.elt.value, ast.Name) or node.elt.value.id != generator.target.id:
        return None
    if not _is_public_attribute(node.elt.attr):
        return None
    return _pluralize(f"{generator.target.id}_{node.elt.attr}")


def _attribute_role(attribute: str) -> str | None:
    if attribute in {"status_code", "headers", "text", "content", "json", "raise_for_status", "ok"}:
        return "response"
    if attribute in {"returncode", "stdout", "stderr", "args"}:
        return "completed_process"
    if attribute in {"group", "groups", "groupdict", "span", "start", "end"}:
        return "match"
    if attribute in {"read", "write", "seek", "close", "fileno"}:
        return "file_handle"
    return None


def _qname(node: ast.expr, scope: ScopeInfo) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return _import_qname(scope, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qname(node.value, scope)
        return (*parent, node.attr) if parent is not None else None
    return None


def _import_qname(scope: ScopeInfo, name: str) -> tuple[str, ...] | None:
    current: ScopeInfo | None = scope
    while current is not None:
        if name in current.imports:
            return current.imports[name]
        if name in current.bindings:
            return None
        current = current.parent
    return None


def _function_return_annotation(scope: ScopeInfo, name: str) -> ast.expr | None:
    current: ScopeInfo | None = scope
    while current is not None:
        if name in current.function_returns:
            return current.function_returns[name]
        if name in current.bindings:
            return None
        current = current.parent
    return None


def _has_explicit_module(scope: ScopeInfo, module: tuple[str, ...]) -> bool:
    current: ScopeInfo | None = scope
    while current is not None:
        if module in current.explicit_modules:
            return True
        current = current.parent
    return False


def _constructor_name(node: ast.expr) -> str | None:
    terminal = _call_terminal_name(node)
    if terminal is None or not terminal[0].isupper():
        return None
    return _to_snake_case(terminal)


def _call_terminal_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_path_open(node: ast.Call, scope: ScopeInfo) -> bool:
    if (
        not isinstance(node.func, ast.Attribute)
        or node.func.attr != "open"
        or not isinstance(node.func.value, ast.Call)
    ):
        return False
    return _qname(node.func.value.func, scope) == ("pathlib", "Path")


def _is_builtin_open(node: ast.expr, scope: ScopeInfo) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == "open"
        and _import_qname(scope, "open") is None
        and not _is_bound(scope, "open")
    )


def _is_bound(scope: ScopeInfo, name: str) -> bool:
    current: ScopeInfo | None = scope
    while current is not None:
        if name in current.bindings:
            return True
        current = current.parent
    return False


def _is_parser_argument(qname: tuple[str, ...] | None, position: int | str, parser: str) -> bool:
    if position != 0:
        return False
    return qname == ("json", "loads") if parser == "json" else qname == ("tomllib", "loads")


def _is_subprocess_argument(qname: tuple[str, ...] | None, position: int | str) -> bool:
    return qname == ("subprocess", "run") and position in {0, "args"}


def _is_http_body_argument(qname: tuple[str, ...] | None, position: int | str) -> bool:
    return (
        qname is not None
        and len(qname) == 2
        and qname[0] in {"requests", "httpx"}
        and qname[1] in {"post", "put", "patch", "request"}
        and position == "content"
    )


def _is_len_call(node: ast.Call, position: int | str) -> bool:
    return position == 0 and isinstance(node.func, ast.Name) and node.func.id == "len"


def _is_command_shape(value: ast.expr) -> bool:
    return isinstance(value, ast.List | ast.Tuple)


def _confirm_collection_candidates(candidates: dict[str, set[str]]) -> None:
    for candidate, evidence in candidates.items():
        if _singularize(candidate) != candidate:
            evidence.add("collection_use")


def _reachable_names(scope: ScopeInfo) -> set[str]:
    if scope.reachable_names is not None:
        return scope.reachable_names
    names: set[str] = set()
    for node in ast.walk(scope.node):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
            if node.name is not None:
                names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.add(node.rest)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            names.update(node.names)
        elif isinstance(node, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple):
            names.add(node.name)
    scope.reachable_names = names
    return names


def _iter_scopes(scope: ScopeInfo) -> Iterable[ScopeInfo]:
    yield scope
    for child in scope.children:
        yield from _iter_scopes(child)


def _arguments(arguments: ast.arguments) -> Iterable[ast.arg]:
    yield from arguments.posonlyargs
    yield from arguments.args
    yield from arguments.kwonlyargs
    if arguments.vararg is not None:
        yield arguments.vararg
    if arguments.kwarg is not None:
        yield arguments.kwarg


def _condition_name(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name) and isinstance(expression.ctx, ast.Load):
        return expression.id
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return _condition_name(expression.operand)
    return None


def _is_reflection_call(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Name):
        return node.func.id in {"locals", "vars", "eval", "exec"}
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "currentframe"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "inspect"
    )


def _position(node: ast.expr | ast.stmt) -> tuple[int, int]:
    return (node.lineno, node.col_offset)


def _target_key(target: ast.Name) -> TargetKey:
    return (target.lineno, target.col_offset)


def _slice_value(node: ast.expr) -> ast.expr:
    return node


def _is_public_attribute(name: str) -> bool:
    return bool(_SNAKE_CASE.fullmatch(name)) and not name.startswith("_") and not name.isupper()


def _is_valid_name(name: str, forbidden_names: set[str]) -> bool:
    return (
        bool(_SNAKE_CASE.fullmatch(name))
        and not keyword.iskeyword(name)
        and name not in forbidden_names
        and name not in _BUILTIN_NAMES
    )


def _to_snake_case(name: str) -> str:
    return _CAMEL_BOUNDARY.sub(r"\1_\2", _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", name)).lower()


def _pluralize(name: str | None) -> str | None:
    if name is None:
        return None
    head, _, tail = name.rpartition("_")
    word = tail or head
    if word in _IRREGULAR_WORDS:
        return None
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        plural = f"{word[:-1]}ies"
    elif word.endswith(("s", "x", "z", "ch", "sh")):
        plural = f"{word}es"
    else:
        plural = f"{word}s"
    return f"{head}_{plural}" if head else plural


def _singularize(name: str) -> str:
    head, _, tail = name.rpartition("_")
    word = tail or head
    if word.endswith("ies") and len(word) > 3:
        singular = f"{word[:-3]}y"
    elif word.endswith(("ches", "shes", "xes", "zes", "sses")):
        singular = word[:-2]
    elif word.endswith("s") and not word.endswith("ss"):
        singular = word[:-1]
    else:
        singular = word
    return f"{head}_{singular}" if head else singular


_GENERIC_TYPE_NAMES = {
    "Any",
    "Literal",
    "Never",
    "None",
    "NoneType",
    "NoReturn",
    "Self",
    "TypeVar",
    "object",
    "bool",
    "bytearray",
    "bytes",
    "complex",
    "dict",
    "float",
    "frozenset",
    "int",
    "list",
    "set",
    "str",
    "tuple",
}
_IRREGULAR_WORDS = {
    "analysis",
    "basis",
    "child",
    "crisis",
    "datum",
    "foot",
    "goose",
    "man",
    "medium",
    "mouse",
    "ox",
    "person",
    "tooth",
    "woman",
}
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_SNAKE_CASE = re.compile(r"[a-z][a-z0-9_]*")
_BUILTIN_NAMES = frozenset(dir(builtins))
