"""Check for redundant **kwargs forwarding to parent __init__ methods.

TRI003: Detects when a class forwards **kwargs to a parent __init__ that
accepts no arguments. This is a logic error that creates misleading inheritance
patterns.

Inline ignore: # pytriage: ignore=TRI003, placed on the __init__ definition line.
"""

from __future__ import annotations

import ast
import logging

from ._base import BaseCheck, Violation, find_ignored_lines, ignore_pattern_for
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("redundant_super_init")

IGNORE_PATTERN = ignore_pattern_for("TRI003")


class SuperInitChecker(ast.NodeVisitor):
    """AST visitor to check for redundant super().__init__(**kwargs)."""

    def __init__(self, filename: str):
        self.filename = filename
        self.violations: list[tuple[int, str]] = []
        self.classes: dict[str, ast.ClassDef] = {}  # Track class definitions

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Store class for later lookup
        self.classes[node.name] = node

        # Find __init__ method
        init_method = None
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                init_method = item
                break

        if init_method:
            self._check_init_method(node, init_method)

        # Continue visiting child nodes
        self.generic_visit(node)

    def _check_init_method(
        self, class_node: ast.ClassDef, init_node: ast.FunctionDef
    ) -> None:
        # Check if __init__ has **kwargs parameter
        has_kwargs = init_node.args.kwarg is not None
        if not has_kwargs:
            return

        # Find super().__init__() calls in the __init__ method
        for stmt in ast.walk(init_node):
            if not isinstance(stmt, ast.Call):
                continue

            # Check if this is super().__init__() call
            if not _is_super_init_call(stmt):
                continue

            # Check if **kwargs is forwarded
            if not _forwards_kwargs(stmt):
                continue

            # Check parent signatures
            for base in class_node.bases:
                if isinstance(base, ast.Name):
                    parent = self.classes.get(base.id)
                    if parent and not _parent_accepts_args(parent, self.classes):
                        self.violations.append(
                            (
                                init_node.lineno,
                                f"Redundant **kwargs forwarded to {base.id}.__init__() "
                                "which accepts no arguments. Or add "
                                "'# pytriage: ignore=TRI003' to suppress.",
                            )
                        )


def _is_super_init_call(node: ast.Call) -> bool:
    # Check if func is Attribute with value=Call(super) and attr='__init__'
    if not isinstance(node.func, ast.Attribute):
        return False

    if node.func.attr != "__init__":
        return False

    # Check if the value is a super() call
    if not isinstance(node.func.value, ast.Call):
        return False

    func = node.func.value.func
    return isinstance(func, ast.Name) and func.id == "super"


def _forwards_kwargs(node: ast.Call) -> bool:
    # Check keywords for **kwargs (Starred node)
    return any(keyword.arg is None for keyword in node.keywords)


def _parent_accepts_args(
    class_node: ast.ClassDef, classes: dict[str, ast.ClassDef]
) -> bool:
    """Recursively traverses the inheritance chain to determine
    if any ancestor class accepts arguments through its __init__ method.

    Returns True if __init__ accepts arguments beyond self.
    """
    for item in class_node.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
            # Check if it has any parameters beyond 'self'
            args = item.args
            # Check positional arguments beyond 'self'
            if len(args.args) > 1:
                return True
            # Check for *args or **kwargs
            if args.vararg or args.kwarg:
                return True
            # Check for keyword-only arguments (e.g., *, key=None)
            if args.kwonlyargs:
                return True
            # Check for positional-only args (e.g., /, value)
            # Exclude 'self', so check for more than 1 posonly arg
            return bool(args.posonlyargs and len(args.posonlyargs) > 1)

    # No __init__ defined, recursively check parent classes
    for base in class_node.bases:
        if isinstance(base, ast.Name):
            # For built-in or imported types, we can't check further
            # but Exception and its subclasses accept **kwargs through BaseException
            if base.id in ("Exception", "BaseException"):
                return True
            # Recursively check user-defined parent classes
            parent = classes.get(base.id)
            if parent and _parent_accepts_args(parent, classes):
                return True
    return False


class RedundantSuperInitCheck(BaseCheck):
    """Check for redundant **kwargs forwarding to parent __init__."""

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
                    col=0,  # No specific column for this check
                    message=message,
                    fixable=False,  # No autofix support
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
