from __future__ import annotations

import ast
import logging
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict, cast

from ._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
    atomic_write_text,
    byte_col_to_char_col,
    find_ignored_lines,
    find_ignored_lines_and_pytriage_comments,
    ignore_pattern_for,
    record_suppression_usage_if_ignored,
    split_lines_like_ast,
)
from ._options import EnumOption
from ._scope import class_scope_binding_names, iter_binding_names, iter_within_scope_from
from .meaningless_vars_suggestions import Confidence, plan_suggestions

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ._options import CheckOption

logger = logging.getLogger("meaningless_vars")

IGNORE_PATTERN = ignore_pattern_for("TR1")

type VariableName = str
type _ComprehensionNode = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp


class MeaninglessVarsFixData(TypedDict):
    name: VariableName
    line: int
    col: int
    byte_col: int
    suggestion: VariableName | None
    auto_fixable: bool


DEFAULT_MEANINGLESS_NAMES = {"data", "result", "results"}


class MeaninglessVarsLevel(Enum):
    CONSERVATIVE = auto()
    PERMISSIVE = auto()


def _function_name_describes_parameter(function_name: str, parameter_name: VariableName) -> bool:
    suffix = f"_{parameter_name}"
    return function_name.endswith(suffix) and len(function_name) > len(suffix)


class MeaninglessNameVisitor(ast.NodeVisitor):
    def __init__(
        self,
        meaningless_names: set[VariableName],
        source: str,
    ) -> None:
        self.meaningless_names = meaningless_names
        self._ast_lines = split_lines_like_ast(source)
        self.violations: list[MeaninglessVarsFixData] = []

    def _check_name(
        self,
        name: VariableName,
        lineno: int,
        col_offset: int,
    ) -> None:
        if name in self.meaningless_names:
            violation: MeaninglessVarsFixData = {
                "name": name,
                "line": lineno,
                "col": byte_col_to_char_col(self._ast_lines[lineno - 1], col_offset),
                "byte_col": col_offset,
                "suggestion": None,
                "auto_fixable": False,
            }
            self.violations.append(violation)

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0]
            self._check_name(target.id, target.lineno, target.col_offset)

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._check_name(node.target.id, node.target.lineno, node.target.col_offset)
        self.generic_visit(node)

    @staticmethod
    def _has_decorator_named(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id == name:
                return True
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == name:
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if not self._has_decorator_named(node, "model_validator"):
            self._check_function_args(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if not self._has_decorator_named(node, "model_validator"):
            self._check_function_args(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.visit(stmt)

    def _check_function_args(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for arg in node.args.args:
            self._check_parameter(node.name, arg)
        for arg in node.args.posonlyargs:
            self._check_parameter(node.name, arg)
        for arg in node.args.kwonlyargs:
            self._check_parameter(node.name, arg)
        if node.args.vararg:
            self._check_parameter(node.name, node.args.vararg)
        if node.args.kwarg:
            self._check_parameter(node.name, node.args.kwarg)

    def _check_parameter(self, function_name: VariableName, arg: ast.arg) -> None:
        if _function_name_describes_parameter(function_name, arg.arg):
            return
        self._check_name(arg.arg, arg.lineno, arg.col_offset)


_CROSSABLE_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _binds_name_in_nested_scope(scope_node: ast.AST, name: VariableName) -> bool:
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = scope_node.args
        all_args = [
            *args.args,
            *args.posonlyargs,
            *args.kwonlyargs,
            *([args.vararg] if args.vararg else []),
            *([args.kwarg] if args.kwarg else []),
        ]
        if any(arg.arg == name for arg in all_args):
            return True
        if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple) and type_param.name == name
            for type_param in scope_node.type_params
        ):
            return True

        for child in _iter_own_scope_descendants(scope_node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and child.name == name:
                return True
            if name in iter_binding_names(child):
                return True
        return False

    assert isinstance(scope_node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp)
    return any(
        isinstance(target, ast.Name) and target.id == name
        for generator in scope_node.generators
        for target in ast.walk(generator.target)
    )


def _has_future_annotations_import(tree: ast.Module) -> bool:
    return any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "__future__"
        and any(a.name == "annotations" for a in stmt.names)
        for stmt in tree.body
    )


def _signature_defaults(args: ast.arguments) -> Iterator[ast.expr]:
    yield from args.defaults
    yield from (default for default in args.kw_defaults if default is not None)


def _signature_annotations(args: ast.arguments) -> Iterator[ast.expr]:
    all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        all_args.append(args.vararg)
    if args.kwarg:
        all_args.append(args.kwarg)
    for arg in all_args:
        if arg.annotation is not None:
            yield arg.annotation


def _type_param_defaults_and_bounds(type_params: list[ast.type_param]) -> Iterator[ast.expr]:
    for type_param in type_params:
        if isinstance(type_param, ast.TypeVar):
            if type_param.bound is not None:
                yield type_param.bound
            if type_param.default_value is not None:
                yield type_param.default_value
        elif isinstance(type_param, ast.ParamSpec | ast.TypeVarTuple) and type_param.default_value is not None:
            yield type_param.default_value


def _peer_filtered_replace_names(
    type_params: list[ast.type_param], replace_names: dict[VariableName, VariableName]
) -> dict[VariableName, VariableName]:
    peer_type_param_names = {
        type_param.name
        for type_param in type_params
        if isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
    }
    return {name: new for name, new in replace_names.items() if name not in peer_type_param_names}


def _outer_scope_children(
    scope_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | _ComprehensionNode,
    *,
    has_future_annotations: bool,
) -> Iterator[ast.AST]:
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef):
        yield from scope_node.decorator_list
        yield from _signature_defaults(scope_node.args)
        if not scope_node.type_params and not has_future_annotations:
            yield from _signature_annotations(scope_node.args)
            if scope_node.returns is not None:
                yield scope_node.returns
    elif isinstance(scope_node, ast.Lambda):
        yield from _signature_defaults(scope_node.args)
    else:
        yield scope_node.generators[0].iter


def _own_scope_children(
    scope_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | _ComprehensionNode,
) -> Iterator[ast.AST]:
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef):
        yield from scope_node.body
    elif isinstance(scope_node, ast.Lambda):
        yield scope_node.body
    elif isinstance(scope_node, ast.DictComp):
        yield scope_node.key
        yield scope_node.value
        yield from _comprehension_own_scope_generators(scope_node.generators)
    else:
        yield scope_node.elt
        yield from _comprehension_own_scope_generators(scope_node.generators)


def _comprehension_own_scope_generators(generators: list[ast.comprehension]) -> Iterator[ast.AST]:
    for index, generator in enumerate(generators):
        if index > 0:
            yield generator.iter
        yield generator.target
        yield from generator.ifs


def _iter_own_scope_descendants(
    scope_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | _ComprehensionNode,
) -> Iterator[ast.AST]:
    yield from iter_within_scope_from(_own_scope_children(scope_node))


def _collect_replacements(
    node: ast.AST,
    replace_names: dict[VariableName, VariableName],
    *,
    outer_replace_names: dict[VariableName, VariableName] | None = None,
    has_future_annotations: bool,
) -> list[tuple[int, int, VariableName, VariableName]]:
    if outer_replace_names is None:
        outer_replace_names = replace_names

    if isinstance(node, ast.Name):
        if node.id in replace_names:
            return [(node.lineno, node.col_offset, node.id, replace_names[node.id])]
        return []

    if isinstance(node, _CROSSABLE_SCOPE_NODES):
        replacements: list[tuple[int, int, VariableName, VariableName]] = []
        for outer_child in _outer_scope_children(node, has_future_annotations=has_future_annotations):
            replacements.extend(
                _collect_replacements(
                    outer_child,
                    replace_names,
                    outer_replace_names=outer_replace_names,
                    has_future_annotations=has_future_annotations,
                )
            )

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.type_params:
            bound_default_names = _peer_filtered_replace_names(node.type_params, replace_names)
            if bound_default_names:
                for expr in _type_param_defaults_and_bounds(node.type_params):
                    replacements.extend(
                        _collect_replacements(
                            expr,
                            bound_default_names,
                            outer_replace_names=bound_default_names,
                            has_future_annotations=has_future_annotations,
                        )
                    )

            if not has_future_annotations and bound_default_names:
                for expr in _signature_annotations(node.args):
                    replacements.extend(
                        _collect_replacements(
                            expr,
                            bound_default_names,
                            outer_replace_names=bound_default_names,
                            has_future_annotations=has_future_annotations,
                        )
                    )
                if node.returns is not None:
                    replacements.extend(
                        _collect_replacements(
                            node.returns,
                            bound_default_names,
                            outer_replace_names=bound_default_names,
                            has_future_annotations=has_future_annotations,
                        )
                    )

        nested_names = {
            name: new for name, new in outer_replace_names.items() if not _binds_name_in_nested_scope(node, name)
        }
        if nested_names:
            for own_child in _own_scope_children(node):
                replacements.extend(
                    _collect_replacements(
                        own_child,
                        nested_names,
                        outer_replace_names=nested_names,
                        has_future_annotations=has_future_annotations,
                    )
                )
        return replacements

    if isinstance(node, ast.TypeAlias):
        replacements = _collect_replacements(
            node.name,
            replace_names,
            outer_replace_names=outer_replace_names,
            has_future_annotations=has_future_annotations,
        )
        filtered_names = _peer_filtered_replace_names(node.type_params, replace_names)
        if filtered_names:
            for expr in _type_param_defaults_and_bounds(node.type_params):
                replacements.extend(
                    _collect_replacements(
                        expr,
                        filtered_names,
                        outer_replace_names=filtered_names,
                        has_future_annotations=has_future_annotations,
                    )
                )
            replacements.extend(
                _collect_replacements(
                    node.value,
                    filtered_names,
                    outer_replace_names=filtered_names,
                    has_future_annotations=has_future_annotations,
                )
            )
        return replacements

    if isinstance(node, ast.ClassDef):
        class_replacements: list[tuple[int, int, VariableName, VariableName]] = []

        def collect(
            target: ast.AST,
            replacements: dict[VariableName, VariableName],
            *,
            outer_names: dict[VariableName, VariableName],
        ) -> None:
            class_replacements.extend(
                _collect_replacements(
                    target,
                    replacements,
                    outer_replace_names=outer_names,
                    has_future_annotations=has_future_annotations,
                )
            )

        class_names = class_scope_binding_names(node)
        class_replace_names = {name: new for name, new in outer_replace_names.items() if name not in class_names}
        if replace_names:
            for decorator in node.decorator_list:
                collect(decorator, replace_names, outer_names=outer_replace_names)
        header_names = _peer_filtered_replace_names(node.type_params, replace_names)
        if header_names:
            for base in node.bases:
                collect(base, header_names, outer_names=header_names)
            for keyword in node.keywords:
                collect(keyword.value, header_names, outer_names=header_names)
            for expression in _type_param_defaults_and_bounds(node.type_params):
                collect(expression, header_names, outer_names=header_names)
        for child in node.body:
            collect(child, class_replace_names, outer_names=outer_replace_names)
        return class_replacements

    return [
        replacement
        for child in ast.iter_child_nodes(node)
        for replacement in _collect_replacements(
            child,
            replace_names,
            outer_replace_names=outer_replace_names,
            has_future_annotations=has_future_annotations,
        )
    ]


def _collect_scope_replacements(
    scope: ast.AST, replace_names: dict[VariableName, VariableName], *, has_future_annotations: bool
) -> list[tuple[int, int, VariableName, VariableName]]:
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        children: Iterator[ast.AST] = _own_scope_children(scope)
    else:
        children = ast.iter_child_nodes(scope)
    return [
        replacement
        for child in children
        for replacement in _collect_replacements(child, replace_names, has_future_annotations=has_future_annotations)
    ]


def _find_enclosing_function(tree: ast.Module, line: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end and (best is None or node.lineno > best.lineno):
            best = node
    return best


def _apply_fixes(
    filepath: Path,
    violations: list[MeaninglessVarsFixData],
    source: str,
    tree: ast.Module,
    *,
    ignored_lines: set[int],
    encoding: str = "utf-8",
) -> FixOutcome:
    lines = source.splitlines(keepends=True)
    has_future_annotations = _has_future_annotations_import(tree)

    violations_by_scope: dict[int | None, list[MeaninglessVarsFixData]] = {}
    scope_nodes: dict[int | None, ast.AST] = {}
    for v in violations:
        scope_node = _find_enclosing_function(tree, v["line"])
        scope_id = id(scope_node) if scope_node else None
        violations_by_scope.setdefault(scope_id, []).append(v)
        scope_nodes[scope_id] = scope_node or tree

    scope_replacements: dict[int | None, dict[VariableName, VariableName]] = {}
    for scope_id, scope_violations in violations_by_scope.items():
        replacements: dict[VariableName, VariableName] = {}
        for v in scope_violations:
            old_name = v["name"]
            new_name = v["suggestion"]
            assert new_name is not None
            replacements[old_name] = new_name
        scope_replacements[scope_id] = replacements

    all_replacements: list[tuple[int, int, VariableName, VariableName]] = []
    for scope_id, replacements in scope_replacements.items():
        collected = _collect_scope_replacements(
            scope_nodes[scope_id], replacements, has_future_annotations=has_future_annotations
        )
        by_old_name: dict[VariableName, list[tuple[int, int, VariableName, VariableName]]] = {}
        for item in collected:
            by_old_name.setdefault(item[2], []).append(item)
        for items in by_old_name.values():
            if any(line_num in ignored_lines for line_num, _col, _old, _new in items):
                continue
            all_replacements.extend(items)

    if not all_replacements:
        return FixOutcome.DECLINED

    all_replacements.sort(key=lambda x: (x[0], x[1]), reverse=True)

    for line_num, byte_col, old_name, new_name in all_replacements:
        line_idx = line_num - 1
        line = lines[line_idx]
        name_len = len(old_name)
        col = byte_col_to_char_col(line, byte_col)
        lines[line_idx] = line[:col] + new_name + line[col + name_len :]

    atomic_write_text(filepath, "".join(lines), encoding, source)
    return FixOutcome.APPLIED


class MeaninglessVarsCheck(BaseCheck):
    __slots__ = ("_level", "meaningless_names")

    OPTIONS: ClassVar[tuple[CheckOption, ...]] = (
        EnumOption(
            name="level",
            values=MeaninglessVarsLevel,
            default=MeaninglessVarsLevel.CONSERVATIVE,
            help=(
                "Whether meaningless-vars (TR1) reports a meaningless name "
                "that has no suggested replacement. 'conservative' "
                "(default) reports a name only when a rename can be "
                "suggested; 'permissive' reports every meaningless name "
                "regardless. --fix only ever applies a high-confidence "
                "suggestion at either level."
            ),
        ),
    )

    def __init__(self, level: MeaninglessVarsLevel = MeaninglessVarsLevel.CONSERVATIVE) -> None:
        self.meaningless_names = DEFAULT_MEANINGLESS_NAMES
        self._level = level

    @property
    def check_id(self) -> str:
        return "meaningless-vars"

    @property
    def error_code(self) -> str:
        return "TR1"

    def get_prefilter_pattern(self) -> list[str] | None:
        return sorted(self.meaningless_names)

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        return self._check(tree, source, collect_suppression_usage=False)

    def check_with_suppression_tracking(self, _filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        return self._check(tree, source, collect_suppression_usage=True)

    def _check(self, tree: ast.Module, source: str, *, collect_suppression_usage: bool) -> CheckResult:
        visitor = MeaninglessNameVisitor(self.meaningless_names, source)
        visitor.visit(tree)

        suppression_usages: list[SuppressionUsage] = []
        if visitor.violations:
            if collect_suppression_usage:
                ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(
                    source, IGNORE_PATTERN
                )
            else:
                ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
            raw_violations = [v for v in visitor.violations if v["line"] not in ignored_lines]
            suggestions = plan_suggestions(tree, self.meaningless_names, ignored_lines)

            suppressed_candidates = [v for v in visitor.violations if v["line"] in ignored_lines]
            if collect_suppression_usage and suppressed_candidates:
                all_suggestions = plan_suggestions(tree, self.meaningless_names, set())
                for candidate in suppressed_candidates:
                    proposal = all_suggestions.get((candidate["line"], candidate["byte_col"]))
                    if self._level is MeaninglessVarsLevel.CONSERVATIVE and proposal is None:
                        continue
                    record_suppression_usage_if_ignored(
                        suppression_usages,
                        comments,
                        ignored_lines=ignored_lines,
                        format_suppressed=format_suppressed,
                        check_id=self.check_id,
                        error_code=self.error_code,
                        candidate_lines=(candidate["line"],),
                    )
        else:
            raw_violations = []
            suggestions = {}

        violations = []
        for v in raw_violations:
            proposal = suggestions.get((v["line"], v["byte_col"]))
            if proposal is not None:
                v["suggestion"] = proposal.name
                v["auto_fixable"] = proposal.confidence is Confidence.AUTO_FIX
            if self._level is MeaninglessVarsLevel.CONSERVATIVE and not v["suggestion"]:
                continue
            if v.get("suggestion"):
                message = f"'{v['name']}' is a meaningless variable name — '{v['suggestion']}' is more descriptive."
            else:
                message = f"Meaningless variable name '{v['name']}' found. Use a more descriptive name."
            message += " Or add '# pytriage: TR1' to suppress."

            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=v["line"],
                    col=v["col"],
                    message=message,
                    fixable=v["auto_fixable"],
                    fix_data=cast("dict[str, Any]", v),
                )
            )

        return CheckResult(violations, suppression_usages)

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> FixResult:
        fixable_violations = [v for v in violations if v.fixable and v.fix_data]
        fixable = [cast("MeaninglessVarsFixData", v.fix_data) for v in fixable_violations]

        if not fixable:
            return FixResult.for_violations(violations, FixOutcome.DECLINED)

        fixable_ids = {id(violation) for violation in fixable_violations}

        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)

        try:
            outcome = _apply_fixes(filepath, fixable, source, tree, ignored_lines=ignored_lines, encoding=encoding)
        except OSError:
            logger.debug("Failed to apply fixes to %s", filepath, exc_info=True)
            return FixResult(
                tuple(
                    FixOutcome.FAILED if id(violation) in fixable_ids else FixOutcome.DECLINED
                    for violation in violations
                )
            )
        else:
            return FixResult(
                tuple(outcome if id(violation) in fixable_ids else FixOutcome.DECLINED for violation in violations)
            )
