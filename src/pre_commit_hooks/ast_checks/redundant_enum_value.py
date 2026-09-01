from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from ._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
    byte_col_to_char_col,
    find_ignored_lines_and_pytriage_comments,
    ignore_pattern_for,
    record_suppression_usage_if_ignored,
)
from ._scope import class_scope_binding_names, iter_binding_names, iter_within_scope

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


CHECK_ID = "redundant-enum-value"
ERROR_CODE = "TR10"
IGNORE_PATTERN = ignore_pattern_for(ERROR_CODE)
_ENUM_TYPE_NAMES = frozenset({"StrEnum", "IntEnum"})
_NONMEMBER_NAME = "nonmember"


class _EnumClass:
    __slots__ = ("members",)

    def __init__(self, members: frozenset[str]) -> None:
        self.members = members


def _names_bound_by(statements: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in statements:
        names.update(iter_binding_names(statement))
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for node in iter_within_scope(statement):
            names.update(iter_binding_names(node))
    return names


def _direct_enum_base(base: ast.expr, enum_type_names: set[str], enum_module_names: set[str]) -> bool:
    if isinstance(base, ast.Name):
        return base.id in enum_type_names
    return (
        isinstance(base, ast.Attribute)
        and base.attr in _ENUM_TYPE_NAMES
        and isinstance(base.value, ast.Name)
        and base.value.id in enum_module_names
    )


def _is_nonmember_call(value: ast.expr, nonmember_names: set[str], enum_module_names: set[str]) -> bool:
    match value:
        case ast.Call(func=ast.Name(id=name)):
            return name in nonmember_names
        case ast.Call(func=ast.Attribute(value=ast.Name(id=name), attr=attr)) if attr == _NONMEMBER_NAME:
            return name in enum_module_names
    return False


def _member_names(class_node: ast.ClassDef, nonmember_names: set[str], enum_module_names: set[str]) -> frozenset[str]:
    members: set[str] = set()
    aliases: set[str] = set()
    for statement in class_node.body:
        match statement:
            case ast.Assign(targets=[ast.Name(id=name)], value=value):
                names = (name,)
            case ast.AnnAssign(target=ast.Name(id=name), value=value):
                names = (name,)
            case _:
                continue
        if value is None:
            continue
        for name in names:
            if name.startswith("_"):
                continue
            if _is_nonmember_call(value, nonmember_names, enum_module_names):
                members.discard(name)
                aliases.discard(name)
                continue
            if isinstance(value, ast.Name) and value.id in members:
                members.discard(name)
                aliases.add(name)
            else:
                aliases.discard(name)
                members.add(name)
    return frozenset(members - aliases)


def _function_bindings(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    names = {
        argument.arg
        for argument in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *([node.args.vararg] if node.args.vararg else []),
            *([node.args.kwarg] if node.args.kwarg else []),
        ]
    }
    if isinstance(node, ast.Lambda):
        return names
    names.update(
        type_param.name
        for type_param in node.type_params
        if isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
    )
    return names | _names_bound_by(node.body)


def _comprehension_bindings(node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> set[str]:
    names: set[str] = set()
    for generator in node.generators:
        names.update(
            name.id
            for name in ast.walk(generator.target)
            if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
        )
    return names


def _scope_bindings(node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    if isinstance(node, ast.Module):
        return set()
    if isinstance(node, ast.ClassDef):
        return class_scope_binding_names(node)
    return _function_bindings(node)


def _nested_scopes(statements: list[ast.stmt]) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    scopes: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            scopes.append(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            scopes.append(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            scopes.append(node)

    visitor = Visitor()
    for statement in statements:
        visitor.visit(statement)
    return scopes


def _local_enum_classes(
    tree: ast.Module,
) -> dict[int, dict[str, _EnumClass]]:
    scope_classes: dict[int, dict[str, _EnumClass]] = {}

    def collect(
        scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        inherited_enum_type_names: set[str],
        inherited_enum_module_names: set[str],
        inherited_nonmember_names: set[str],
    ) -> None:
        bound_names = _scope_bindings(scope)
        enum_type_names = inherited_enum_type_names - bound_names
        enum_module_names = inherited_enum_module_names - bound_names
        nonmember_names = inherited_nonmember_names - bound_names
        enum_classes: dict[str, _EnumClass] = {}
        for statement in scope.body:
            bindings = _names_bound_by([statement])
            enum_type_names.difference_update(bindings)
            enum_module_names.difference_update(bindings)
            nonmember_names.difference_update(bindings)
            for name in bindings:
                enum_classes.pop(name, None)
            match statement:
                case ast.Import():
                    for alias in statement.names:
                        if alias.name == "enum":
                            enum_module_names.add(alias.asname or "enum")
                case ast.ImportFrom(module="enum"):
                    for alias in statement.names:
                        imported_name = alias.asname or alias.name
                        if alias.name in _ENUM_TYPE_NAMES:
                            enum_type_names.add(imported_name)
                        elif alias.name == _NONMEMBER_NAME:
                            nonmember_names.add(imported_name)
                case ast.ClassDef():
                    if not statement.decorator_list and any(
                        _direct_enum_base(base, enum_type_names, enum_module_names) for base in statement.bases
                    ):
                        enum_classes[statement.name] = _EnumClass(
                            _member_names(statement, nonmember_names, enum_module_names)
                        )
            for nested_scope in _nested_scopes([statement]):
                collect(nested_scope, enum_type_names, enum_module_names, nonmember_names)
        scope_classes[id(scope)] = enum_classes

    collect(tree, set(), set(), set())
    return scope_classes


class _ValueVisitor(ast.NodeVisitor):
    def __init__(self, scope_classes: dict[int, dict[str, _EnumClass]]) -> None:
        self._scope_classes = scope_classes
        self._scope_stack: list[tuple[ast.AST, set[str], dict[str, _EnumClass]]] = []
        self.candidates: list[ast.Attribute] = []

    def _visit_scope(self, node: ast.AST, bindings: set[str], children: Iterable[ast.AST]) -> None:
        enum_classes = self._scope_classes.get(id(node), {})
        self._scope_stack.append((node, bindings - enum_classes.keys(), enum_classes))
        for child in children:
            self.visit(child)
        self._scope_stack.pop()

    def _enum_class(self, class_name: str) -> _EnumClass | None:
        inside_function = False
        for scope, bindings, enum_classes in reversed(self._scope_stack):
            if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                inside_function = True
            if inside_function and isinstance(scope, ast.ClassDef):
                continue
            if class_name in bindings:
                return None
            if enum_class := enum_classes.get(class_name):
                return enum_class
        return None

    def visit_Module(self, node: ast.Module) -> None:
        self._visit_scope(node, set(), node.body)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for child in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults, node.returns]:
            if child is not None:
                self.visit(child)
        self._visit_scope(node, _function_bindings(node), node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for child in [*node.args.defaults, *node.args.kw_defaults]:
            if child is not None:
                self.visit(child)
        self._visit_scope(node, _function_bindings(node), [node.body])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for child in [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]:
            self.visit(child)
        self._visit_scope(node, class_scope_binding_names(node), node.body)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_scope(node, _comprehension_bindings(node), [node.elt, *node.generators])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_scope(node, _comprehension_bindings(node), [node.elt, *node.generators])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_scope(node, _comprehension_bindings(node), [node.elt, *node.generators])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_scope(node, _comprehension_bindings(node), [node.key, node.value, *node.generators])

    def visit_Attribute(self, node: ast.Attribute) -> None:
        match node:
            case ast.Attribute(value=ast.Attribute(value=ast.Name(id=class_name), attr=member_name), attr="value"):
                enum_class = self._enum_class(class_name)
                if enum_class is not None and member_name in enum_class.members:
                    self.candidates.append(node)
        self.generic_visit(node)


class RedundantEnumValueCheck(BaseCheck):
    __slots__ = ()

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["StrEnum", "IntEnum", ".value"]

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        scope_classes = _local_enum_classes(tree)
        if not any(scope_classes.values()):
            return CheckResult()
        visitor = _ValueVisitor(scope_classes)
        visitor.visit(tree)
        if not visitor.candidates:
            return CheckResult()
        ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(source, IGNORE_PATTERN)
        source_lines = source.splitlines(keepends=True)
        violations = []
        suppression_usages: list[SuppressionUsage] = []
        for candidate in sorted(visitor.candidates, key=lambda item: (item.lineno, item.col_offset)):
            if record_suppression_usage_if_ignored(
                suppression_usages,
                comments,
                ignored_lines=ignored_lines,
                format_suppressed=format_suppressed,
                check_id=CHECK_ID,
                error_code=ERROR_CODE,
                candidate_lines=(candidate.lineno,),
            ):
                continue
            violations.append(
                Violation(
                    check_id=CHECK_ID,
                    error_code=ERROR_CODE,
                    line=candidate.lineno,
                    col=byte_col_to_char_col(source_lines[candidate.lineno - 1], candidate.col_offset),
                    message=(
                        "Redundant enum `.value` access; pass the enum member directly. "
                        f"Or add '# pytriage: {ERROR_CODE}' to suppress."
                    ),
                    fixable=False,
                )
            )
        return CheckResult(violations, suppression_usages)

    def fix(
        self,
        _filepath: Path,
        violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.DECLINED)
