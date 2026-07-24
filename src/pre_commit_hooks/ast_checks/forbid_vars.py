"""Check for forbidden meaningless variable names like 'data' and 'result'.

TRI001: Detects and suggests replacements for meaningless variable names that
reduce code maintainability.

Inline ignore: # pytriage: ignore=TRI001
"""

from __future__ import annotations

import ast
import logging
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, TypedDict, cast

from ._base import (
    BaseCheck,
    Violation,
    atomic_write_text,
    byte_col_to_char_col,
    find_ignored_lines,
    ignore_pattern_for,
    mark_fix_failed,
    split_lines_like_ast,
)
from ._forbid_vars_suggestions import Confidence, plan_suggestions
from ._scope import iter_within_scope_from

if TYPE_CHECKING:
    import argparse
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
    byte_col: int
    suggestion: VariableName | None
    auto_fixable: bool


DEFAULT_FORBIDDEN_NAMES = {"data", "result"}


class ForbidVarsLevel(Enum):
    """See ForbidVarsCheck.check() and docs/adr/0031-forbid-vars-conservative-reporting-default.md."""

    CONSERVATIVE = auto()
    PERMISSIVE = auto()


def _function_name_describes_parameter(function_name: str, parameter_name: VariableName) -> bool:
    suffix = f"_{parameter_name}"
    return function_name.endswith(suffix) and len(function_name) > len(suffix)


class ForbiddenNameVisitor(ast.NodeVisitor):
    """Detects forbidden variable names in every context where a variable is defined."""

    def __init__(
        self,
        forbidden_names: set[VariableName],
        source: str,
    ) -> None:
        self.forbidden_names = forbidden_names
        self._ast_lines = split_lines_like_ast(source)
        self.violations: list[ForbidVarsFixData] = []

    def _check_name(
        self,
        name: VariableName,
        lineno: int,
        col_offset: int,
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
                "byte_col": col_offset,
                "suggestion": None,
                "auto_fixable": False,
            }
            self.violations.append(violation)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Visit regular assignment nodes: data = 1."""
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0]
            self._check_name(target.id, target.lineno, target.col_offset)

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Visit annotated assignment nodes: data: int = 1."""
        if isinstance(node.target, ast.Name):
            self._check_name(node.target.id, node.target.lineno, node.target.col_offset)
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
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Visit async function definition nodes: async def foo(data):."""
        if not self._has_decorator_named(node, "model_validator"):
            self._check_function_args(node)
        self.generic_visit(node)

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
        """Skips a parameter whose own function name already describes it
        (``feed_data(self, data: bytes)``, ``parse_client_bulk_write_result(result)``)
        the same way `visit_ClassDef` skips a class attribute: the enclosing
        name already provides sufficient context, so flagging the parameter
        too is redundant.
        """
        if _function_name_describes_parameter(function_name, arg.arg):
            return
        self._check_name(arg.arg, arg.lineno, arg.col_offset)


_CROSSABLE_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _binds_name_in_nested_scope(scope_node: ast.AST, name: VariableName) -> bool:
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

        for child in _iter_own_scope_descendants(scope_node):
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
    parameters (see `_signature_annotations`), in which case they run in the
    type parameters' own implicit scope instead — `_collect_replacements`
    visits them directly, with a mapping filtered for type-parameter peers
    only, rather than through this function or `_own_scope_children` (see
    there for why: that implicit scope isn't the function's own body scope
    either, so neither bucket's shadow rules actually apply to it) — *or*
    when `has_future_annotations` (PEP 563, `from __future__ import
    annotations`) is active: then no annotation is ever evaluated eagerly
    in any scope at all, only stored as a string and resolved later
    (typically by `typing.get_type_hints()`) against the function's
    *module* globals — never the enclosing function's locals, unlike a
    default value. Renaming such an annotation to follow a local variable's
    rename would silently point it at a name that doesn't exist at module
    scope (`NameError` from `get_type_hints()`) or, worse, an unrelated
    module global that happens to share the new name — so `has_future_annotations`
    excludes annotations from `_outer_scope_children`, `_own_scope_children`,
    and `_collect_replacements`'s own type-parameter-scope handling alike,
    the same "don't touch what we can't safely follow" treatment already
    given to `global`/`nonlocal`. A
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
) -> Iterator[ast.AST]:
    """Direct or indirect children of `scope_node` that belong to its own,
    new scope — the counterpart to `_outer_scope_children` above.

    A `FunctionDef`/`AsyncFunctionDef` with PEP 695 type parameters and no
    deferred annotations does *not* put its own parameter/return annotations
    here, even though they're evaluated outside `_outer_scope_children`'s
    bucket too: they run in the type parameters' own implicit scope, which
    sees the *type parameters* as peers plus (via closure) the enclosing
    scope — never `scope_node`'s own body-local variables or parameters
    (confirmed against CPython: `def outer(): data = 1; def inner[T](value:
    data): data = 2; return value` still binds the annotation to outer's
    `1`, unaffected by `inner`'s own later `data = 2`). Filtering them by
    `_binds_name_in_nested_scope` here, alongside the body, would wrongly
    treat a body-local rebind as shadowing an annotation it can't actually
    see. `_collect_replacements` visits them directly instead, with a
    mapping filtered only for type-parameter peers.
    """
    if isinstance(scope_node, ast.FunctionDef | ast.AsyncFunctionDef):
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
    variable into two unrelated ones. A `FunctionDef`/`AsyncFunctionDef`'s
    own parameter/return annotations (when it has PEP 695 type parameters)
    are excluded from `_own_scope_children()` for the same reason, so
    they're never reachable from here either — they can't be shadowed by
    anything `scope_node`'s own body binds in the first place.
    """
    yield from iter_within_scope_from(_own_scope_children(scope_node))


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

    When that same `FunctionDef`/`AsyncFunctionDef` also has parameter/
    return annotations and no deferred annotations active, those get the
    *same* peer-filtered mapping too, visited separately from both
    `_outer_scope_children` and `_own_scope_children` (neither is right:
    the annotations aren't evaluated in the enclosing scope like a default,
    nor do they see the function's own body/parameters like an ordinary
    body statement — only the type parameters' own implicit scope, shadowed
    only by a peer type parameter of the same name — confirmed against
    CPython: a body-local reassignment of the annotated name has no effect
    on what the annotation itself resolves to).

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

            if not has_future_annotations:
                annotation_names = _peer_filtered_replace_names(node.type_params, replace_names)
                if annotation_names:
                    for expr in _signature_annotations(node.args):
                        results.extend(
                            _collect_replacements(expr, annotation_names, has_future_annotations=has_future_annotations)
                        )
                    if node.returns is not None:
                        results.extend(
                            _collect_replacements(
                                node.returns, annotation_names, has_future_annotations=has_future_annotations
                            )
                        )

        nested_names = {name: new for name, new in replace_names.items() if not _binds_name_in_nested_scope(node, name)}
        if nested_names:
            for own_child in _own_scope_children(node):
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
        children: Iterator[ast.AST] = _own_scope_children(scope)
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
    __slots__ = ("_level", "forbidden_names")

    def __init__(self, level: ForbidVarsLevel = ForbidVarsLevel.CONSERVATIVE) -> None:
        self.forbidden_names = DEFAULT_FORBIDDEN_NAMES
        self._level = level

    @property
    def check_id(self) -> str:
        return "forbid-vars"

    @property
    def error_code(self) -> str:
        return "TRI001"

    def get_prefilter_pattern(self) -> list[str] | None:
        return sorted(self.forbidden_names)

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--forbid-vars-level",
            choices=["conservative", "permissive"],
            default="conservative",
            help=(
                "Whether forbid-vars (TRI001) reports a forbidden name "
                "that has no suggested replacement. 'conservative' "
                "(default) reports a name only when a rename can be "
                "suggested; 'permissive' reports every forbidden name "
                "regardless. --fix only ever applies a high-confidence "
                "suggestion at either level."
            ),
        )

    @classmethod
    def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]:
        return {"level": ForbidVarsLevel[args.forbid_vars_level.upper()]}

    def check(self, _filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        visitor = ForbiddenNameVisitor(self.forbidden_names, source)
        visitor.visit(tree)

        if visitor.violations:
            ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
            raw_violations = [v for v in visitor.violations if v["line"] not in ignored_lines]
            suggestions = plan_suggestions(tree, self.forbidden_names, ignored_lines)
        else:
            raw_violations = []
            suggestions = {}

        violations = []
        for v in raw_violations:
            proposal = suggestions.get((v["line"], v["byte_col"]))
            if proposal is not None:
                v["suggestion"] = proposal.name
                v["auto_fixable"] = proposal.confidence is Confidence.AUTO_FIX
            if self._level is ForbidVarsLevel.CONSERVATIVE and not v["suggestion"]:
                continue
            if v.get("suggestion"):
                message = f"'{v['name']}' is a meaningless variable name — '{v['suggestion']}' is more descriptive."
            else:
                message = f"Forbidden variable name '{v['name']}' found. Use a more descriptive name."
            message += " Or add '# pytriage: ignore=TRI001' to suppress."

            violations.append(
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=v["line"],
                    col=v["col"],
                    message=message,
                    fixable=v["auto_fixable"],
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
