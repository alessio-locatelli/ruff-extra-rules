from __future__ import annotations

import ast
import logging
from typing import TYPE_CHECKING

from ._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
    find_ignored_lines_and_pytriage_comments,
    ignore_pattern_for,
    record_suppression_usage_if_ignored,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("redundant_super_init")

IGNORE_PATTERN = ignore_pattern_for("TR3")


class SuperInitChecker(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[tuple[int, str]] = []
        self.classes: dict[str, ast.ClassDef] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes[node.name] = node

        init_method = None
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                init_method = item
                break

        if init_method:
            self._check_init_method(node, init_method)

        self.generic_visit(node)

    def _check_init_method(self, class_node: ast.ClassDef, init_node: ast.FunctionDef) -> None:
        has_kwargs = init_node.args.kwarg is not None
        if not has_kwargs:
            return

        for stmt in ast.walk(init_node):
            if not isinstance(stmt, ast.Call):
                continue

            if not _is_super_init_call(stmt):
                continue

            if not _forwards_kwargs(stmt):
                continue

            for base in class_node.bases:
                if isinstance(base, ast.Name):
                    parent = self.classes.get(base.id)
                    if parent and not _parent_accepts_args(parent, self.classes):
                        self.violations.append(
                            (
                                init_node.lineno,
                                (
                                    f"Redundant **kwargs forwarded to {base.id}.__init__() "
                                    "which accepts no arguments. Or add "
                                    "'# pytriage: TR3' to suppress."
                                ),
                            )
                        )


def _is_super_init_call(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False

    if node.func.attr != "__init__":
        return False

    if not isinstance(node.func.value, ast.Call):
        return False

    func = node.func.value.func
    return isinstance(func, ast.Name) and func.id == "super"


def _forwards_kwargs(node: ast.Call) -> bool:
    return any(keyword.arg is None for keyword in node.keywords)


def _parent_accepts_args(class_node: ast.ClassDef, classes: dict[str, ast.ClassDef]) -> bool:
    for item in class_node.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
            args = item.args
            if len(args.args) > 1:
                return True
            if args.vararg or args.kwarg:
                return True
            if args.kwonlyargs:
                return True
            return bool(args.posonlyargs and len(args.posonlyargs) > 1)

    for base in class_node.bases:
        if isinstance(base, ast.Name):
            if base.id in ("Exception", "BaseException"):
                return True
            parent = classes.get(base.id)
            if parent and _parent_accepts_args(parent, classes):
                return True
    return False


class RedundantSuperInitCheck(BaseCheck):
    __slots__ = ()

    @property
    def check_id(self) -> str:
        return "redundant-super-init"

    @property
    def error_code(self) -> str:
        return "TR3"

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["super().__init__"]

    def check(self, filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        checker = SuperInitChecker(str(filepath))
        checker.visit(tree)

        if not checker.violations:
            return CheckResult()

        ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(source, IGNORE_PATTERN)
        violations = []
        suppression_usages: list[SuppressionUsage] = []
        for line_num, message in checker.violations:
            if record_suppression_usage_if_ignored(
                suppression_usages,
                comments,
                ignored_lines=ignored_lines,
                format_suppressed=format_suppressed,
                check_id=self.check_id,
                error_code=self.error_code,
                candidate_lines=(line_num,),
            ):
                continue
            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=line_num,
                    col=0,  # This check doesn't track a specific column.
                    message=message,
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
