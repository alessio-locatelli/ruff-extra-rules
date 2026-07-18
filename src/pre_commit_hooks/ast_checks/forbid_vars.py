"""Check for forbidden meaningless variable names like 'data' and 'result'.

TRI001: Detects and suggests replacements for meaningless variable names that
reduce code maintainability.

Inline ignore: # pytriage: ignore=TRI001
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Any, TypedDict, cast, TYPE_CHECKING

from ._base import (
    BaseCheck,
    Violation,
    atomic_write_text,
    byte_col_to_char_col,
    find_ignored_lines,
    ignore_pattern_for,
)
from ._scope import collect_scope_names, iter_within_scope

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("forbid_vars")

# Regex pattern for inline ignore comments
# Format: # pytriage: ignore=TRI001
IGNORE_PATTERN = ignore_pattern_for("TRI001")


class ForbidVarsFixData(TypedDict):
    """Constructed by ForbiddenNameVisitor._check_name(), read back by fix()
    via _apply_fixes(). Must stay JSON-serializable — no AST node — since
    fix() re-resolves the enclosing scope from the fresh tree it's given
    instead (see _find_enclosing_function).
    """

    name: str
    line: int
    col: int
    suggestion: str | None


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


# Autofix patterns are always active and not user-configurable — see README.
AUTOFIX_PATTERNS = _compile_patterns(DEFAULT_AUTOFIX_PATTERNS)


class ForbiddenNameVisitor(ast.NodeVisitor):
    """AST visitor that detects forbidden variable names in Python code.

    This visitor checks all contexts where variables are defined and
    tries to find an autofix suggestion.
    """

    def __init__(
        self,
        forbidden_names: set[str],
        source: str,
    ) -> None:
        """Initialize the visitor.

        Args:
            forbidden_names: Set of variable names that are not allowed.
            source: The source code of the file being checked.
        """
        self.forbidden_names = forbidden_names
        self.source = source  # Store full source (optimization: avoid reconstruction)
        self.source_lines = source.splitlines()
        self.violations: list[ForbidVarsFixData] = []
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
        assert self.tree is not None, "tree must be set by check() before scope lookups"
        names = collect_scope_names(scope_node or self.tree)

        # Cache for future use
        self.scope_names_cache[scope_id] = names
        return names

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

        for patterns in AUTOFIX_PATTERNS.values():
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
            # fix_data (built from this dict) must stay serializable, so it
            # can't carry an AST node — fix() re-resolves the enclosing
            # scope from the fresh tree it's given instead (see
            # _find_enclosing_function).
            violation: ForbidVarsFixData = {
                "name": name,
                "line": lineno,
                "col": col_offset,
                "suggestion": None,
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
            # get_source_segment always resolves given a consistent
            # tree/source pair, which check() guarantees.
            assert rhs_source
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


def _collect_scope_replacements(
    scope: ast.AST, replace_names: dict[str, str]
) -> list[tuple[int, int, str, str]]:
    """Find (line, col, old_name, new_name) for every `Name` node within
    `scope` (not nested scopes) whose id is being replaced.

    `ast.arg` parameter bindings are a distinct node type from `ast.Name`,
    so a same-named parameter is never matched here — only actual variable
    references within the scope are.
    """
    return [
        (node.lineno, node.col_offset, node.id, replace_names[node.id])
        for node in iter_within_scope(scope)
        if isinstance(node, ast.Name) and node.id in replace_names
    ]


def _find_enclosing_function(
    tree: ast.Module, line: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find the innermost function containing `line`, resolved fresh against `tree`.

    CheckOrchestrator re-reads and re-parses the file before every check's
    fix() call (an earlier check's fix may have already changed it), so a
    scope captured during check() could belong to a different, stale tree
    object by the time fix() runs. Resolving from `line` against the tree
    fix() was actually given avoids that.
    """
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
    violations: list[ForbidVarsFixData],
    source: str,
    tree: ast.Module,
    encoding: str = "utf-8",
) -> None:
    """Apply autofixes by replacing forbidden variable assignments and their uses.

    This implementation is scope-aware: it groups violations by scope and replaces
    ALL uses of a variable within that scope, not just the assignment position.

    Args:
        filepath: Path to the file being fixed
        violations: List of violations with suggestions
        source: Original source code
        tree: Pre-parsed AST tree (optimization: avoid re-parsing)
        encoding: Encoding to write the file back with
    """
    lines = source.splitlines(keepends=True)

    # Step 1: Group violations by their enclosing scope. The caller
    # (ForbidVarsCheck.fix()) already filters to violations with a
    # suggestion and only calls here with a non-empty list.
    violations_by_scope: dict[int | None, list[ForbidVarsFixData]] = {}
    scope_nodes: dict[int | None, ast.AST] = {}
    for v in violations:
        scope_node = _find_enclosing_function(tree, v["line"])
        scope_id = id(scope_node) if scope_node else None
        violations_by_scope.setdefault(scope_id, []).append(v)
        scope_nodes[scope_id] = scope_node or tree

    # Step 2: Build scope-specific replacement mappings
    scope_replacements: dict[int | None, dict[str, str]] = {}
    for scope_id, scope_violations in violations_by_scope.items():
        replacements: dict[str, str] = {}
        for v in scope_violations:
            old_name = v["name"]
            new_name = v["suggestion"]
            # The caller only ever includes violations with a suggestion
            # (see the module docstring above); this narrows str | None to
            # str for the dict[str, str] below.
            assert new_name is not None
            if old_name not in replacements:
                # First violation of this name in this scope wins
                replacements[old_name] = new_name
        scope_replacements[scope_id] = replacements

    # Step 3: Collect replacements for each scope
    all_replacements: list[tuple[int, int, str, str]] = []
    for scope_id, replacements in scope_replacements.items():
        all_replacements.extend(
            _collect_scope_replacements(scope_nodes[scope_id], replacements)
        )

    # Step 4: Sort reverse and apply replacements
    all_replacements.sort(key=lambda x: (x[0], x[1]), reverse=True)

    # Positions come from real ast.Name nodes resolved against this same
    # tree/source, and are applied in descending (line, col) order so an
    # earlier (later-in-line) edit never shifts a not-yet-applied position
    # before it — so line/col are always in range, the text at each
    # position always equals old_name, and (since a Name node's span is
    # always a maximal tokenizer match) it's always on a word boundary.
    for line_num, byte_col, old_name, new_name in all_replacements:
        line_idx = line_num - 1
        line = lines[line_idx]
        name_len = len(old_name)
        # byte_col is a UTF-8 byte offset (from ast.col_offset); convert to a
        # character offset before indexing into `line` (a str).
        col = byte_col_to_char_col(line, byte_col)
        lines[line_idx] = line[:col] + new_name + line[col + name_len :]

    atomic_write_text(filepath, "".join(lines), encoding)


class ForbidVarsCheck(BaseCheck):
    """Check for forbidden meaningless variable names."""

    def __init__(self) -> None:
        """Initialize check."""
        self.forbidden_names = DEFAULT_FORBIDDEN_NAMES

    @property
    def check_id(self) -> str:
        return "forbid-vars"

    @property
    def error_code(self) -> str:
        return "TRI001"

    def get_prefilter_pattern(self) -> list[str] | None:
        """Returns all forbidden names as prefilter patterns."""
        return sorted(self.forbidden_names)

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        """Run check and return violations.

        Args:
            filepath: Path to file
            tree: Parsed AST tree
            source: Source code

        Returns:
            List of violations
        """
        visitor = ForbiddenNameVisitor(self.forbidden_names, source)
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
                    # Violation.fix_data is intentionally untyped (dict[str,
                    # Any]) at this boundary; see ForbidVarsFixData above for
                    # the shape check()/fix() actually agree on.
                    fix_data=cast("dict[str, Any]", v),
                )
            )

        return violations

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool:
        """Apply fixes for forbidden variable names.

        Args:
            filepath: Path to file
            violations: Violations to fix
            source: Source code
            tree: Parsed AST tree
            encoding: Encoding to write the file back with

        Returns:
            True if fixes were applied successfully
        """
        # Extract fixable violations with suggestions
        fixable = [
            cast("ForbidVarsFixData", v.fix_data)
            for v in violations
            if v.fixable and v.fix_data
        ]

        if not fixable:
            return False

        try:
            _apply_fixes(filepath, fixable, source, tree, encoding)
            return True
        except OSError as fix_error:
            logger.error("Failed to apply fixes to %s: %s", filepath, repr(fix_error))
            return False
