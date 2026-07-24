"""Check for redundant **kwargs forwarding to parent __init__ methods.

TRI003: Detects when a class forwards **kwargs to a parent __init__ that
accepts no arguments. This is a logic error that creates misleading inheritance
patterns.

Inline ignore: # pytriage: ignore=TRI003, placed on the __init__ definition line.
"""

from __future__ import annotations

import ast
import logging
from typing import TYPE_CHECKING

from ._base import BaseCheck, Violation, find_ignored_lines, ignore_pattern_for

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("redundant_super_init")

IGNORE_PATTERN = ignore_pattern_for("TRI003")


class SuperInitChecker(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[tuple[int, str]] = []
        self.classes: dict[str, ast.ClassDef] = {}  # Track class definitions

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
                                    "'# pytriage: ignore=TRI003' to suppress."
                                ),
                            )
                        )


def _is_super_init_call(node: ast.Call) -> bool:
    """True for `super().__init__(...)`."""
    if not isinstance(node.func, ast.Attribute):
        return False

    if node.func.attr != "__init__":
        return False

    if not isinstance(node.func.value, ast.Call):
        return False

    func = node.func.value.func
    return isinstance(func, ast.Name) and func.id == "super"


def _forwards_kwargs(node: ast.Call) -> bool:
    # **kwargs forwarding is a `keyword` node with `arg=None` (not
    # `ast.Starred`, which is only for bare `*args` unpacking).
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
            # `self` only lands in posonlyargs when the signature marks it
            # positional-only too (e.g. `def __init__(self, /, x)`), so a
            # lone posonly entry there is just `self`, not a real parameter.
            return bool(args.posonlyargs and len(args.posonlyargs) > 1)

    # No __init__ found on this class itself; fall back to its bases.
    for base in class_node.bases:
        if isinstance(base, ast.Name):
            # Can't inspect a built-in/imported type's own __init__, but
            # Exception and its subclasses accept **kwargs through BaseException.
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
        return "TRI003"

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["super().__init__"]

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        checker = SuperInitChecker(str(filepath))
        checker.visit(tree)

        if not checker.violations:
            return []

        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
        violations = []
        for line_num, message in checker.violations:
            if line_num in ignored_lines:
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

        return violations

    def fix(
        self,
        _filepath: Path,
        _violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> bool:
        """No autofix support."""
        return False
