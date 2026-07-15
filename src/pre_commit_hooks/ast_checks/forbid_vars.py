"""Check for forbidden meaningless variable names like 'data' and 'result'.

TRI001: Detects and suggests replacements for meaningless variable names that
reduce code maintainability.

Inline ignore: # pytriage: ignore=TRI001
"""

from __future__ import annotations

import ast
import logging
import re
import tomllib
from pathlib import Path
from typing import Any

from . import register_check
from ._base import Violation, find_ignored_lines

logger = logging.getLogger("forbid_vars")

# Regex pattern for inline ignore comments
# Format: # pytriage: ignore=TRI001
IGNORE_PATTERN = re.compile(r"#\s*pytriage:\s*ignore=TRI001", re.IGNORECASE)

# Default forbidden variable names
DEFAULT_FORBIDDEN_NAMES = {"data", "result"}

# Default autofix patterns
DEFAULT_AUTOFIX_PATTERNS = {
    "http": [
        {"regex": r"\.get\(.*\)", "name": "response"},
        {"regex": r"\.post\(.*\)", "name": "response"},
        {"regex": r"\.json\(\)", "name": "payload"},
    ],
    "file": [
        {"regex": r"open\(.*\)", "name": "file_handle"},
        {"regex": r"\.read_text\(.*\)", "name": "file_content"},
        {"regex": r"\.read\(.*\)", "name": "content"},
        {"regex": r"json\.load\(.*\)", "name": "parsed_data"},
    ],
    "database": [
        {"regex": r"\.execute\(.*\)", "name": "cursor"},
        {"regex": r"\.fetchall\(.*\)", "name": "rows"},
        {"regex": r"\.objects\.filter\(.*\)", "name": "queryset"},
        {"regex": r"\.objects\.get\(.*\)", "name": "instance"},
    ],
    "data-science": [
        {"regex": r"pd\.read_csv\(.*\)", "name": "df"},
        {"regex": r"np\.array\(.*\)", "name": "arr"},
        {"regex": r"re\.search\(.*\)", "name": "match"},
        {"regex": r"re\.findall\(.*\)", "name": "matches"},
    ],
    "semantic": [
        {"regex": r"get_([a-zA-Z0-9_]+)\(.*\)", "name": r"\1"},
        {"regex": r"find_([a-zA-Z0-9_]+)\(.*\)", "name": r"found_\1"},
        {"regex": r"create_([a-zA-Z0-9_]+)\(.*\)", "name": r"new_\1"},
    ],
}


def _compile_patterns(
    patterns_dict: dict[str, list[dict[str, str]]],
) -> dict[str, list[dict[str, Any]]]:
    """Pre-compile regex patterns for performance.

    Args:
        patterns_dict: Dictionary of pattern categories with regex strings

    Returns:
        Dictionary with compiled regex patterns
    """
    compiled: dict[str, list[dict[str, Any]]] = {}
    for category, patterns in patterns_dict.items():
        compiled[category] = []
        for pattern in patterns:
            compiled[category].append(
                {
                    "regex": pattern["regex"],  # Keep original for reference
                    "compiled": re.compile(pattern["regex"]),  # Pre-compiled
                    "name": pattern["name"],
                }
            )
    return compiled


def load_autofix_config() -> dict[str, Any]:
    """Load autofix configuration from pyproject.toml and pre-compile regex patterns.

    Returns:
        A dictionary containing the autofix configuration with pre-compiled regexes.
    """
    pyproject_path = Path("pyproject.toml")
    if not pyproject_path.exists():
        return {
            "patterns": _compile_patterns(DEFAULT_AUTOFIX_PATTERNS),
            "enabled": ["http"],
        }

    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)

    config = pyproject_data.get("tool", {}).get("forbid-vars", {}).get("autofix", {})

    # Combine default and custom patterns
    patterns = DEFAULT_AUTOFIX_PATTERNS.copy()
    custom_patterns = config.get("patterns", [])
    for custom_pattern in custom_patterns:
        category = custom_pattern.get("category")
        if category:
            if category not in patterns:
                patterns[category] = []
            patterns[category].append(
                {
                    "regex": custom_pattern["regex"],
                    "name": custom_pattern["name"],
                }
            )

    # Get enabled categories, default to http
    enabled = config.get("enabled", ["http"])

    # Pre-compile all regex patterns (performance optimization)
    return {"patterns": _compile_patterns(patterns), "enabled": enabled}


class ScopeVisitor(ast.NodeVisitor):
    """A visitor that collects all names in a scope (not nested scopes)."""

    def __init__(self, target_scope: ast.AST | None = None) -> None:
        self.names: set[str] = set()
        self.target_scope = target_scope
        self.in_target_scope = target_scope is None  # Module level = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Don't descend into nested function definitions."""
        if node is self.target_scope:
            # This is the target scope - enter it
            self.in_target_scope = True
            self.generic_visit(node)
            self.in_target_scope = False
        elif self.in_target_scope:
            # Nested function - don't descend (separate scope)
            pass
        else:
            # Different scope - don't descend
            pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Don't descend into nested async function definitions."""
        if node is self.target_scope:
            # This is the target scope - enter it
            self.in_target_scope = True
            self.generic_visit(node)
            self.in_target_scope = False
        elif self.in_target_scope:
            # Nested function - don't descend (separate scope)
            pass
        else:
            # Different scope - don't descend
            pass

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Don't descend into class definitions (separate scope)."""
        if self.in_target_scope:
            # Class inside function - don't descend
            pass
        else:
            # Continue visiting if we're looking for module-level names
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Collect name if we're in the target scope."""
        if self.in_target_scope:
            self.names.add(node.id)
        self.generic_visit(node)


class ForbiddenNameVisitor(ast.NodeVisitor):
    """AST visitor that detects forbidden variable names in Python code.

    This visitor checks all contexts where variables are defined and
    tries to find an autofix suggestion.
    """

    def __init__(
        self,
        forbidden_names: set[str],
        source: str,
        autofix_config: dict[str, Any],
        scope_names: set[str],
    ) -> None:
        """Initialize the visitor.

        Args:
            forbidden_names: Set of variable names that are not allowed.
            source: The source code of the file being checked.
            autofix_config: Configuration for the autofix feature.
            scope_names: All names defined in the current file's scope.
        """
        self.forbidden_names = forbidden_names
        self.source = source  # Store full source (optimization: avoid reconstruction)
        self.source_lines = source.splitlines()
        self.autofix_config = autofix_config
        self.scope_names = scope_names
        self.violations: list[dict[str, Any]] = []
        # Scope tracking for scope-aware name generation and replacement
        self.current_scope: list[ast.AST] = []
        self.tree: ast.Module | None = None
        self.scope_used_suggestions: dict[int | None, set[str]] = {}
        # Maps (scope_id, forbidden_var_name) to the generated suggestion
        self.scope_var_suggestions: dict[tuple[int | None, str], str] = {}
        # Cache scope names to avoid O(n²) repeated AST walks (performance optimization)
        self.scope_names_cache: dict[int | None, set[str]] = {}

    def _get_scope_names(self, scope_node: ast.AST | None) -> set[str]:
        """Collect all names defined in a specific scope only.

        Uses caching to avoid repeated AST walks for the same scope
        (performance optimization).

        Args:
            scope_node: The AST node representing the scope (function/class/module).
                       None means module-level scope.

        Returns:
            Set of all variable names defined in that scope.
        """
        scope_id = id(scope_node) if scope_node else None

        # Check cache first
        if scope_id in self.scope_names_cache:
            return self.scope_names_cache[scope_id]

        # Cache miss - compute scope names
        visitor = ScopeVisitor(target_scope=scope_node)
        if scope_node:
            visitor.visit(scope_node)
        elif self.tree:  # pragma: no cover (tree always set by check method)
            visitor.visit(self.tree)

        # Cache for future use
        self.scope_names_cache[scope_id] = visitor.names
        return visitor.names

    def _generate_unique_name(self, suggestion: str, forbidden_var_name: str) -> str:
        """Generate a unique variable name considering only the current scope.

        This ensures that variables with the same name in different functions
        don't get unnecessary suffixes (e.g., response_2, response_3).

        Args:
            suggestion: The suggested replacement name
            forbidden_var_name: The original forbidden variable name

        Returns:
            A unique name suitable for this scope
        """
        if suggestion in self.forbidden_names:
            suggestion = "var"  # Fallback

        # Get current scope
        scope_node = self.current_scope[-1] if self.current_scope else None
        scope_id = id(scope_node) if scope_node else None

        # Check if we already generated a suggestion for this variable in this scope
        cache_key = (scope_id, forbidden_var_name)
        if cache_key in self.scope_var_suggestions:
            return self.scope_var_suggestions[cache_key]

        # Get names in THIS scope only (not file-wide!)
        scope_names = self._get_scope_names(scope_node)

        # Track used suggestions in this scope
        if scope_id not in self.scope_used_suggestions:
            self.scope_used_suggestions[scope_id] = set()

        # Check conflicts - only add suffix if there's a conflict in THIS scope
        if (
            suggestion not in scope_names
            and suggestion not in self.scope_used_suggestions[scope_id]
        ):
            self.scope_used_suggestions[scope_id].add(suggestion)
            self.scope_var_suggestions[cache_key] = suggestion
            return suggestion

        # Generate with suffix (only if needed in this scope!)
        counter = 2
        while (
            f"{suggestion}_{counter}" in scope_names
            or f"{suggestion}_{counter}" in self.scope_used_suggestions[scope_id]
        ):
            counter += 1

        unique = f"{suggestion}_{counter}"
        self.scope_used_suggestions[scope_id].add(unique)
        self.scope_var_suggestions[cache_key] = unique
        return unique

    def _find_best_match(self, rhs_source: str) -> dict[str, Any] | None:
        """Find the best autofix pattern for a given RHS source."""
        best_match = None
        max_specificity = -1

        enabled_categories = self.autofix_config.get("enabled", [])
        all_patterns = self.autofix_config.get("patterns", {})

        for category in enabled_categories:
            patterns = all_patterns.get(category, [])
            for pattern in patterns:
                # Use pre-compiled regex (performance optimization)
                if pattern["compiled"].search(rhs_source):
                    specificity = len(pattern["regex"])
                    if specificity > max_specificity:
                        max_specificity = specificity
                        best_match = pattern
        return best_match

    def _check_name(
        self,
        name: str,
        lineno: int,
        col_offset: int,
        match: dict[str, Any] | None = None,
    ) -> None:
        """Check if a variable name is forbidden and record violation."""
        if name in self.forbidden_names:
            # Get current scope for scope-aware processing
            scope_node = self.current_scope[-1] if self.current_scope else None

            violation = {
                "name": name,
                "line": lineno,
                "col": col_offset,
                "suggestion": None,
                "scope_id": id(scope_node) if scope_node else None,
                "scope_node": scope_node,
            }
            if match:
                suggested_name = match["name"]
                # Handle semantic naming where name is from regex group
                if "\\" in suggested_name:
                    rhs_source = self.source_lines[lineno - 1]
                    regex_match = re.search(match["regex"], rhs_source)
                    if regex_match:
                        suggested_name = regex_match.expand(suggested_name)

                violation["suggestion"] = self._generate_unique_name(
                    suggested_name, name
                )
            self.violations.append(violation)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Visit regular assignment nodes: data = 1."""
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0]
            # Use pre-stored source instead of rebuilding (performance optimization)
            rhs_source = ast.get_source_segment(self.source, node.value)
            if rhs_source:
                match = self._find_best_match(rhs_source)
                self._check_name(target.id, target.lineno, target.col_offset, match)

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Visit annotated assignment nodes: data: int = 1."""
        if isinstance(node.target, ast.Name):
            if node.value:
                # Use pre-stored source instead of rebuilding (performance optimization)
                rhs_source = ast.get_source_segment(self.source, node.value)
                match = self._find_best_match(rhs_source) if rhs_source else None
            else:
                match = None

            self._check_name(
                node.target.id, node.target.lineno, node.target.col_offset, match
            )
        self.generic_visit(node)

    @staticmethod
    def _has_decorator_named(
        node: ast.FunctionDef | ast.AsyncFunctionDef, name: str
    ) -> bool:
        """Return True if the function has a decorator identified by *name*.

        Handles both bare decorators (``@model_validator``) and called
        decorators (``@model_validator(mode="before")``).
        """
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id == name:
                return True
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == name
            ):
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit function definition nodes: def foo(data):."""
        if not self._has_decorator_named(node, "model_validator"):
            self._check_function_args(node)
        # Push scope before visiting function body
        self.current_scope.append(node)
        self.generic_visit(node)
        # Pop scope after visiting function body
        self.current_scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Visit async function definition nodes: async def foo(data):."""
        if not self._has_decorator_named(node, "model_validator"):
            self._check_function_args(node)
        # Push scope before visiting function body
        self.current_scope.append(node)
        self.generic_visit(node)
        # Pop scope after visiting function body
        self.current_scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit class body but only descend into method definitions.

        Class-level attribute assignments (NamedTuple fields, dataclass fields,
        plain class attributes) are excluded because the class name provides
        sufficient context.  Method bodies ARE analysed — a 'result =' inside
        a test method is just as meaningless as one in a standalone function.
        """
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.visit(stmt)

    def _check_function_args(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """Check all function arguments for forbidden names."""
        for arg in node.args.args:
            self._check_name(arg.arg, arg.lineno, arg.col_offset)
        for arg in node.args.posonlyargs:
            self._check_name(arg.arg, arg.lineno, arg.col_offset)
        for arg in node.args.kwonlyargs:
            self._check_name(arg.arg, arg.lineno, arg.col_offset)
        if node.args.vararg:
            self._check_name(
                node.args.vararg.arg,
                node.args.vararg.lineno,
                node.args.vararg.col_offset,
            )
        if node.args.kwarg:
            self._check_name(
                node.args.kwarg.arg, node.args.kwarg.lineno, node.args.kwarg.col_offset
            )


class ScopedNameCollector(ast.NodeVisitor):
    """Collect Name nodes within a specific scope only (not nested scopes)."""

    def __init__(
        self, scope_node: ast.AST | None, replace_names: dict[str, str]
    ) -> None:
        self.scope_node = scope_node
        self.replace_names = replace_names  # {old_name: new_name}
        self.nodes_to_replace: list[tuple[int, int, str, str]] = []
        self.in_target_scope = scope_node is None  # Module-level = True
        self.param_positions: set[tuple[int, int]] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Handle function definitions - enter target scope, skip nested."""
        if node is self.scope_node:
            # Enter target scope
            self.in_target_scope = True
            # Mark parameters as restricted (don't replace parameter names)
            all_args = node.args.args + node.args.posonlyargs + node.args.kwonlyargs
            for arg in all_args:
                # pragma: lax no cover
                if arg.arg in self.replace_names:
                    self.param_positions.add((arg.lineno, arg.col_offset))
            # pragma: lax no cover
            if node.args.vararg and node.args.vararg.arg in self.replace_names:
                pos = (node.args.vararg.lineno, node.args.vararg.col_offset)
                self.param_positions.add(pos)
            # pragma: lax no cover
            if node.args.kwarg and node.args.kwarg.arg in self.replace_names:
                pos = (node.args.kwarg.lineno, node.args.kwarg.col_offset)
                self.param_positions.add(pos)
            self.generic_visit(node)
            self.in_target_scope = False
        # pragma: no cover (nested functions have separate scopes)
        elif self.in_target_scope:
            # Nested function - don't descend (separate scope)
            pass
        # pragma: no cover (different scope not visited)
        else:
            # Different scope - don't descend
            pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Handle async function definitions - same logic as visit_FunctionDef."""
        if node is self.scope_node:
            self.in_target_scope = True
            all_args = node.args.args + node.args.posonlyargs + node.args.kwonlyargs
            for arg in all_args:
                # pragma: lax no cover
                if arg.arg in self.replace_names:
                    self.param_positions.add((arg.lineno, arg.col_offset))
            # pragma: lax no cover
            if node.args.vararg and node.args.vararg.arg in self.replace_names:
                pos = (node.args.vararg.lineno, node.args.vararg.col_offset)
                self.param_positions.add(pos)
            # pragma: lax no cover
            if node.args.kwarg and node.args.kwarg.arg in self.replace_names:
                pos = (node.args.kwarg.lineno, node.args.kwarg.col_offset)
                self.param_positions.add(pos)
            self.generic_visit(node)
            self.in_target_scope = False
        # pragma: no cover (nested async functions have separate scopes)
        elif self.in_target_scope:
            pass
        # pragma: no cover (different scope not visited)
        else:
            pass

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Don't descend into class definitions (separate scope)."""
        # pragma: no cover (classes inside functions not typical)
        if self.in_target_scope:
            # Class inside function - don't descend
            pass
        else:
            # Continue visiting if we're looking for module-level names
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Collect Name node if we're in the target scope."""
        if self.in_target_scope and node.id in self.replace_names:
            pos = (node.lineno, node.col_offset)
            # pragma: lax no cover
            if pos not in self.param_positions:
                replacement = (
                    node.lineno,
                    node.col_offset,
                    node.id,
                    self.replace_names[node.id],
                )
                self.nodes_to_replace.append(replacement)
        self.generic_visit(node)


def _apply_fixes(
    filepath: Path, violations: list[dict[str, Any]], source: str, tree: ast.Module
) -> None:
    """Apply autofixes by replacing forbidden variable assignments and their uses.

    This implementation is scope-aware: it groups violations by scope and replaces
    ALL uses of a variable within that scope, not just the assignment position.

    Args:
        filepath: Path to the file being fixed
        violations: List of violations with suggestions
        source: Original source code
        tree: Pre-parsed AST tree (optimization: avoid re-parsing)
    """
    lines = source.splitlines(keepends=True)

    # Step 1: Group violations by scope
    violations_by_scope: dict[int | None, list[dict[str, Any]]] = {}
    for v in violations:
        if v.get("suggestion"):
            scope_id = v.get("scope_id")
            if scope_id not in violations_by_scope:
                violations_by_scope[scope_id] = []
            violations_by_scope[scope_id].append(v)

    # pragma: no cover (caller filters for fixable violations)
    if not violations_by_scope:
        return

    # Step 2: Build scope-specific replacement mappings
    scope_replacements: dict[int | None, dict[str, str]] = {}
    for scope_id, scope_violations in violations_by_scope.items():
        replacements: dict[str, str] = {}
        for v in scope_violations:
            old_name = v["name"]
            new_name = v["suggestion"]
            if old_name not in replacements:
                # First violation of this name in this scope wins
                replacements[old_name] = new_name
        scope_replacements[scope_id] = replacements

    # Step 3: Collect replacements for each scope
    all_replacements: list[tuple[int, int, str, str]] = []
    for scope_id, replacements in scope_replacements.items():
        # Find scope node from first violation
        scope_node = None
        for v in violations_by_scope[scope_id]:  # pragma: lax no cover
            scope_node = v.get("scope_node")
            break

        # Collect Name nodes in this scope
        collector = ScopedNameCollector(scope_node, replacements)
        if scope_node:  # pragma: lax no cover
            collector.visit(scope_node)
        else:  # pragma: lax no cover
            collector.visit(tree)
        all_replacements.extend(collector.nodes_to_replace)

    # Step 4: Sort reverse and apply replacements
    all_replacements.sort(key=lambda x: (x[0], x[1]), reverse=True)

    for line_num, col, old_name, new_name in all_replacements:
        line_idx = line_num - 1
        if line_idx >= len(lines):  # pragma: no cover (AST line numbers always valid)
            continue

        line = lines[line_idx]
        name_len = len(old_name)

        # Bounds check
        # pragma: no cover (AST columns always valid)
        if col >= len(line) or col + name_len > len(line):
            continue

        # Verify the name matches at this position
        # pragma: no cover (AST positions always match)
        if line[col : col + name_len] != old_name:
            continue

        # Check word boundaries
        before_ok = col == 0 or not (line[col - 1].isalnum() or line[col - 1] == "_")
        after_ok = col + name_len >= len(line) or not (
            line[col + name_len].isalnum() or line[col + name_len] == "_"
        )

        if before_ok and after_ok:  # pragma: lax no cover
            lines[line_idx] = line[:col] + new_name + line[col + name_len :]

    filepath.write_text("".join(lines), encoding="utf-8")


@register_check
class ForbidVarsCheck:
    """Check for forbidden meaningless variable names."""

    def __init__(self, forbidden_names: set[str] | None = None) -> None:
        """Initialize check.

        Args:
            forbidden_names: Set of forbidden variable names (default: data, result)
        """
        self.forbidden_names = forbidden_names or DEFAULT_FORBIDDEN_NAMES
        self.autofix_config = load_autofix_config()

    @property
    def check_id(self) -> str:
        return "forbid-vars"

    @property
    def error_code(self) -> str:
        return "TRI001"

    def get_prefilter_pattern(self) -> list[str] | None:
        """Returns all forbidden names as prefilter patterns."""
        if self.forbidden_names:
            return sorted(self.forbidden_names)
        # pragma: no cover (constructor defaults to DEFAULT_FORBIDDEN_NAMES)
        return None

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        """Run check and return violations.

        Args:
            filepath: Path to file
            tree: Parsed AST tree
            source: Source code

        Returns:
            List of violations
        """
        scope_visitor = ScopeVisitor()
        scope_visitor.visit(tree)
        scope_names = scope_visitor.names

        visitor = ForbiddenNameVisitor(
            self.forbidden_names, source, self.autofix_config, scope_names
        )
        visitor.tree = tree  # Store tree for scope-aware name generation
        visitor.visit(tree)

        # Lazy tokenization: only tokenize if violations found
        if visitor.violations:
            ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
            raw_violations = [
                v for v in visitor.violations if v["line"] not in ignored_lines
            ]
        else:
            raw_violations = []

        # Convert to Violation objects
        violations = []
        for v in raw_violations:
            message = f"Forbidden variable name '{v['name']}' found."
            if v.get("suggestion"):
                message += f" Consider renaming to '{v['suggestion']}'."
            else:
                message += " Use a more descriptive name."
            message += " Or add '# pytriage: ignore=TRI001' to suppress."

            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=v["line"],
                    col=v["col"],
                    message=message,
                    fixable=bool(v.get("suggestion")),
                    fix_data=v,  # Store full violation data for fixing
                )
            )

        return violations

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
    ) -> bool:
        """Apply fixes for forbidden variable names.

        Args:
            filepath: Path to file
            violations: Violations to fix
            source: Source code
            tree: Parsed AST tree

        Returns:
            True if fixes were applied successfully
        """
        # Extract fixable violations with suggestions
        fixable = [v.fix_data for v in violations if v.fixable and v.fix_data]

        if not fixable:
            return False

        try:
            _apply_fixes(filepath, fixable, source, tree)
            return True
        except Exception as fix_error:  # noqa: BLE001  # pragma: no cover (defensive error handling)
            logger.error("Failed to apply fixes to %s: %s", filepath, repr(fix_error))
            return False
