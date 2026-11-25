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
from pre_commit_hooks.ast_checks._references import (
    class_bound_and_read_names,
    collect_scope_replacements,
    has_future_annotations_import,
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


class _SelfAttributeCollector(ast.NodeVisitor):
    def __init__(self, old_name: str, lines: list[str]) -> None:
        self.old_name = old_name
        self.lines = lines
        self.positions: list[SourcePosition] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == self.old_name and isinstance(node.ctx, ast.Load) and _is_self_like_receiver(node.value):
            self.positions.append(_attr_name_position(node, self.lines))
        self.generic_visit(node)


def _name_reference_positions(
    tree: ast.Module, scope_node: ast.AST, name: str, lines: list[str]
) -> list[SourcePosition]:
    has_future_annotations = scope_node is not tree and has_future_annotations_import(tree)
    return [
        (line, byte_col_to_char_col(lines[line - 1], byte_col))
        for line, byte_col, _old, _new in collect_scope_replacements(
            scope_node, {name: name}, has_future_annotations=has_future_annotations
        )
    ]


def _is_self_like_receiver(value: ast.expr) -> bool:
    return isinstance(value, ast.Name) and value.id in ("self", "cls")


def _resolve_rename_scope(tree: ast.Module, func_node: _FuncNode) -> tuple[ast.AST, bool]:
    parent = getattr(func_node, "parent", None)
    if isinstance(parent, ast.ClassDef):
        return parent, True
    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return parent, False
    return tree, False


def apply_fix(filepath: Path, suggestion: Suggestion) -> FixOutcome:
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

    if any(
        isinstance(node, ast.Global | ast.Nonlocal) and func_node.name in node.names
        for node in (iter_within_scope(scope_node) if is_method else ast.walk(scope_node))
    ):
        return FixOutcome.DECLINED

    if not is_method and (
        _is_rebound_in_scope(scope_node, func_node.name, func_node)
        or class_bound_and_read_names(scope_node, [func_node.name])
    ):
        return FixOutcome.DECLINED

    if is_method:
        collector = _SelfAttributeCollector(func_node.name, lines)
        collector.visit(scope_node)
        positions.extend(collector.positions)
    else:
        positions.extend(_name_reference_positions(tree, scope_node, func_node.name, lines))

    ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
    if any(line_num in ignored_lines for line_num, _col in positions):
        return FixOutcome.DECLINED

    old_name = func_node.name
    new_name = suggestion.suggested_name
    old_len = len(old_name)

    for line_num, col in sorted(set(positions), reverse=True):
        line_idx = line_num - 1
        line = lines[line_idx]
        lines[line_idx] = line[:col] + new_name + line[col + old_len :]

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
