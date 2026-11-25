from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Literal

from pre_commit_hooks.ast_checks._scope import class_scope_binding_names, iter_binding_names, iter_within_scope_from

from .catalog import (
    ABSTRACT_TYPES,
    CONSTRUCTORS,
    DICT,
    LIST,
    PRODUCERS,
    SET,
    TUPLE,
    TYPING_MODULES,
    AbstractParameters,
    AbstractType,
    ConcreteParameters,
    ConcreteType,
)
from .names import FunctionNode, NameResolver, Scope, function_arguments, type_parameter_names

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .catalog import QualifiedName


class OverlyBroadReturnTypeLevel(Enum):
    CONSERVATIVE = auto()
    AGGRESSIVE = auto()


@dataclass(frozen=True, slots=True)
class Finding:
    node: ast.expr
    candidate_lines: tuple[int, ...]
    annotation: str
    concrete: str
    suggestion: str | None
    returns_none: bool


@dataclass(frozen=True, slots=True)
class _Candidate:
    function: FunctionNode
    node: ast.expr
    abstract: AbstractType
    concrete: ConcreteType
    returns_none: bool
    through_variable: bool


@dataclass(frozen=True, slots=True)
class _Site:
    kind: Literal["assign", "annotation", "augmented"]
    value: ast.expr | None
    annotation: ast.expr | None = None
    operator: ast.operator | None = None


def _qualified(*dotted_names: str) -> frozenset[QualifiedName]:
    return frozenset(tuple(name.split(".")) for name in dotted_names)


def _typing(name: str) -> frozenset[QualifiedName]:
    return _qualified(*(f"{module}.{name}" for module in TYPING_MODULES))


_OVERLOAD = _typing("overload")
_ABSTRACTMETHOD = _qualified("abc.abstractmethod")
_FINAL = _typing("final")
_OPTIONAL = _typing("Optional")
_UNION = _typing("Union")
_PROTOCOL_OR_ABC_BASES = _typing("Protocol") | _qualified("abc.ABC")
_ABC_METACLASSES = _qualified("abc.ABCMeta")
_TRIVIAL_BASES = _typing("Generic") | _qualified("builtins.object")
_STATEMENT_BLOCK_FIELDS = ("body", "orelse", "finalbody")
_ACCESSOR_ATTRIBUTES = frozenset({"getter", "setter", "deleter"})
_ABSTRACT_NAMES = frozenset(qualified_name[-1] for qualified_name in ABSTRACT_TYPES)
_PARAMETER_COUNTS = {AbstractParameters.NONE: 0, AbstractParameters.ELEMENT: 1, AbstractParameters.MAPPING: 2}
_HEAD_SWAP_PARAMETERS = frozenset(
    {
        (AbstractParameters.ELEMENT, ConcreteParameters.ELEMENT),
        (AbstractParameters.ELEMENT, ConcreteParameters.COUNTER),
        (AbstractParameters.MAPPING, ConcreteParameters.MAPPING),
    }
)
_DISPLAY_TYPES: dict[type[ast.expr], ConcreteType] = {
    ast.Dict: DICT,
    ast.DictComp: DICT,
    ast.List: LIST,
    ast.ListComp: LIST,
    ast.Set: SET,
    ast.SetComp: SET,
    ast.Tuple: TUPLE,
}


def find_findings(tree: ast.Module, level: OverlyBroadReturnTypeLevel) -> list[Finding]:
    if not _mentions_abstract_return(tree):
        return []
    return _Analyzer(tree, level).findings()


def _mentions_abstract_return(tree: ast.Module) -> bool:
    statements = list(_iter_statements(tree.body))
    names = _ABSTRACT_NAMES | {
        alias.asname
        for statement in statements
        if isinstance(statement, ast.ImportFrom)
        for alias in statement.names
        if alias.asname is not None and alias.name in _ABSTRACT_NAMES
    }
    return any(
        isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.returns is not None
        and any(
            (isinstance(part, ast.Name) and part.id in names)
            or (isinstance(part, ast.Attribute) and part.attr in _ABSTRACT_NAMES)
            for part in ast.walk(statement.returns)
        )
        for statement in statements
    )


def _iter_statements(statements: list[ast.stmt]) -> Iterator[ast.stmt]:
    pending = list(statements)
    while pending:
        statement = pending.pop()
        yield statement
        for field in _STATEMENT_BLOCK_FIELDS:
            block = getattr(statement, field, None)
            if block:
                pending.extend(block)
        if isinstance(statement, (ast.Try, ast.TryStar)):
            pending.extend(child for handler in statement.handlers for child in handler.body)
        elif isinstance(statement, ast.Match):
            pending.extend(child for case in statement.cases for child in case.body)


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _union_members(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*_union_members(node.left), *_union_members(node.right)]
    return [node]


def _single_optional_member(members: list[ast.expr]) -> tuple[ast.expr, bool] | None:
    others = [member for member in members if not _is_none(member)]
    if len(others) != 1 or len(others) == len(members):
        return None
    return others[0], True


def _annotation_head(node: ast.expr) -> ast.expr:
    return node.value if isinstance(node, ast.Subscript) else node


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _variable_dependencies(sites: list[_Site]) -> Iterator[str]:
    return _returned_names([site.value for site in sites if site.value is not None])


def _returned_names(values: list[ast.expr]) -> Iterator[str]:
    pending = list(values)
    while pending:
        match pending.pop():
            case ast.IfExp(body=body, orelse=orelse):
                pending.extend((body, orelse))
            case ast.NamedExpr(value=value):
                pending.append(value)
            case ast.Name(id=name, ctx=ast.Load()):
                yield name


def _is_mangled(name: str) -> bool:
    return name.startswith("__") and not name.endswith("__")


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_accessor(node: FunctionNode) -> bool:
    return any(
        isinstance(decorator, ast.Attribute)
        and decorator.attr in _ACCESSOR_ATTRIBUTES
        and isinstance(decorator.value, ast.Name)
        and decorator.value.id == node.name
        for decorator in node.decorator_list
    )


def _return_statements(node: FunctionNode) -> list[ast.Return] | None:
    returns: list[ast.Return] = []
    for child in iter_within_scope_from(node.body):
        if isinstance(child, ast.Yield | ast.YieldFrom):
            return None
        if isinstance(child, ast.Return):
            returns.append(child)
    return returns


def _always_exits(statements: list[ast.stmt]) -> bool:
    return any(_statement_always_exits(statement) for statement in statements)


def _statement_always_exits(statement: ast.stmt) -> bool:
    match statement:
        case ast.Return() | ast.Raise():
            return True
        case ast.If(body=body, orelse=orelse):
            return _always_exits(body) and _always_exits(orelse)
        case ast.With(body=body) | ast.AsyncWith(body=body):
            return _always_exits(body)
        case (
            ast.Try(body=body, handlers=handlers, orelse=orelse, finalbody=finalbody)
            | ast.TryStar(body=body, handlers=handlers, orelse=orelse, finalbody=finalbody)
        ):
            return _always_exits(finalbody) or (
                (_always_exits(body) or _always_exits(orelse))
                and all(_always_exits(handler.body) for handler in handlers)
            )
        case ast.While(test=ast.Constant(value=test), body=body):
            return bool(test) and not _breaks_out(body)
        case ast.Match(cases=cases):
            return any(
                isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None and case.guard is None
                for case in cases
            ) and all(_always_exits(case.body) for case in cases)
    return False


def _breaks_out(statements: list[ast.stmt]) -> bool:
    pending: list[ast.AST] = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Break):
            return True
        if isinstance(node, ast.For | ast.AsyncFor | ast.While):
            pending.extend(node.orelse)
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return False


class _Analyzer:
    __slots__ = (
        "_bases",
        "_binding_names",
        "_last_hierarchy",
        "_level",
        "_pending_variables",
        "_resolver",
        "_sites",
        "_subclasses",
        "_unknown_local_bases",
        "_variable_proofs",
        "_variants",
    )

    def __init__(self, tree: ast.Module, level: OverlyBroadReturnTypeLevel) -> None:
        self._level = level
        self._resolver = NameResolver(tree)
        self._sites: dict[ast.AST, dict[str, list[_Site]]] = {}
        self._variable_proofs: dict[ast.AST, dict[str, ConcreteType | None]] = {}
        self._pending_variables: dict[ast.AST, set[str]] = {}
        self._unknown_local_bases: bool | None = None
        self._variants: dict[ast.AST, dict[str, int]] = {}
        self._binding_names: dict[ast.ClassDef, set[str]] = {}
        self._subclasses: dict[ast.ClassDef, list[ast.ClassDef]] | None = None
        self._bases: dict[ast.ClassDef, list[ast.ClassDef] | None] = {}
        self._last_hierarchy: tuple[ast.ClassDef, set[ast.ClassDef] | None] | None = None

    def findings(self) -> list[Finding]:
        findings: list[Finding] = []
        for scope in list(self._resolver.scopes()):
            node = scope.node
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.returns is None:
                continue
            assert scope.parent is not None
            finding = self._function_finding(node, scope, scope.parent)
            if finding is not None:
                findings.append(finding)
        return sorted(findings, key=lambda finding: (finding.node.lineno, finding.node.col_offset))

    def _function_finding(self, node: FunctionNode, scope: Scope, parent: Scope) -> Finding | None:
        assert node.returns is not None
        split = self._split_optional(node.returns, parent)
        if split is None:
            return None
        abstract_node, allows_none = split
        abstract = self._abstract_type(abstract_node, parent, node)
        if abstract is None:
            return None
        decorators = [self._decorator_names(decorator, parent) for decorator in node.decorator_list]
        if not self._eligible(node, parent, decorators):
            return None
        returns = _return_statements(node)
        if returns is None:
            return None
        returns_none = not _always_exits(node.body)
        concretes: list[ConcreteType] = []
        for statement in returns:
            if statement.value is None or _is_none(statement.value):
                returns_none = True
                continue
            concrete = self._prove(statement.value, scope)
            if concrete is None:
                return None
            concretes.append(concrete)
        if not concretes or (returns_none and not allows_none):
            return None
        concrete = concretes[0]
        if any(other is not concrete for other in concretes) or abstract.name not in concrete.supertypes:
            return None
        return self._finding(
            _Candidate(
                function=node,
                node=abstract_node,
                abstract=abstract,
                concrete=concrete,
                returns_none=returns_none,
                through_variable=next(
                    _returned_names([statement.value for statement in returns if statement.value]), None
                )
                is not None,
            )
        )

    def _decorator_names(self, decorator: ast.expr, scope: Scope) -> frozenset[QualifiedName] | None:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        return self._resolver.qualified_names(target, scope)

    def _eligible(self, node: FunctionNode, parent: Scope, decorators: list[frozenset[QualifiedName] | None]) -> bool:
        if any(names is not None and names & (_OVERLOAD | _ABSTRACTMETHOD) for names in decorators):
            return False
        if self._function_variants(parent).get(node.name, 0) > 1:
            return False
        if not isinstance(parent.node, ast.ClassDef):
            return True
        return self._method_eligible(node, parent.node, decorators)

    def _function_variants(self, scope: Scope) -> dict[str, int]:
        variants = self._variants.get(scope.node)
        if variants is None:
            variants = defaultdict(int)
            for child in iter_within_scope_from(scope.node.body):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and not _is_accessor(child):
                    variants[child.name] += 1
            self._variants[scope.node] = variants
        return variants

    def _method_eligible(
        self, node: FunctionNode, class_node: ast.ClassDef, decorators: list[frozenset[QualifiedName] | None]
    ) -> bool:
        if _is_dunder(node.name) or self._is_protocol_or_abc(class_node):
            return False
        if (
            any(names is not None and names & _FINAL for names in decorators)
            or self._is_final_class(class_node)
            or _is_mangled(node.name)
        ):
            return True
        if self._level is OverlyBroadReturnTypeLevel.CONSERVATIVE:
            return False
        if self._has_unknown_local_bases() or self._is_placeholder(node):
            return False
        hierarchy = self._hierarchy(class_node)
        return hierarchy is not None and not any(
            node.name in self._class_binding_names(related_class) for related_class in hierarchy
        )

    def _hierarchy(self, class_node: ast.ClassDef) -> set[ast.ClassDef] | None:
        if self._last_hierarchy is not None and self._last_hierarchy[0] is class_node:
            return self._last_hierarchy[1]
        visited = {class_node}
        hierarchy: set[ast.ClassDef] | None = None
        if self._collect_ancestors(class_node, visited):
            descendants = self._descendants(class_node)
            visited.update(descendants)
            if all(self._collect_ancestors(descendant, visited) for descendant in descendants):
                hierarchy = visited - {class_node}
        self._last_hierarchy = (class_node, hierarchy)
        return hierarchy

    def _collect_ancestors(self, class_node: ast.ClassDef, visited: set[ast.ClassDef]) -> bool:
        pending = [class_node]
        while pending:
            bases = self._local_bases(pending.pop())
            if bases is None:
                return False
            for base in bases:
                if base not in visited:
                    visited.add(base)
                    pending.append(base)
        return True

    def _has_unknown_local_bases(self) -> bool:
        if self._unknown_local_bases is None:
            self._unknown_local_bases = any(
                self._resolver.qualified_names(_annotation_head(base), scope.parent) is None
                and self._resolver.local_class(_annotation_head(base), scope.parent) is None
                for scope in list(self._resolver.scopes())
                if isinstance(scope.node, ast.ClassDef) and scope.parent is not None
                for base in scope.node.bases
            )
        return self._unknown_local_bases

    def _is_placeholder(self, node: FunctionNode) -> bool:
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
            return False
        value = body[0].value
        match value:
            case ast.List(elts=[]) | ast.Tuple(elts=[]) | ast.Dict(keys=[]):
                return True
            case ast.Call(args=[], keywords=[]):
                names = self._resolver.qualified_names(value.func, self._resolver.scope(node))
                return names is not None and all(name in CONSTRUCTORS for name in names)
        return False

    def _class_parent_scope(self, class_node: ast.ClassDef) -> Scope:
        parent = self._resolver.scope(class_node).parent
        assert parent is not None
        return parent

    def _class_binding_names(self, class_node: ast.ClassDef) -> set[str]:
        names = self._binding_names.get(class_node)
        if names is None:
            names = class_scope_binding_names(class_node)
            self._binding_names[class_node] = names
        return names

    def _is_protocol_or_abc(self, class_node: ast.ClassDef) -> bool:
        parent = self._class_parent_scope(class_node)
        for base in class_node.bases:
            names = self._resolver.qualified_names(_annotation_head(base), parent)
            if names is not None and names & _PROTOCOL_OR_ABC_BASES:
                return True
        for keyword in class_node.keywords:
            names = self._resolver.qualified_names(keyword.value, parent)
            if keyword.arg == "metaclass" and names is not None and names & _ABC_METACLASSES:
                return True
        class_scope = self._resolver.scope(class_node)
        return any(
            isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
            and any(
                (names := self._decorator_names(decorator, class_scope)) is not None and names & _ABSTRACTMETHOD
                for decorator in statement.decorator_list
            )
            for statement in iter_within_scope_from(class_node.body)
        )

    def _is_final_class(self, class_node: ast.ClassDef) -> bool:
        parent = self._class_parent_scope(class_node)
        return any(
            (names := self._decorator_names(decorator, parent)) is not None and names & _FINAL
            for decorator in class_node.decorator_list
        )

    def _local_bases(self, class_node: ast.ClassDef) -> list[ast.ClassDef] | None:
        if class_node not in self._bases:
            self._bases[class_node] = self._resolve_local_bases(class_node)
        return self._bases[class_node]

    def _resolve_local_bases(self, class_node: ast.ClassDef) -> list[ast.ClassDef] | None:
        if class_node.keywords:
            return None
        parent = self._class_parent_scope(class_node)
        bases: list[ast.ClassDef] = []
        for base in class_node.bases:
            head = _annotation_head(base)
            names = self._resolver.qualified_names(head, parent)
            if names is not None and names <= _TRIVIAL_BASES:
                continue
            local = self._resolver.local_class(head, parent)
            if local is None:
                return None
            bases.append(local)
        return bases

    def _local_subclasses(self) -> dict[ast.ClassDef, list[ast.ClassDef]]:
        subclasses: dict[ast.ClassDef, list[ast.ClassDef]] = defaultdict(list)
        for scope in list(self._resolver.scopes()):
            if not isinstance(scope.node, ast.ClassDef):
                continue
            assert scope.parent is not None
            for base in scope.node.bases:
                local = self._resolver.local_class(_annotation_head(base), scope.parent)
                if local is not None:
                    subclasses[local].append(scope.node)
        return subclasses

    def _descendants(self, class_node: ast.ClassDef) -> list[ast.ClassDef]:
        descendants: list[ast.ClassDef] = []
        pending = [class_node]
        seen = {class_node}
        if self._subclasses is None:
            self._subclasses = self._local_subclasses()
        while pending:
            for subclass in self._subclasses.get(pending.pop(), ()):
                if subclass not in seen:
                    seen.add(subclass)
                    descendants.append(subclass)
                    pending.append(subclass)
        return descendants

    def _split_optional(self, annotation: ast.expr, scope: Scope) -> tuple[ast.expr, bool] | None:
        if isinstance(annotation, ast.Constant):
            return None
        if isinstance(annotation, ast.BinOp):
            return _single_optional_member(_union_members(annotation))
        if isinstance(annotation, ast.Subscript):
            names = self._resolver.qualified_names(annotation.value, scope)
            if names and names <= _OPTIONAL:
                return None if isinstance(annotation.slice, ast.Tuple) else (annotation.slice, True)
            if names and names <= _UNION:
                members = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
                return _single_optional_member(members)
        return annotation, False

    def _abstract_type(self, node: ast.expr, scope: Scope, function: FunctionNode) -> AbstractType | None:
        head = _annotation_head(node)
        if _root_name(head) in type_parameter_names(function):
            return None
        names = self._resolver.qualified_names(head, scope)
        if not names:
            return None
        abstract_types = {ABSTRACT_TYPES.get(name) for name in names}
        if len(abstract_types) != 1:
            return None
        abstract = abstract_types.pop()
        if abstract is None:
            return None
        if isinstance(node, ast.Subscript):
            arguments = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            expected = _PARAMETER_COUNTS[abstract.parameters]
            if len(arguments) != expected or (expected == 1 and isinstance(node.slice, ast.Tuple)):
                return None
            if any(isinstance(argument, ast.Starred) for argument in arguments):
                return None
        return abstract

    def _prove(self, node: ast.expr, scope: Scope) -> ConcreteType | None:
        display_type = _DISPLAY_TYPES.get(type(node))
        if display_type is not None:
            return display_type
        match node:
            case ast.Call():
                return self._prove_call(node, scope)
            case ast.IfExp(body=body, orelse=orelse):
                first = self._prove(body, scope)
                return first if first is not None and first is self._prove(orelse, scope) else None
            case ast.NamedExpr(value=value):
                return self._prove(value, scope)
            case ast.Name(ctx=ast.Load()):
                return self._variable_proof(node.id, scope)
        return None

    def _prove_call(self, node: ast.Call, scope: Scope) -> ConcreteType | None:
        names = self._resolver.qualified_names(node.func, scope)
        if not names:
            return None
        concretes = {CONSTRUCTORS.get(name) or PRODUCERS.get(name) for name in names}
        return concretes.pop() if len(concretes) == 1 else None

    def _variable_proof(self, name: str, scope: Scope) -> ConcreteType | None:
        node = scope.node
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        proofs = self._variable_proofs.setdefault(node, {})
        if name in proofs:
            return proofs[name]
        pending = self._pending_variables.setdefault(node, set())
        if name in pending:
            return None
        sites = self._local_sites(node)
        stack = [(name, False)]
        while stack:
            current, dependencies_done = stack.pop()
            if current in proofs:
                continue
            if dependencies_done:
                proofs[current] = self._evaluate_variable(sites[current], scope)
                pending.discard(current)
            elif current not in sites:
                proofs[current] = None
            else:
                pending.add(current)
                stack.append((current, True))
                stack.extend(
                    (dependency, False)
                    for dependency in _variable_dependencies(sites[current])
                    if dependency not in proofs and dependency not in pending
                )
        return proofs[name]

    def _evaluate_variable(self, sites: list[_Site], scope: Scope) -> ConcreteType | None:
        concrete: ConcreteType | None = None
        for site in sites:
            if site.kind == "augmented" or site.value is None:
                continue
            proven = self._prove(site.value, scope)
            if proven is None or (concrete is not None and proven is not concrete):
                return None
            concrete = proven
        if concrete is None:
            return None
        for site in sites:
            if site.annotation is not None:
                names = self._resolver.qualified_names(_annotation_head(site.annotation), scope)
                if names != {concrete.qualified_name}:
                    return None
            if site.kind == "augmented" and not self._preserves_type(site, concrete, scope):
                return None
        return concrete

    def _preserves_type(self, site: _Site, concrete: ConcreteType, scope: Scope) -> bool:
        operator = type(site.operator)
        if operator in concrete.augmented_operators:
            return True
        if operator not in concrete.operand_operators or site.value is None:
            return False
        operand = self._prove(site.value, scope)
        return operand is not None and operand.name in concrete.operand_types

    def _local_sites(self, node: FunctionNode) -> dict[str, list[_Site]]:
        cached = self._sites.get(node)
        if cached is not None:
            return cached
        sites: dict[str, list[_Site]] = defaultdict(list)
        registered: set[int] = set()
        unsupported = {argument.arg for argument in function_arguments(node)}
        for child in ast.walk(node):
            if isinstance(child, ast.Global | ast.Nonlocal):
                unsupported.update(child.names)
        children = list(iter_within_scope_from(node.body))
        for child in children:
            match child:
                case ast.Assign(targets=targets, value=value):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            sites[target.id].append(_Site("assign", value))
                            registered.add(id(target))
                case ast.AnnAssign(target=ast.Name() as target, annotation=annotation, value=value):
                    sites[target.id].append(_Site("annotation", value, annotation))
                    registered.add(id(target))
                case ast.AugAssign(target=ast.Name() as target, op=operator, value=value):
                    sites[target.id].append(_Site("augmented", value, operator=operator))
                    registered.add(id(target))
                case ast.NamedExpr(target=target, value=value):
                    sites[target.id].append(_Site("assign", value))
                    registered.add(id(target))
        for child in children:
            if isinstance(child, ast.Name):
                if id(child) not in registered and isinstance(child.ctx, ast.Store | ast.Del):
                    unsupported.add(child.id)
            else:
                unsupported.update(iter_binding_names(child))
        local_sites = {name: name_sites for name, name_sites in sites.items() if name not in unsupported}
        self._sites[node] = local_sites
        return local_sites

    def _finding(self, candidate: _Candidate) -> Finding:
        node, abstract, concrete = candidate.node, candidate.abstract, candidate.concrete
        suggestion: str | None = None
        if isinstance(node, ast.Subscript) and not candidate.through_variable:
            arguments = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            spelled = ", ".join(ast.unparse(argument) for argument in arguments)
            pair = (abstract.parameters, concrete.parameters)
            if pair in _HEAD_SWAP_PARAMETERS:
                suggestion = f"{concrete.name}[{spelled}]"
            elif pair == (AbstractParameters.ELEMENT, ConcreteParameters.TUPLE):
                suggestion = f"{concrete.name}[{spelled}, ...]"
            elif pair == (AbstractParameters.MAPPING, ConcreteParameters.COUNTER):
                suggestion = f"{concrete.name}[{ast.unparse(arguments[0])}]"
        return Finding(
            node=node,
            candidate_lines=tuple(sorted({candidate.function.lineno, node.lineno, node.end_lineno or node.lineno})),
            annotation=ast.unparse(node),
            concrete=concrete.name,
            suggestion=suggestion,
            returns_none=candidate.returns_none,
        )
