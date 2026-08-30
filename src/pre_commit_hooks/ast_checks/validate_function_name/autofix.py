from __future__ import annotations

import ast
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pre_commit_hooks.ast_checks._base import (
    ConcurrentModificationError,
    FixOutcome,
    FixValidationError,
    atomic_write_text,
    byte_col_to_char_col,
    find_ignored_lines,
    read_source_with_encoding,
)
from pre_commit_hooks.ast_checks._scope import iter_within_scope

from .analysis import IGNORE_PATTERN, Suggestion, attach_parents, read_source

logger = logging.getLogger("validate-function-name")

_FuncNode = ast.FunctionDef | ast.AsyncFunctionDef

type SourcePosition = tuple[int, int]
type RepositoryReferenceStatus = Literal["safe", "external", "unavailable"]


def _count_nesting_depth(func_node: _FuncNode) -> int:
    max_depth = 0
    stack: list[tuple[ast.AST, int]] = [(stmt, 0) for stmt in func_node.body]
    while stack:
        node, depth = stack.pop()
        max_depth = max(max_depth, depth)
        child_depth = depth + 1 if isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.Try)) else depth
        stack.extend((child, child_depth) for child in ast.iter_child_nodes(node))

    return max_depth


def _count_returns(func_node: _FuncNode) -> int:
    return sum(1 for node in ast.walk(func_node) if isinstance(node, ast.Return))


def _count_function_lines(func_node: _FuncNode) -> int:
    docstring_lines = 0
    if (
        func_node.body
        and isinstance(func_node.body[0], ast.Expr)
        and isinstance(func_node.body[0].value, ast.Constant)
        and isinstance(func_node.body[0].value.value, str)
    ):
        docstring_node = func_node.body[0]
        docstring_lines = docstring_node.end_lineno - docstring_node.lineno + 1  # type: ignore[operator]

    total_lines = func_node.end_lineno - func_node.lineno + 1  # type: ignore[operator]

    return total_lines - docstring_lines


def _find_function_node(tree: ast.Module, name: str, lineno: int) -> _FuncNode | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name and node.lineno == lineno:
            return node
    return None


type FunctionIndex = dict[tuple[str, int], _FuncNode]


def index_function_nodes(tree: ast.Module) -> FunctionIndex:
    return {
        (node.name, node.lineno): node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def should_autofix(filepath: Path, suggestion: Suggestion) -> FixOutcome:
    try:
        tree = ast.parse(read_source(filepath))
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as error:
        logger.warning("Filepath: %s. Error: %s", filepath, repr(error))
        return FixOutcome.FAILED

    attach_parents(tree)
    return FixOutcome.APPLIED if is_autofix_safe(index_function_nodes(tree), suggestion) else FixOutcome.DECLINED


def _repository_reference_status(filepath: Path, name: str) -> RepositoryReferenceStatus:
    git = shutil.which("git")
    if git is None:
        logger.warning("Could not safely search repository references because git is unavailable")
        return "unavailable"

    try:
        root_result = subprocess.run(
            [git, "-C", filepath.parent, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired:
        logger.warning("Could not safely establish repository scope for %s", filepath)
        return "unavailable"

    if root_result.returncode != 0 or root_result.stderr:
        if root_result.returncode == 128 and "not a git repository" in root_result.stderr:
            return "safe"
        logger.warning("Could not safely establish repository scope for %s", filepath)
        return "unavailable"

    root = Path(root_result.stdout.strip())
    try:
        reference_result = subprocess.run(
            [
                git,
                "-C",
                root,
                "grep",
                "--files-with-matches",
                "--null",
                "--untracked",
                "--fixed-strings",
                "-e",
                name,
                "--",
                ":(glob)**/*.py",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired:
        logger.warning("Could not safely search repository references for %s", name)
        return "unavailable"

    if reference_result.returncode not in (0, 1) or reference_result.stderr:
        logger.warning("Could not safely search repository references for %s", name)
        return "unavailable"

    if any((root / path).resolve() != filepath.resolve() for path in reference_result.stdout.split("\0") if path):
        return "external"
    return "safe"


def is_autofix_safe(function_index: FunctionIndex, suggestion: Suggestion) -> bool:
    if suggestion.reason == "no confident suggestion":
        return False

    func_node = function_index.get((suggestion.func_name, suggestion.lineno))
    if func_node is None:
        return False

    if isinstance(getattr(func_node, "parent", None), ast.ClassDef):
        return False

    line_count = _count_function_lines(func_node)
    if line_count >= 20:
        return False

    nesting = _count_nesting_depth(func_node)
    if nesting > 1:
        return False

    returns = _count_returns(func_node)
    return returns <= 1


def _def_name_position(lines: list[str], func_node: _FuncNode) -> SourcePosition:
    line_idx = func_node.lineno - 1
    assert line_idx < len(lines)

    pattern = re.compile(rf"\bdef\s+({re.escape(func_node.name)})\b")
    match = pattern.search(lines[line_idx], func_node.col_offset)
    assert match is not None

    return (func_node.lineno, match.start(1))


def _attr_name_position(node: ast.Attribute, lines: list[str]) -> SourcePosition:
    assert node.end_lineno is not None
    assert node.end_col_offset is not None
    line = lines[node.end_lineno - 1]
    char_end = byte_col_to_char_col(line, node.end_col_offset)
    return (node.end_lineno, char_end - len(node.attr))


def _binds_name(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str, target: ast.AST) -> bool:
    args = node.args
    all_args = [
        *args.args,
        *args.posonlyargs,
        *args.kwonlyargs,
        *([args.vararg] if args.vararg else []),
        *([args.kwarg] if args.kwarg else []),
    ]
    if any(arg.arg == name for arg in all_args):
        return True

    for child in iter_within_scope(node):
        if child is target:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == name:
            return True
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store) and child.id == name:
            return True
        if isinstance(child, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name) == name for alias in child.names
        ):
            return True
    return False


def _is_rebound_in_scope(scope_node: ast.AST, name: str, target: ast.AST) -> bool:
    for child in iter_within_scope(scope_node):
        if child is target:
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store) and child.id == name:
            return True
        if isinstance(child, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name) == name for alias in child.names
        ):
            return True
    return False


class _ReferenceCollector(ast.NodeVisitor):
    def __init__(self, old_name: str, target: ast.AST, lines: list[str], *, is_method: bool) -> None:
        self.old_name = old_name
        self.is_method = is_method
        self.target = target
        self.lines = lines
        self.positions: list[SourcePosition] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if not self.is_method and _binds_name(node, self.old_name, self.target):
            return
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if not self.is_method and _binds_name(node, self.old_name, self.target):
            return
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        args = node.args
        all_args = [
            *args.args,
            *args.posonlyargs,
            *args.kwonlyargs,
            *([args.vararg] if args.vararg else []),
            *([args.kwarg] if args.kwarg else []),
        ]
        if not self.is_method and any(arg.arg == self.old_name for arg in all_args):
            return
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            self.is_method
            and node.attr == self.old_name
            and isinstance(node.ctx, ast.Load)
            and _is_self_like_receiver(node.value)
        ):
            self.positions.append(_attr_name_position(node, self.lines))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not self.is_method and node.id == self.old_name and isinstance(node.ctx, ast.Load):
            # col_offset is a UTF-8 byte offset; convert to a character
            # offset before it's used to index into the line as a str.
            char_col = byte_col_to_char_col(self.lines[node.lineno - 1], node.col_offset)
            self.positions.append((node.lineno, char_col))
        self.generic_visit(node)


def _is_self_like_receiver(value: ast.expr) -> bool:
    """Whether an attribute's receiver refers to the current instance/class.

    Matches `self.x` and `cls.x` only. `super().x` is deliberately excluded:
    see `_ReferenceCollector`.
    """
    return isinstance(value, ast.Name) and value.id in ("self", "cls")


def _resolve_rename_scope(tree: ast.Module, func_node: _FuncNode) -> tuple[ast.AST, bool]:
    """Returns (scope_node, is_method) — scope_node is the enclosing class (for
    methods, so unrelated classes are never touched), the enclosing function
    (for nested/closure functions), or the whole module (for top-level
    functions). `tree` must already have parent links attached.
    """
    parent = getattr(func_node, "parent", None)
    if isinstance(parent, ast.ClassDef):
        return parent, True
    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return parent, False
    return tree, False


def apply_fix(filepath: Path, suggestion: Suggestion) -> FixOutcome:
    """AST-scoped rename: renames the function definition itself plus true
    call-site references (`Name`/`Attribute` nodes reached via normal AST
    traversal) within the scope the function is visible in. Never touches
    string/byte literals, comments, or identically-named symbols in
    unrelated scopes (e.g. a same-named method on a different class).
    """
    try:
        source, encoding = read_source_with_encoding(filepath)
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as error:
        logger.warning("Filepath: %s. Error: %s", filepath, repr(error))
        return FixOutcome.FAILED

    try:
        tree = ast.parse(source)
    except SyntaxError as syntax_error:
        logger.warning("Filepath: %s. Error: %s", filepath, repr(syntax_error))
        return FixOutcome.FAILED

    attach_parents(tree)

    func_node = _find_function_node(tree, suggestion.func_name, suggestion.lineno)
    if func_node is None:
        return FixOutcome.DECLINED

    lines = source.splitlines(keepends=True)

    positions: list[SourcePosition] = [_def_name_position(lines, func_node)]

    scope_node, is_method = _resolve_rename_scope(tree, func_node)

    # A reassignment (`get_data = fake`) or shadowing import anywhere in the
    # same scope means some Load references may no longer point at this
    # function; refuse to rename call sites we can't safely tell apart.
    if not is_method and _is_rebound_in_scope(scope_node, func_node.name, func_node):
        return FixOutcome.DECLINED

    collector = _ReferenceCollector(func_node.name, func_node, lines, is_method=is_method)
    collector.visit(scope_node)
    positions.extend(collector.positions)

    # Checked against every reference's own line too, not just the
    # definition line collect_suggestions() already gated on -- see
    # docs/adr/0050-format-suppression-pragmas.md.
    ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
    if any(line_num in ignored_lines for line_num, _col in positions):
        return FixOutcome.DECLINED

    old_name = func_node.name
    new_name = suggestion.suggested_name
    old_len = len(old_name)

    # Positions come from real ast.Name/Attribute nodes resolved against
    # this same tree/source, so line/col are always in range and the text
    # at each position always equals old_name.
    for line_num, col in sorted(set(positions), reverse=True):  # pytriage: TR6 -- dedupes, not redundant
        line_idx = line_num - 1
        line = lines[line_idx]
        lines[line_idx] = line[:col] + new_name + line[col + old_len :]

    # suggested_name is always != func_node.name (collect_suggestions only
    # emits a Suggestion when they differ), so replacing at least one
    # position always changes the source.
    new_source = "".join(lines)

    try:
        atomic_write_text(filepath, new_source, encoding, source)
    except OSError as error:
        logger.warning("Filepath: %s. Error: %s", filepath, repr(error))
        return FixOutcome.FAILED
    except FixValidationError:
        return FixOutcome.REJECTED
    except ConcurrentModificationError:
        return FixOutcome.ABORTED
    else:
        return FixOutcome.APPLIED
