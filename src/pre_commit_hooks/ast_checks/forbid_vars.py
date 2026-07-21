"""Check for forbidden meaningless variable names like 'data' and 'result'.

TRI001: Detects and suggests replacements for meaningless variable names that
reduce code maintainability.

Inline ignore: # pytriage: ignore=TRI001
"""

from __future__ import annotations

import ast
import logging
import re
from typing import TYPE_CHECKING, Any, TypedDict, cast

from ._base import (
    BaseCheck,
    Violation,
    atomic_write_text,
    byte_col_to_char_col,
    fast_get_source_segment,
    find_ignored_lines,
    ignore_pattern_for,
    mark_fix_failed,
    split_lines_like_ast,
)
from ._scope import iter_within_scope_from

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger("forbid_vars")

# Format: # pytriage: ignore=TRI001
IGNORE_PATTERN = ignore_pattern_for("TRI001")

type VariableName = str
type _ComprehensionNode = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp


class ForbidVarsFixData(TypedDict):
    """Constructed by ForbiddenNameVisitor._check_name(), read back by fix()
    via _apply_fixes(). Must stay JSON-serializable — no AST node — since
    fix() re-resolves the enclosing scope from the fresh tree it's given
    instead (see _find_enclosing_function).
    """

    name: VariableName
    line: int
    col: int
    suggestion: VariableName | None


DEFAULT_FORBIDDEN_NAMES = {"data", "result"}

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
    """Pre-compiles each pattern's regex once, up front, instead of on every match attempt."""
    compiled: dict[str, list[dict[str, Any]]] = {}
    for category, patterns in patterns_dict.items():
        compiled[category] = []
        for pattern in patterns:
            compiled[category].append(
                {
                    "regex": pattern["regex"],  # Keep original for reference
                    "compiled": re.compile(pattern["regex"]),
                    "name": pattern["name"],
                }
            )
    return compiled


# Autofix patterns are always active and not user-configurable — see README.
AUTOFIX_PATTERNS = _compile_patterns(DEFAULT_AUTOFIX_PATTERNS)


class ForbiddenNameVisitor(ast.NodeVisitor):
    """Detects forbidden variable names in every context where a variable is
    defined, and tries to find an autofix suggestion.
    """

    def __init__(
        self,
        forbidden_names: set[VariableName],
        source: str,
    ) -> None:
        self.forbidden_names = forbidden_names
        self.source = source
        self.source_lines = source.splitlines()
        # For fast_get_source_segment only: split on the same line
        # boundaries ast's own lineno/end_lineno use, unlike
        # self.source_lines above (see split_lines_like_ast).
        self._ast_lines = split_lines_like_ast(source)
        self.violations: list[ForbidVarsFixData] = []
        self.current_scope: list[ast.AST] = []
        self.tree: ast.Module | None = None
        self.has_future_annotations = False
        self._annotation_names_cache: set[VariableName] | None = None
        self.scope_used_suggestions: dict[int | None, set[str]] = {}
        # Maps (scope_id, forbidden_var_name) to the generated suggestion
        self.scope_var_suggestions: dict[tuple[int | None, str], str] = {}
        # Cache scope names to avoid O(n²) repeated AST walks (performance optimization)
        self.scope_names_cache: dict[int | None, set[str]] = {}
        # Cache global/nonlocal-declared names to avoid O(n²) repeated AST walks (performance optimization)
        self.global_nonlocal_names_cache: dict[int | None, set[str]] = {}
        # Fixable violations found during visit(), queued here rather than
        # given a suggestion immediately — see assign_suggestions().
        self._pending_suggestions: list[tuple[ForbidVarsFixData, VariableName, VariableName, list[ast.AST]]] = []

    def assign_suggestions(self) -> None:
        """Generate a suggestion for every fixable violation found during
        `visit()`, in ascending scope-depth order (module scope first, most
        deeply nested scope last) rather than the AST's own textual/visit
        order.

        `_generate_unique_name()` avoids a name already claimed by an
        *ancestor* scope's own violation, since `fix()`'s closure-following
        rename can land an ancestor's rename inside a descendant scope too
        (`_ancestor_used_suggestions`). That check only works if the
        ancestor's own suggestion has already been chosen by the time a
        descendant scope's violation asks — true when the ancestor's own
        violation happens to appear earlier in the source, but *not* when a
        nested closure is defined *before* the variable it will eventually
        capture is assigned (valid Python: closures resolve names at call
        time, not definition time). Processing strictly by ascending scope
        depth instead of visit order guarantees every ancestor of a given
        scope — which by definition has a strictly smaller depth — has
        already had all of its own violations assigned a suggestion,
        regardless of where either one appears in the source.

        Depth ties (siblings, or violations in the same scope) keep their
        original relative order (`sorted` is stable), which matches the
        previous behavior for same-scope name suffixing (`payload`,
        `payload_2`, ...) exactly.
        """
        for violation, suggested_name, forbidden_name, scope_stack in sorted(
            self._pending_suggestions, key=lambda pending: len(pending[3])
        ):
            violation["suggestion"] = self._generate_unique_name(suggested_name, forbidden_name, scope_stack)

    def _get_scope_names(self, scope_node: ast.AST | None) -> set[VariableName]:
        """`scope_node=None` means module-level scope. Cached to avoid repeated AST walks for the same scope.

        Deliberately walks the *entire* subtree — including every nested
        function/lambda/comprehension, unlike `collect_scope_names()`'s own
        immediate-scope-only traversal — since `fix()`'s closure-following
        rename (`_collect_replacements()`) can propagate a suggestion into
        any non-shadowing nested scope. A suggestion that's merely unique
        within `scope_node`'s own immediate names could still collide with
        an unrelated name (or another violation's own independently
        generated suggestion) that lives in one of those nested scopes —
        this errs conservative (occasionally avoiding a name a rename would
        never actually reach) rather than risk two different renames
        landing on the same identifier in the same scope.

        Also includes every *non*-`ast.Name` binding a nested scope can
        introduce (`ast.arg`, `except ... as`, match captures, type
        parameters, `def`/`class`/import names — the same set
        `_binds_name_in_nested_scope` treats as shadowing), not just plain
        `ast.Name` references: a suggestion equal to a nested function's own
        *parameter* name (never itself an `ast.Name` node) would otherwise
        go undetected here, silently rebinding a closure read to that
        parameter once the rename lands inside the nested function's body.

        Also reserves every name declared in a `global`/`nonlocal`
        statement anywhere in the subtree (`_global_or_nonlocal_names`): a
        suggestion equal to one would, once the closure-following rename
        reaches that nested scope, turn what used to be a closure read into
        a lookup of that unrelated global/nonlocal binding instead — same
        failure class as the parameter collision above, just via a name
        that isn't a binding construct at all, only a redirection to one
        that lives elsewhere.
        """
        scope_id = id(scope_node) if scope_node else None

        if scope_id in self.scope_names_cache:
            return self.scope_names_cache[scope_id]

        assert self.tree is not None, "tree must be set by check() before scope lookups"
        names: set[VariableName] = set()
        for node in ast.walk(scope_node or self.tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
                if node.name is not None:
                    names.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest is not None:
                names.add(node.rest)
            elif isinstance(node, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple):
                names.add(node.name)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.split(".")[0])
        names |= self._global_or_nonlocal_names(scope_node)

        self.scope_names_cache[scope_id] = names
        return names

    def _global_or_nonlocal_names(self, scope_node: ast.AST | None) -> set[VariableName]:
        """Every name declared in a `global`/`nonlocal` statement anywhere
        within `scope_node`'s subtree, at any nesting depth. Cached per
        scope like `_get_scope_names`, since `_check_name()` calls
        `_referenced_via_global_or_nonlocal()` once per violation in the
        scope — without caching, a scope with many matching violations
        re-walks its own subtree once per violation.
        """
        scope_id = id(scope_node) if scope_node else None

        if scope_id in self.global_nonlocal_names_cache:
            return self.global_nonlocal_names_cache[scope_id]

        assert self.tree is not None, "tree must be set by check() before scope lookups"
        names: set[VariableName] = set()
        for node in ast.walk(scope_node or self.tree):
            if isinstance(node, ast.Global | ast.Nonlocal):
                names.update(node.names)

        self.global_nonlocal_names_cache[scope_id] = names
        return names

    def _referenced_via_global_or_nonlocal(self, name: VariableName) -> bool:
        """Whether `name` is mentioned in a `global`/`nonlocal` statement
        anywhere within the current scope's subtree, at any nesting depth.

        A `global`/`nonlocal` declaration stores its name as a plain string
        (`ast.Global.names`/`ast.Nonlocal.names`), not an `ast.Name` node,
        so `fix()`'s rename has no position there it could safely rewrite —
        renaming everything else while leaving that declaration stale can
        silently misdirect a later read (`NameError`) or, worse, a later
        write (creating an unrelated new local/nonlocal binding instead of
        mutating the variable the declaration named, with no error at all).
        Refusing to suggest a fix at all for such a violation, checked here
        at detection time so `fixable` stays honest, avoids the whole
        class rather than trying to patch each such construct safely.
        """
        scope_node = self.current_scope[-1] if self.current_scope else self.tree
        return name in self._global_or_nonlocal_names(scope_node)

    def _annotation_referenced_names(self) -> set[VariableName]:
        """Every identifier referenced anywhere in the module within a
        parameter or return annotation expression, cached once (module-wide,
        not per-scope: under PEP 563 every annotation resolves the same way
        regardless of how deeply it's nested — see
        `_unsafe_module_scope_annotation_reference`).
        """
        if self._annotation_names_cache is not None:
            return self._annotation_names_cache

        assert self.tree is not None, "tree must be set by check() before scope lookups"
        names: set[VariableName] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for annotation in _signature_annotations(node.args):
                    names.update(child.id for child in ast.walk(annotation) if isinstance(child, ast.Name))
                if node.returns is not None:
                    names.update(child.id for child in ast.walk(node.returns) if isinstance(child, ast.Name))

        self._annotation_names_cache = names
        return names

    def _unsafe_module_scope_annotation_reference(self, name: VariableName) -> bool:
        """Whether renaming a module-scope `name` is unsafe because some
        annotation elsewhere in the module also mentions it.

        Under PEP 563 (`from __future__ import annotations`), every
        annotation is stored as a string and resolved later only against the
        annotated function's own module globals — *never* against any
        enclosing scope's locals, and *ignoring* any local shadowing along
        the way (confirmed against real `typing.get_type_hints()` behavior:
        a nested function's own local of the same name has no effect on
        what its annotations resolve to). So a rename of a module-level
        binding should, in principle, follow into every annotation
        referencing it, at any nesting depth, regardless of intervening
        shadowing — a fundamentally different resolution rule than the
        shadow-respecting closure-following `_collect_replacements()` uses
        for ordinary references. Building a second, annotation-specific
        traversal that ignores shadowing (while everything else respects
        it) is disproportionate for how rarely a fixable violation's RHS
        pattern (e.g. `.json()`) would plausibly double as a type
        elsewhere, so this refuses to suggest a fix instead, the same
        "don't touch what we can't safely follow" treatment already given
        to `global`/`nonlocal`. Only relevant for a module-scope violation:
        a nested scope's own local is already never followed into any
        annotation under deferred annotations regardless (see
        `_outer_scope_children`), so no annotation there could ever
        possibly resolve to it in the first place.
        """
        return not self.current_scope and self.has_future_annotations and name in self._annotation_referenced_names()

    def _ancestor_used_suggestions(self, scope_stack: list[ast.AST]) -> set[VariableName]:
        """Union of every already-used suggestion in the current scope's own
        bucket and every *ancestor* scope's bucket (module scope, then each
        enclosing function, down to the immediate one) — `scope_stack` is a
        snapshot of `current_scope` from the point a violation was found
        (see `assign_suggestions`), not necessarily the live stack.

        A suggestion chosen for an ancestor scope's own violation can still
        end up inside a scope nested within it, via `fix()`'s
        closure-following rename (`_binds_name_in_nested_scope`) — so a
        violation in this scope must avoid reusing a suggestion an ancestor
        already claimed, or the two independently-renamed variables would
        collide into the same identifier once `fix()` actually runs. This
        only works because `assign_suggestions()` processes every scope's
        violations in ascending depth order: an ancestor (strictly shallower
        than this scope) has always already had its own suggestions chosen
        by the time this is called, regardless of which one appears earlier
        in the source.
        """
        used = set(self.scope_used_suggestions.get(None, set()))
        for ancestor in scope_stack:
            used |= self.scope_used_suggestions.get(id(ancestor), set())
        return used

    def _generate_unique_name(
        self, suggestion: VariableName, forbidden_var_name: VariableName, scope_stack: list[ast.AST]
    ) -> VariableName:
        """Considers only the current scope (plus its ancestors, see
        `_ancestor_used_suggestions`), so variables with the same name in
        unrelated, non-nested functions don't get unnecessary suffixes
        (e.g., response_2, response_3). `scope_stack` is a snapshot of
        `current_scope` from the point the violation was found.
        """
        if suggestion in self.forbidden_names:
            suggestion = "var"

        scope_node = scope_stack[-1] if scope_stack else None
        scope_id = id(scope_node) if scope_node else None

        cache_key = (scope_id, forbidden_var_name)
        if cache_key in self.scope_var_suggestions:
            return self.scope_var_suggestions[cache_key]

        # Names anywhere in this scope's own subtree (crossing into nested
        # scopes too — see _get_scope_names).
        scope_names = self._get_scope_names(scope_node)
        ancestor_used = self._ancestor_used_suggestions(scope_stack)

        if scope_id not in self.scope_used_suggestions:
            self.scope_used_suggestions[scope_id] = set()

        if (
            suggestion not in scope_names
            and suggestion not in self.scope_used_suggestions[scope_id]
            and suggestion not in ancestor_used
        ):
            self.scope_used_suggestions[scope_id].add(suggestion)
            self.scope_var_suggestions[cache_key] = suggestion
            return suggestion

        counter = 2
        while (
            f"{suggestion}_{counter}" in scope_names
            or f"{suggestion}_{counter}" in self.scope_used_suggestions[scope_id]
            or f"{suggestion}_{counter}" in ancestor_used
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
                if pattern["compiled"].search(rhs_source):
                    specificity = len(pattern["regex"])
                    if specificity > max_specificity:
                        max_specificity = specificity
                        best_match = pattern
        return best_match

    def _check_name(
        self,
        name: VariableName,
        lineno: int,
        col_offset: int,
        match: dict[str, Any] | None = None,
    ) -> None:
        if name in self.forbidden_names:
            # fix_data (built from this dict) must stay serializable, so it
            # can't carry an AST node — fix() re-resolves the enclosing
            # scope from the fresh tree it's given instead (see
            # _find_enclosing_function).
            violation: ForbidVarsFixData = {
                "name": name,
                "line": lineno,
                # col_offset is a UTF-8 byte offset (from ast.col_offset);
                # the reported diagnostic column is a character offset
                # (matching misplaced-comment's own tokenize-derived
                # column). This is the only place "col" feeds into —
                # _apply_fixes()/_collect_scope_replacements() below always
                # re-derive their own edit positions fresh from the tree,
                # never from this stored value.
                "col": byte_col_to_char_col(self._ast_lines[lineno - 1], col_offset),
                "suggestion": None,
            }
            if (
                match
                and not self._referenced_via_global_or_nonlocal(name)
                and not self._unsafe_module_scope_annotation_reference(name)
            ):
                suggested_name = match["name"]
                # Handle semantic naming where name is from regex group
                if "\\" in suggested_name:
                    rhs_source = self.source_lines[lineno - 1]
                    regex_match = re.search(match["regex"], rhs_source)
                    if regex_match:
                        suggested_name = regex_match.expand(suggested_name)

                # Suggestion generation is deferred to assign_suggestions()
                # (called after the full tree has been visited), not done
                # here — see its docstring for why visit-order isn't safe.
                self._pending_suggestions.append((violation, suggested_name, name, list(self.current_scope)))
            self.violations.append(violation)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Visit regular assignment nodes: data = 1."""
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0]
            # Reuses self.source_lines (computed once) instead of
            # ast.get_source_segment's own per-call re-split of the whole
            # file — see fast_get_source_segment.
            rhs_source = fast_get_source_segment(self.source, self._ast_lines, node.value)
            # fast_get_source_segment always resolves given a consistent
            # tree/source pair, which check() guarantees.
            assert rhs_source
            match = self._find_best_match(rhs_source)
            self._check_name(target.id, target.lineno, target.col_offset, match)

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Visit annotated assignment nodes: data: int = 1."""
        if isinstance(node.target, ast.Name):
            if node.value:
                # See visit_Assign above.
                rhs_source = fast_get_source_segment(self.source, self._ast_lines, node.value)
                match = self._find_best_match(rhs_source) if rhs_source else None
            else:
                match = None

            self._check_name(node.target.id, node.target.lineno, node.target.col_offset, match)
        self.generic_visit(node)

    @staticmethod
    def _has_decorator_named(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
        """Handles both bare decorators (``@model_validator``) and called
        decorators (``@model_validator(mode="before")``).
        """
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id == name:
                return True
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == name:
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit function definition nodes: def foo(data):."""
        if not self._has_decorator_named(node, "model_validator"):
            self._check_function_args(node)
        self.current_scope.append(node)
        self.generic_visit(node)
        self.current_scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Visit async function definition nodes: async def foo(data):."""
        if not self._has_decorator_named(node, "model_validator"):
            self._check_function_args(node)
        self.current_scope.append(node)
        self.generic_visit(node)
        self.current_scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit class body but only descend into method definitions.

        Class-level attribute assignments (NamedTuple fields, dataclass fields,
        plain class attributes) are excluded because the class name provides
        sufficient context. Method bodies ARE analysed — a 'result =' inside
        a test method is just as meaningless as one in a standalone function.
        """
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.visit(stmt)

    def _check_function_args(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
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
            self._check_name(node.args.kwarg.arg, node.args.kwarg.lineno, node.args.kwarg.col_offset)


_CROSSABLE_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _binds_name_in_nested_scope(scope_node: ast.AST, name: VariableName, *, has_future_annotations: bool) -> bool:
    """Whether entering `scope_node` (a nested function, lambda, or
    comprehension) introduces its own binding for `name`, so a reference to
    `name` inside it resolves to that new binding instead of whatever
    `scope_node`'s own enclosing scope defines — matching Python's rule that
    a single parameter/assignment/deletion/import/global/nonlocal/except-as/
    match-capture/type-parameter declaration anywhere in a scope governs
    every reference to that name in the *entire* scope, not just from that
    point on.

    An `except ... as`/match-capture (`case x:`, `case {**rest}:`)/
    type-parameter (`def f[T]():`) declaration of `name` is treated the
    same as a shadowing bind here even though it doesn't create a new local
    variable in the usual sense: each stores its name as a plain string
    (`ast.ExceptHandler.name`, `ast.MatchAs.name`, `ast.MatchMapping.rest`,
    `ast.TypeVar.name`, ...), not an `ast.Name` node, so there's no text
    position `_collect_scope_replacements` could safely rewrite there
    anyway. Treating any of them as shadowing and refusing to recurse is
    the same conservative bail-out `validate_function_name.autofix`'s
    `_is_rebound_in_scope` already uses for an analogous ambiguity — and
    it's not just conservative but necessary: unlike `global`/`nonlocal`
    (which `ForbiddenNameVisitor._referenced_via_global_or_nonlocal`
    already refuses to suggest a fix for at all, at check() time, precisely
    because a rename can't safely follow into either), `def f(): except E
    as data: return data` renamed to `return payload` is *syntactically
    valid*, so a wrong rename there would silently change which value the
    function returns instead of being rejected by `atomic_write_text()`'s
    `compile()` check.

    `global`/`nonlocal` themselves need no check here: any name mentioned
    in either, anywhere within the violation's own enclosing scope, is
    already excluded from `replace_names` before `_apply_fixes` ever calls
    this function (see `_referenced_via_global_or_nonlocal`), so this
    function is never even asked about such a name.

    Deletion (`del name`) is treated the same as an assignment: Python's
    "any binding operation anywhere in a scope makes the name local to the
    *whole* scope" rule applies to `del` exactly as it does to `=` — a
    function that deletes then reassigns `name` has its own, separate local
    variable, not a reference to an enclosing scope's variable of the same
    name.
    """
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
        # Lambda has no type_params (PEP 695 generics only apply to def/class).
        # ast.type_param's own subclasses (TypeVar/ParamSpec/TypeVarTuple)
        # all carry .name, but the base class doesn't expose it statically.
        if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple) and type_param.name == name
            for type_param in scope_node.type_params
        ):
            return True

        for child in _iter_own_scope_descendants(scope_node, has_future_annotations=has_future_annotations):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and child.name == name:
                return True
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store | ast.Del) and child.id == name:
                return True
            # A dotted `import a.b.c` (no `as`) binds only the first
            # component ("a") in the local namespace — alias.name is the
            # full dotted path "a.b.c", which never equals a bare `name`.
            # ImportFrom aliases never contain a dot (`from x import y`
            # only ever binds "y"/its "as" alias), so no split is needed.
            if isinstance(child, ast.Import) and any(
                (alias.asname or alias.name.split(".")[0]) == name for alias in child.names
            ):
                return True
            if isinstance(child, ast.ImportFrom) and any((alias.asname or alias.name) == name for alias in child.names):
                return True
            if isinstance(child, ast.ExceptHandler) and child.name == name:
                return True
            if isinstance(child, ast.MatchAs | ast.MatchStar) and child.name == name:
                return True
            if isinstance(child, ast.MatchMapping) and child.rest == name:
                return True
        return False

    # The only caller (_collect_scope_replacements) only ever passes a
    # member of _CROSSABLE_SCOPE_NODES, so anything that isn't a
    # function/lambda above is one of the four comprehension types. A
    # comprehension's grammar only allows expressions, so none of the
    # statement-level constructs checked above (def/class/import/global/
    # nonlocal/except/match) can appear directly within one at all — the
    # *only* thing that can locally bind a name here is a `for` target.
    # A walrus (`:=`) target inside a comprehension looks like another
    # Store here, but PEP 572 binds it to the nearest *enclosing*
    # non-comprehension scope instead of the comprehension itself, so it
    # must never be treated as shadowing that outer scope's own variable —
    # unlike a `for` target, checking for one here would be wrong, not just
    # unnecessary.
    assert isinstance(scope_node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp)  # Type narrowing
    return any(
        isinstance(target, ast.Name) and target.id == name
        for generator in scope_node.generators
        for target in ast.walk(generator.target)
    )


def _has_future_annotations_import(tree: ast.Module) -> bool:
    """Whether `from __future__ import annotations` (PEP 563) is active for
    this module. Must be a top-level statement if present at all (Python
    itself rejects it anywhere else at compile time), so only `tree.body`
    needs checking, not the whole subtree.
    """
    return any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "__future__"
        and any(a.name == "annotations" for a in stmt.names)
        for stmt in tree.body
    )


def _signature_defaults(args: ast.arguments) -> Iterator[ast.expr]:
    """Default values in a function/lambda signature — always evaluated
    once, at `def`/`lambda` time, in the true *enclosing* scope. Unlike
    annotations (see `_signature_annotations`), this holds even when the
    function has PEP 695 type parameters (`def f[T](x=default):` — `default`
    still sees the enclosing scope, confirmed against CPython directly:
    `def outer(): data = 1; def inner[data](x=data): return x` returns the
    *outer* `1`, not the `TypeVar`).
    """
    yield from args.defaults
    yield from (default for default in args.kw_defaults if default is not None)


def _signature_annotations(args: ast.arguments) -> Iterator[ast.expr]:
    """Parameter annotations in a function signature. Evaluated in the
    enclosing scope *unless* the function has PEP 695 type parameters, in
    which case they're evaluated within the type parameters' own implicit
    scope instead — which a same-named type parameter shadows exactly like
    a same-named parameter shadows the function's own body (confirmed
    against CPython: `def outer(): data = 1; def inner[data]() -> data: ...`
    binds the return annotation to the `TypeVar`, not the outer `1`). Callers
    decide which bucket (`_outer_scope_children`/`_own_scope_children`) this
    belongs to based on whether `type_params` is empty.
    """
    all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        all_args.append(args.vararg)
    if args.kwarg:
        all_args.append(args.kwarg)
    for arg in all_args:
        if arg.annotation is not None:
            yield arg.annotation


def _type_param_defaults_and_bounds(type_params: list[ast.type_param]) -> Iterator[ast.expr]:
    """A PEP 695 type parameter's own `bound`/`default_value` expressions
    (`def f[T: bound = default](): ...`) — unlike a parameter/return
    annotation, these are evaluated lazily through a real closure over the
    scope enclosing the `def`, regardless of PEP 563 deferred annotations
    (confirmed against CPython: a nested `def f[T: data]():` still resolves
    `data` to the nearest enclosing binding, respecting shadowing, exactly
    like a parameter default would — both with and without `from __future__
    import annotations` active). So callers always treat these as
    `_outer_scope_children`, never gated by `has_future_annotations`.
    """
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
    """`replace_names`, with every one of `type_params`'s own peer names
    removed. A PEP 695 type parameter's own bound/default, or a `type`
    alias's own value, can reference an *earlier* peer from the same
    `type_params` list by name — that resolves to the peer object itself,
    never to whatever the enclosing scope binds under the same name (see
    `_type_param_defaults_and_bounds`), so a peer name must never be treated
    as an enclosing-scope reference to rename. Works unchanged when
    `type_params` is empty (no peers to filter).
    """
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
    """Direct or indirect children of `scope_node` that are evaluated in its
    *enclosing* scope rather than its own — so a rename must still visit
    them even when `scope_node`'s own body shadows the name being renamed.

    A PEP 695 type parameter's own `bound`/`default_value` expression (see
    `_type_param_defaults_and_bounds`) is deliberately *not* included here,
    even though it's evaluated lazily through a closure over this same
    enclosing scope: unlike a decorator or parameter default, it can also
    reference an earlier type parameter from the same `type_params` list,
    which shares the implicit type-parameter scope rather than this
    enclosing one — so it needs its own replacement mapping with those peer
    names filtered out first. `_collect_replacements` handles it directly
    instead of through this function.

    Decorators and parameter defaults always run once when the `def`/
    `lambda` statement itself executes, in whatever scope contains it —
    never inside the function's own body scope. Parameter/return
    annotations do too, *except* when the function has PEP 695 type
    parameters (see `_signature_annotations`), in which case
    `_own_scope_children` handles them instead — *or* when
    `has_future_annotations` (PEP 563, `from __future__ import
    annotations`) is active: then no annotation is ever evaluated eagerly
    in any scope at all, only stored as a string and resolved later
    (typically by `typing.get_type_hints()`) against the function's
    *module* globals — never the enclosing function's locals, unlike a
    default value. Renaming such an annotation to follow a local variable's
    rename would silently point it at a name that doesn't exist at module
    scope (`NameError` from `get_type_hints()`) or, worse, an unrelated
    module global that happens to share the new name — so `has_future_annotations`
    excludes annotations from both `_outer_scope_children` and
    `_own_scope_children` entirely, the same "don't touch what we can't
    safely follow" treatment already given to `global`/`nonlocal`. A
    comprehension's *first* `for` clause's iterable is the one exception to
    "everything in a comprehension runs in its own scope": Python evaluates
    only that first iterable eagerly, in the enclosing scope (passed as an
    argument to the implicit generator function), before entering the
    comprehension's own scope for everything else — every later `for`/`if`
    clause and the element expression all run inside it.
    """
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef):
        yield from scope_node.decorator_list
        yield from _signature_defaults(scope_node.args)
        if not scope_node.type_params and not has_future_annotations:
            yield from _signature_annotations(scope_node.args)
            if scope_node.returns is not None:
                yield scope_node.returns
    elif isinstance(scope_node, ast.Lambda):
        # Lambda can't have annotations, a return type, or type_params at all.
        yield from _signature_defaults(scope_node.args)
    else:
        yield scope_node.generators[0].iter


def _own_scope_children(
    scope_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | _ComprehensionNode,
    *,
    has_future_annotations: bool,
) -> Iterator[ast.AST]:
    """Direct or indirect children of `scope_node` that belong to its own,
    new scope — the counterpart to `_outer_scope_children` above.
    """
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef):
        if scope_node.type_params and not has_future_annotations:
            yield from _signature_annotations(scope_node.args)
            if scope_node.returns is not None:
                yield scope_node.returns
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
    *,
    has_future_annotations: bool,
) -> Iterator[ast.AST]:
    """Every node belonging to `scope_node`'s own execution scope — its
    `_own_scope_children()` themselves, plus their own descendants, without
    crossing into any *further* nested scope (`iter_within_scope_from`
    handles that: a further-nested function/lambda/comprehension found
    among the descendants is yielded but never itself descended into).

    `_binds_name_in_nested_scope()` uses this instead of walking
    `scope_node` wholesale: `scope_node`'s `_outer_scope_children()`
    (decorators, defaults, annotations without type parameters) run in the
    *enclosing* scope, not `scope_node`'s own — a `Store` there (e.g. a
    walrus inside a default value, `def f(x=(data := 1)):`) must never be
    mistaken for a binding that shadows `scope_node`'s own body, or a
    legitimate closure reference in the body would be wrongly skipped as
    "shadowed" while the default itself still got renamed, splitting one
    variable into two unrelated ones.
    """
    yield from iter_within_scope_from(_own_scope_children(scope_node, has_future_annotations=has_future_annotations))


def _collect_replacements(
    node: ast.AST, replace_names: dict[VariableName, VariableName], *, has_future_annotations: bool
) -> list[tuple[int, int, VariableName, VariableName]]:
    """Find (line, col, old_name, new_name) for every `Name` node at or
    below `node` whose id is being replaced — including `node` itself
    (unlike `_collect_scope_replacements()`, which only inspects a node's
    children), and including references inside a nested function/lambda/
    comprehension that reads an enclosing variable as a closure, as long as
    that nested scope doesn't itself rebind the name (see
    `_binds_name_in_nested_scope`). Renaming only the assignment while
    leaving a closure's own reference untouched would leave the closure
    reading a name that no longer exists — a `NameError` the moment it
    runs, not merely an incomplete rename (ch. 2: "MUST NOT perform an
    auto-fix that can change runtime behavior"; "MUST ensure that a fix
    does not change name binding or scope unintentionally").

    Checking `node` itself, not just its children, matters here in two
    ways: `_outer_scope_children()`/`_own_scope_children()` can yield a
    bare expression rather than a container whose *children* hold the
    interesting nodes — e.g. a parameter default that's just `data`
    (`def inner(data=data):`) or a comprehension's `elt` that's just `data`
    (`[data for data in items]`) — and a nested function/lambda/
    comprehension reached that way needs its *own* shadow-check and
    outer/own-scope split applied to it, not just to whatever nodes
    happened to reach it as someone else's "child".

    A nested scope that shadows the name is never fully skipped: its
    `_outer_scope_children` (decorators, defaults, annotations, a
    comprehension's first iterable) are still visited with the *unfiltered*
    `replace_names`, since Python evaluates them in the enclosing scope
    regardless of what the nested scope's own body shadows — only its
    `_own_scope_children` are visited with the shadowed name filtered out.

    A `FunctionDef`/`AsyncFunctionDef`'s own PEP 695 type parameter
    `bound`/`default_value` expressions (`_type_param_defaults_and_bounds`)
    get a *third*, separately-filtered mapping: unlike every other outer
    child, one of these can also reference an *earlier* type parameter from
    the same `type_params` list (confirmed against CPython: within one
    list, a later type parameter's bound/default sees every peer as a real
    binding, not the enclosing scope) — renaming such a reference would
    silently repoint it at the enclosing scope's variable instead of the
    peer it actually resolves to, the same failure class as any other
    wrong-scope rename.

    A PEP 695 `type` alias statement (`ast.TypeAlias`) gets the same
    peer-filtered treatment, for both its own `type_params`'s bound/default
    expressions *and* its `value` — confirmed against CPython that the
    alias's value expression, like a type parameter's own bound/default, is
    lazily evaluated but resolves a peer type parameter reference to that
    peer rather than the enclosing scope, exactly like a nested function's
    type parameters do. `ast.TypeAlias` isn't a member of
    `_CROSSABLE_SCOPE_NODES` (it can't itself be shadowed the way a function
    body can — the alias's own type parameters are confined to the
    statement, never leaking into whatever scope contains it), so it gets
    its own dedicated branch here instead.

    A class body is never recursed into: `self.x`/`cls.x` access is a
    distinct `ast.Attribute` node, not `ast.Name`, so a bare class-body
    reference to an enclosing scope's variable is a separate, rarer pattern
    this function doesn't attempt — consistent with `ForbiddenNameVisitor`
    already excluding class-level attribute assignments from detection
    entirely (see its `visit_ClassDef`).

    `ast.arg` parameter bindings are a distinct node type from `ast.Name`,
    so a same-named parameter is never matched here — only actual variable
    references are.
    """
    if isinstance(node, ast.Name):
        if node.id in replace_names:
            return [(node.lineno, node.col_offset, node.id, replace_names[node.id])]
        return []

    if isinstance(node, _CROSSABLE_SCOPE_NODES):
        results: list[tuple[int, int, VariableName, VariableName]] = []
        for outer_child in _outer_scope_children(node, has_future_annotations=has_future_annotations):
            results.extend(
                _collect_replacements(outer_child, replace_names, has_future_annotations=has_future_annotations)
            )

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.type_params:
            bound_default_names = _peer_filtered_replace_names(node.type_params, replace_names)
            if bound_default_names:
                for expr in _type_param_defaults_and_bounds(node.type_params):
                    results.extend(
                        _collect_replacements(expr, bound_default_names, has_future_annotations=has_future_annotations)
                    )

        nested_names = {
            name: new
            for name, new in replace_names.items()
            if not _binds_name_in_nested_scope(node, name, has_future_annotations=has_future_annotations)
        }
        if nested_names:
            for own_child in _own_scope_children(node, has_future_annotations=has_future_annotations):
                results.extend(
                    _collect_replacements(own_child, nested_names, has_future_annotations=has_future_annotations)
                )
        return results

    if isinstance(node, ast.TypeAlias):
        # `node.name` is bound in the *enclosing* scope (like a function's
        # own name), so it's visited with the unfiltered mapping. The
        # implicit type-parameter scope covers both `type_params`' own
        # bound/default expressions *and* `value` itself (confirmed against
        # CPython: `type Alias[data, T: data] = (T, data)` resolves both
        # `data` references to the peer `TypeVar`, not to an enclosing local
        # of the same name) — both use the same peer-filtered mapping.
        results = list(_collect_replacements(node.name, replace_names, has_future_annotations=has_future_annotations))
        filtered_names = _peer_filtered_replace_names(node.type_params, replace_names)
        if filtered_names:
            for expr in _type_param_defaults_and_bounds(node.type_params):
                results.extend(
                    _collect_replacements(expr, filtered_names, has_future_annotations=has_future_annotations)
                )
            results.extend(
                _collect_replacements(node.value, filtered_names, has_future_annotations=has_future_annotations)
            )
        return results

    if isinstance(node, ast.ClassDef):
        return []

    return [
        replacement
        for child in ast.iter_child_nodes(node)
        for replacement in _collect_replacements(child, replace_names, has_future_annotations=has_future_annotations)
    ]


def _collect_scope_replacements(
    scope: ast.AST, replace_names: dict[VariableName, VariableName], *, has_future_annotations: bool
) -> list[tuple[int, int, VariableName, VariableName]]:
    """Entry point for `_apply_fixes()`: like `_collect_replacements()`
    above, but only ever inspects `scope`'s children, never `scope` itself.

    `scope` is always the violation's own enclosing function or the module
    (from `_find_enclosing_function()`/`_apply_fixes()`). When it's a
    function, `scope`'s own decorators/defaults/annotations/type-parameter
    bounds run in whatever scope *contains* `scope`, not inside `scope`
    itself (see `_outer_scope_children`) — that syntax is already visited,
    with the *enclosing* scope's own mapping, when the enclosing scope's own
    `_collect_scope_replacements` call crosses into `scope` as a nested node
    via `_collect_replacements`'s `_CROSSABLE_SCOPE_NODES` branch. Walking
    every one of `scope`'s immediate children unfiltered here too — as
    `ast.iter_child_nodes` would — reused *this* mapping (`scope`'s own, for
    names shadowed inside its body) for that same outer-scope syntax a
    second time, e.g. renaming a parameter default's reference
    (`def inner(x=data): data = ...`) or a type parameter's bound
    referencing an earlier peer (`def inner[data, T: data](): data = ...`)
    using the *inner* rename instead of leaving it untouched — corrupting
    the source outright when the two renames differ in length. Restricting
    this to `_own_scope_children()` keeps `scope`'s own mapping scoped to
    the syntax that actually runs inside `scope`.
    """
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        children: Iterator[ast.AST] = _own_scope_children(scope, has_future_annotations=has_future_annotations)
    else:
        children = ast.iter_child_nodes(scope)
    return [
        replacement
        for child in children
        for replacement in _collect_replacements(child, replace_names, has_future_annotations=has_future_annotations)
    ]


def _find_enclosing_function(tree: ast.Module, line: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
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
    """Scope-aware: groups violations by scope and replaces ALL uses of a
    variable within that scope, not just the assignment position.
    """
    lines = source.splitlines(keepends=True)
    has_future_annotations = _has_future_annotations_import(tree)

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
    scope_replacements: dict[int | None, dict[VariableName, VariableName]] = {}
    for scope_id, scope_violations in violations_by_scope.items():
        replacements: dict[VariableName, VariableName] = {}
        for v in scope_violations:
            old_name = v["name"]
            new_name = v["suggestion"]
            # The caller only ever includes violations with a suggestion
            # (see the module docstring above); this narrows
            # VariableName | None to VariableName for the dict below.
            assert new_name is not None
            if old_name not in replacements:
                # First violation of this name in this scope wins
                replacements[old_name] = new_name
        scope_replacements[scope_id] = replacements

    # Step 3: Collect replacements for each scope
    all_replacements: list[tuple[int, int, VariableName, VariableName]] = []
    for scope_id, replacements in scope_replacements.items():
        all_replacements.extend(
            _collect_scope_replacements(
                scope_nodes[scope_id], replacements, has_future_annotations=has_future_annotations
            )
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
    def __init__(self) -> None:
        self.forbidden_names = DEFAULT_FORBIDDEN_NAMES

    @property
    def check_id(self) -> str:
        return "forbid-vars"

    @property
    def error_code(self) -> str:
        return "TRI001"

    def get_prefilter_pattern(self) -> list[str] | None:
        return sorted(self.forbidden_names)

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        visitor = ForbiddenNameVisitor(self.forbidden_names, source)
        visitor.tree = tree  # Store tree for scope-aware name generation
        visitor.has_future_annotations = _has_future_annotations_import(tree)
        visitor.visit(tree)
        visitor.assign_suggestions()

        # Lazy tokenization: only tokenize if violations found
        if visitor.violations:
            ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
            raw_violations = [v for v in visitor.violations if v["line"] not in ignored_lines]
        else:
            raw_violations = []

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
        fixable = [cast("ForbidVarsFixData", v.fix_data) for v in violations if v.fixable and v.fix_data]

        if not fixable:
            return False

        try:
            _apply_fixes(filepath, fixable, source, tree, encoding)
        except OSError:
            # Debug-only: mark_fix_failed() below already reports this
            # cleanly as [FIX FAILED] — an ERROR-level .exception() call
            # here would just leak a redundant raw traceback onto the
            # user's stderr by default (nothing in this codebase configures
            # logging, so Python's own lastResort handler prints WARNING+
            # straight to stderr).
            logger.debug("Failed to apply fixes to %s", filepath, exc_info=True)
            for v in violations:
                if v.fixable:
                    mark_fix_failed(v)
            return False
        else:
            return True
