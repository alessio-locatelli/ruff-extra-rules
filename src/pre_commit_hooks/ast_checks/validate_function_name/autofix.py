"""Safe autofix implementation for function renames."""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from .._base import atomic_write_text, byte_col_to_char_col, read_source_with_encoding
from .._scope import iter_within_scope
from .analysis import Suggestion, attach_parents, read_source

logger = logging.getLogger("validate-function-name")

_FuncNode = ast.FunctionDef | ast.AsyncFunctionDef


def _count_nesting_depth(func_node: _FuncNode) -> int:
    """Calculate maximum nesting depth of control flow in function.

    Args:
        func_node: Function AST node

    Returns:
        Maximum nesting depth (0 = no nesting, 1 = single level, etc.)
    """
    max_depth = 0

    def _walk_depth(node: ast.AST, current_depth: int) -> None:
        nonlocal max_depth
        max_depth = max(max_depth, current_depth)

        # Increase depth for control flow structures
        if isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            for child in ast.iter_child_nodes(node):
                _walk_depth(child, current_depth + 1)
        else:
            for child in ast.iter_child_nodes(node):
                _walk_depth(child, current_depth)

    # Start from function body
    for stmt in func_node.body:
        _walk_depth(stmt, 0)

    return max_depth


def _count_returns(func_node: _FuncNode) -> int:
    """Count number of return statements in function.

    Args:
        func_node: Function AST node

    Returns:
        Number of return statements
    """
    return sum(1 for node in ast.walk(func_node) if isinstance(node, ast.Return))


def _count_function_lines(func_node: _FuncNode) -> int:
    """Count lines of code in function, excluding docstring.

    Args:
        func_node: Function AST node

    Returns:
        Number of lines (excluding docstring)
    """
    # Check if first statement is a docstring
    docstring_lines = 0
    if (
        func_node.body
        and isinstance(func_node.body[0], ast.Expr)
        and isinstance(func_node.body[0].value, ast.Constant)
        and isinstance(func_node.body[0].value.value, str)
    ):
        # Count docstring lines
        docstring_node = func_node.body[0]
        docstring_lines = docstring_node.end_lineno - docstring_node.lineno + 1  # type: ignore[operator]

    # Total function lines
    total_lines = func_node.end_lineno - func_node.lineno + 1  # type: ignore[operator]

    # Subtract docstring lines
    return total_lines - docstring_lines


def _find_function_node(tree: ast.Module, name: str, lineno: int) -> _FuncNode | None:
    """Find the function/async function definition matching name and line.

    Args:
        tree: Parsed AST tree
        name: Function name to find
        lineno: Line number the function is defined on

    Returns:
        The matching function node, or None if not found
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
            and node.lineno == lineno
        ):
            return node
    return None


def should_autofix(filepath: Path, suggestion: Suggestion) -> bool:
    """Determine if a suggestion is safe to auto-fix.

    Safe autofix criteria (ALL must be met):
    1. High confidence (not "no confident suggestion")
    2. Not a method (see below)
    3. Function is small (< 20 lines of code, excluding docstring)
    4. Simple control flow (max nesting depth ≤ 1)
    5. Single return point (at most one return statement)

    Methods are never auto-fixed: `apply_fix` can only find `self.x`/`cls.x`
    call sites within the same class body, not external calls through a
    differently-named receiver (e.g. `reader.get_report()` in a free
    function elsewhere in the file). Renaming the definition without being
    able to find every such call site would break real, unrenamed callers.

    Args:
        filepath: Path to the file containing the function
        suggestion: Naming suggestion to evaluate

    Returns:
        True if safe to auto-fix, False otherwise
    """
    # Check 1: Confidence
    if suggestion.reason == "no confident suggestion":
        return False

    # Parse file and find the function
    try:
        tree = ast.parse(read_source(filepath))
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as error:
        logger.warning("Filepath: %s. Error: %s", filepath, repr(error))
        return False

    func_node = _find_function_node(tree, suggestion.func_name, suggestion.lineno)
    if func_node is None:
        return False

    # Check 2: Not a method
    attach_parents(tree)
    if isinstance(getattr(func_node, "parent", None), ast.ClassDef):
        return False

    # Check 3: Size (< 20 lines excluding docstring)
    line_count = _count_function_lines(func_node)
    if line_count >= 20:
        return False

    # Check 4: Complexity (nesting depth ≤ 1)
    nesting = _count_nesting_depth(func_node)
    if nesting > 1:
        return False

    # Check 5: Single return (≤ 1 return statement)
    returns = _count_returns(func_node)
    return returns <= 1


def _def_name_position(lines: list[str], func_node: _FuncNode) -> tuple[int, int]:
    """Locate the exact (line, col) of the function name in its `def` statement.

    Searches only the `def`/`async def` line, starting at the node's own
    column offset, so it can never match text elsewhere in the file.

    Args:
        lines: Source lines (with line endings)
        func_node: Function AST node

    Returns:
        (1-indexed line number, 0-indexed column) of the name
    """
    line_idx = func_node.lineno - 1
    # AST line numbers are always valid for the source that produced them.
    assert line_idx < len(lines)

    pattern = re.compile(rf"\bdef\s+({re.escape(func_node.name)})\b")
    match = pattern.search(lines[line_idx], func_node.col_offset)
    # The def keyword and name are always present on their own line.
    assert match is not None

    return (func_node.lineno, match.start(1))


def _attr_name_position(node: ast.Attribute, lines: list[str]) -> tuple[int, int]:
    """Locate the (line, col) of the attribute name in `obj.attr`.

    Args:
        node: Attribute AST node
        lines: Source lines (with line endings), for byte-to-char conversion

    Returns:
        (1-indexed line number, 0-indexed column) of the attribute name
    """
    # Always populated for a real node from a parsed tree.
    assert node.end_lineno is not None
    assert node.end_col_offset is not None
    # end_col_offset is a UTF-8 byte offset; convert to a character offset
    # before subtracting the (character) length of the attribute name.
    line = lines[node.end_lineno - 1]
    char_end = byte_col_to_char_col(line, node.end_col_offset)
    return (node.end_lineno, char_end - len(node.attr))


def _binds_name(
    node: ast.FunctionDef | ast.AsyncFunctionDef, name: str, target: ast.AST
) -> bool:
    """Whether a function's own scope introduces a new binding for `name`.

    Conservative by design: covers a same-named parameter, a same-named
    nested def/class anywhere in the body (not crossing further nested scope
    boundaries), and any plain assignment to `name` anywhere in the body —
    matching real Python scoping, where a single assignment anywhere in a
    function makes that name local for the *entire* function. When this
    returns True, every reference to `name` inside the function refers to
    that local binding, not an outer scope's definition, so the whole
    function must be skipped.

    `target` (the function actually being renamed) is excluded from
    consideration: it may legitimately appear as a nested def matching
    `name` inside `node`'s body (when `node` is target's enclosing function),
    and that must not itself count as a shadow, or its own call sites within
    `node` would be wrongly skipped.
    """
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
        if (
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and child.name == name
        ):
            return True
        if (
            isinstance(child, ast.Name)
            and isinstance(child.ctx, ast.Store)
            and child.id == name
        ):
            return True
        if isinstance(child, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name) == name for alias in child.names
        ):
            return True
    return False


def _is_rebound_in_scope(scope_node: ast.AST, name: str, target: ast.AST) -> bool:
    """Whether `name` is rebound directly within `scope_node`'s own execution
    context (module or enclosing function), outside any nested scope.

    A reassignment like `get_data = fake` permanently rebinds the name for
    the rest of that scope's runtime lifetime (Python has no block scoping),
    so any `Load` reference could refer to the new value instead of the
    function being renamed. Detecting this precisely requires control-flow
    analysis this tool doesn't do, so when a rebinding is found anywhere in
    the scope, the caller should refuse to rename at all rather than risk
    renaming a reference that no longer points at the target function.
    """
    for child in iter_within_scope(scope_node):
        if child is target:
            continue
        if (
            isinstance(child, ast.Name)
            and isinstance(child.ctx, ast.Store)
            and child.id == name
        ):
            return True
        if isinstance(child, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name) == name for alias in child.names
        ):
            return True
    return False


class _ReferenceCollector(ast.NodeVisitor):
    """Collects exact source positions of true references to one function.

    Only ever visits `Name`/`Attribute` nodes reached via normal AST
    traversal, so it structurally cannot match text inside string/byte
    literals or comments. For methods, only `self.name`/`cls.name` accesses
    within the same class body are considered references, so identically
    named methods on unrelated classes are never touched. `super().name()`
    calls in subclasses are intentionally left alone: renaming them would be
    unsafe if the subclass overrides the method, since `self.name()` then
    resolves dynamically to the subclass's own (differently named) method.
    For free functions, a nested function/lambda that shadows the name (its
    own parameter, or any local binding to that name) is skipped entirely,
    so a same-named local helper's own call sites are never touched.
    """

    def __init__(
        self, old_name: str, is_method: bool, target: ast.AST, lines: list[str]
    ) -> None:
        self.old_name = old_name
        self.is_method = is_method
        self.target = target
        self.lines = lines
        self.positions: list[tuple[int, int]] = []

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
        if (
            not self.is_method
            and node.id == self.old_name
            and isinstance(node.ctx, ast.Load)
        ):
            # col_offset is a UTF-8 byte offset; convert to a character
            # offset before it's used to index into the line as a str.
            char_col = byte_col_to_char_col(
                self.lines[node.lineno - 1], node.col_offset
            )
            self.positions.append((node.lineno, char_col))
        self.generic_visit(node)


def _is_self_like_receiver(value: ast.expr) -> bool:
    """Whether an attribute's receiver refers to the current instance/class.

    Matches `self.x` and `cls.x` only. `super().x` is deliberately excluded:
    see `_ReferenceCollector`.
    """
    return isinstance(value, ast.Name) and value.id in ("self", "cls")


def _resolve_rename_scope(
    tree: ast.Module, func_node: _FuncNode
) -> tuple[ast.AST, bool]:
    """Determine the AST subtree in which call-site references may be renamed.

    Args:
        tree: Parsed AST tree (with parent links attached)
        func_node: The function being renamed

    Returns:
        (scope_node, is_method) — scope_node is the enclosing class (for
        methods, so unrelated classes are never touched), the enclosing
        function (for nested/closure functions), or the whole module (for
        top-level functions).
    """
    parent = getattr(func_node, "parent", None)
    if isinstance(parent, ast.ClassDef):
        return parent, True
    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return parent, False
    return tree, False


def apply_fix(filepath: Path, suggestion: Suggestion) -> bool:
    """Apply a rename fix to a file.

    Strategy: AST-scoped rename. Renames the function definition itself plus
    true call-site references (`Name`/`Attribute` nodes reached via normal
    AST traversal) within the scope the function is visible in. Never
    touches string/byte literals, comments, or identically-named symbols in
    unrelated scopes (e.g. a same-named method on a different class).

    Args:
        filepath: Path to the file to fix
        suggestion: Naming suggestion to apply

    Returns:
        True if fix was applied successfully, False otherwise
    """
    try:
        source, encoding = read_source_with_encoding(filepath)
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as error:
        logger.warning("Filepath: %s. Error: %s", filepath, repr(error))
        return False

    try:
        tree = ast.parse(source)
    except SyntaxError as syntax_error:
        logger.warning("Filepath: %s. Error: %s", filepath, repr(syntax_error))
        return False

    attach_parents(tree)

    func_node = _find_function_node(tree, suggestion.func_name, suggestion.lineno)
    if func_node is None:
        return False

    lines = source.splitlines(keepends=True)

    positions: list[tuple[int, int]] = [_def_name_position(lines, func_node)]

    scope_node, is_method = _resolve_rename_scope(tree, func_node)

    # A reassignment (`get_data = fake`) or shadowing import anywhere in the
    # same scope means some Load references may no longer point at this
    # function; refuse to rename call sites we can't safely tell apart.
    if not is_method and _is_rebound_in_scope(scope_node, func_node.name, func_node):
        return False

    collector = _ReferenceCollector(func_node.name, is_method, func_node, lines)
    collector.visit(scope_node)
    positions.extend(collector.positions)

    old_name = func_node.name
    new_name = suggestion.suggested_name
    old_len = len(old_name)

    # Positions come from real ast.Name/Attribute nodes resolved against
    # this same tree/source, so line/col are always in range and the text
    # at each position always equals old_name.
    for line_num, col in sorted(set(positions), reverse=True):
        line_idx = line_num - 1
        line = lines[line_idx]
        lines[line_idx] = line[:col] + new_name + line[col + old_len :]

    # suggested_name is always != func_node.name (collect_suggestions only
    # emits a Suggestion when they differ), so replacing at least one
    # position always changes the source.
    new_source = "".join(lines)

    try:
        atomic_write_text(filepath, new_source, encoding)
        return True
    except OSError as os_error:
        logger.warning("Filepath: %s. Error: %s", filepath, repr(os_error))
        return False
