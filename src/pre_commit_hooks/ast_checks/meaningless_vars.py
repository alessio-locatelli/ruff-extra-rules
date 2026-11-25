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
from ._references import (
    AnnotationReferences,
    QuotedAnnotationTooComplexError,
    class_bound_and_read_names,
    collect_scope_replacements,
    has_future_annotations_import,
)
from .meaningless_vars_suggestions import Confidence, plan_suggestions

if TYPE_CHECKING:
    from pathlib import Path

    from ._options import CheckOption

logger = logging.getLogger("meaningless_vars")

IGNORE_PATTERN = ignore_pattern_for("TR1")

type VariableName = str


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
    AGGRESSIVE = auto()


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
    has_future_annotations = has_future_annotations_import(tree)
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
    for scope_id, scope_names in scope_replacements.items():
        ambiguous = class_bound_and_read_names(scope_nodes[scope_id], scope_names)
        replacements = {old: new for old, new in scope_names.items() if old not in ambiguous}
        collected = collect_scope_replacements(
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

    lines = source.splitlines(keepends=True)
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
                "suggested; 'aggressive' reports every meaningless name "
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

        try:
            annotation_names = (
                AnnotationReferences(tree, has_future_annotations=has_future_annotations_import(tree)).ambiguous_names()
                if suggestions
                else set()
            )
        except QuotedAnnotationTooComplexError:
            annotation_names = None
        violations = []
        for v in raw_violations:
            proposal = suggestions.get((v["line"], v["byte_col"]))
            if proposal is not None:
                v["suggestion"] = proposal.name
                v["auto_fixable"] = (
                    proposal.confidence is Confidence.AUTO_FIX
                    and annotation_names is not None
                    and v["name"] not in annotation_names
                    and proposal.name not in annotation_names
                )
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
