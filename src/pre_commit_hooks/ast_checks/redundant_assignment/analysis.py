"""Variable tracking and redundancy pattern detection for TRI005."""

from __future__ import annotations

import ast
import bisect
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Literal

from pre_commit_hooks.ast_checks._base import classify_comment_lines, fast_get_source_segment, split_lines_like_ast

if TYPE_CHECKING:
    from collections.abc import Iterator

type UsageContext = Literal["attribute_or_subscript_assignment", "augmented_assignment", "unknown"]


class PatternType(Enum):
    IMMEDIATE_SINGLE_USE = auto()  # x = "foo"; func(x=x)
    SINGLE_USE = auto()  # x = calc(); return x
    LITERAL_IDENTITY = auto()  # foo = "foo"


@dataclass
class AssignmentInfo:
    var_name: str
    line: int
    col: int
    stmt_index: int
    rhs_node: ast.expr
    rhs_source: str
    scope_id: int
    has_type_annotation: bool = False
    in_loop: bool = False
    in_control_flow: bool = False
    in_global_scope: bool = False
    has_comment_above: bool = False
    has_inline_comment: bool = False
    rhs_has_await: bool = False
    # See _record_compound_target_rebindings.
    is_rebinding_marker: bool = False


@dataclass
class UsageInfo:
    var_name: str
    line: int
    col: int
    stmt_index: int
    context: UsageContext
    scope_id: int
    usage_has_await: bool = False
    in_control_flow: bool = False
    # Independent of whether the *assignment* is in a loop — used to reject
    # inlining a call whose single textual use would actually execute
    # repeatedly.
    in_loop: bool = False
    # A use inside a lambda body executes later (and possibly repeatedly, or
    # never) at call time, not once at the point the lambda is defined.
    in_lambda: bool = False
    in_comprehension: bool = False
    # Identity of the node for this use, not just its position. None for
    # usage kinds that don't track it.
    node: ast.expr | None = None
    # The statement containing `node`, for is_preceded_by_call below. Stored
    # (not eagerly evaluated) so its O(statement size) evaluation-order walk
    # only runs for the rare usages that actually need it (should_autofix's
    # zero-arg-call carve-out) instead of every Name-load in the file.
    enclosing_stmt: ast.stmt | None = None
    # Whether this use sits anywhere inside an f-string replacement field
    # (`ast.FormattedValue`) — used to gate the naive text-substitution
    # autofix, which would otherwise re-quote a string literal inside `{}`
    # (issue #72).
    in_fstring_expression: bool = False
    # Byte-offset (start, end) span of the *entire* enclosing
    # `ast.FormattedValue` (braces, any conversion/format spec, all of it)
    # when this use IS that field's whole expression (no conversion, no
    # format spec, single line) — the one shape where a string literal can
    # be safely spliced as raw text in place of the field. None otherwise,
    # including when in_fstring_expression is True but the use is only
    # part of a larger field expression (e.g. `{x.attr}`).
    fstring_field_span: tuple[int, int] | None = None


@dataclass
class VariableLifecycle:
    assignment: AssignmentInfo
    uses: list[UsageInfo]
    # True when the RHS's underlying Name/Attribute reference is itself
    # reassigned (or mutated via the same exact reference) somewhere
    # between this assignment and its single use — see detect_redundancy
    # for why that disqualifies "redundant assignment" entirely, not just
    # its autofix (issue #74).
    rhs_reference_reassigned_before_use: bool = False

    @property
    def is_single_use(self) -> bool:
        return len(self.uses) == 1

    @property
    def is_immediate_use(self) -> bool:
        """First use is 0-1 statements after the assignment.

        Uses in different scopes (closures) are never considered immediate,
        even if their statement index appears close, because they're in
        nested functions and the variable is captured by the closure.
        """
        if not self.uses:
            return False
        first_use = self.uses[0]

        if first_use.scope_id != self.assignment.scope_id:
            return False

        return first_use.stmt_index <= self.assignment.stmt_index + 1


def _unwind_to_base_name(node: ast.expr) -> ast.Name | None:
    base = node
    while isinstance(base, ast.Attribute | ast.Subscript):
        base = base.value
    return base if isinstance(base, ast.Name) else None


def _has_await_expression(node: ast.expr) -> bool:
    class AwaitDetector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.has_await = False

        def visit_Await(self, _node: ast.Await) -> None:
            self.has_await = True

    detector = AwaitDetector()
    detector.visit(node)
    return detector.has_await


# Node types treated as "may run arbitrary user code, or suspend execution"
# for evaluation-order purposes: an explicit call; attribute/subscript
# access (`@property` getters and `__getitem__`/descriptors can execute
# arbitrary code just as a call can); await/yield (suspension points where
# other code can run and change state before control resumes); operators,
# which can invoke arbitrary user code via dunder overloads (`__add__`,
# `__eq__`, `__bool__`, etc.); and IfExp, whose `test` truthiness check
# invokes `__bool__` the same way BoolOp's short-circuit check does.
_POTENTIALLY_EFFECTFUL_NODE_TYPES = (
    ast.Call,
    ast.Attribute,
    ast.Subscript,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
    ast.BinOp,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
)


def _evaluation_order_children(node: ast.AST) -> Iterator[tuple[ast.AST, bool]]:
    """Yield `node`'s children in Python's actual evaluation order.

    Each child is paired with whether it's *conditionally* evaluated —
    i.e. it might run zero times at runtime, unlike everything else here
    which always runs exactly once if its parent does. Inlining a call into
    a conditional position is just as unsafe as inlining it somewhere with
    a preceding effect: both change whether/when the call actually fires
    relative to the original, unconditional assignment.

    `ast.iter_child_nodes` matches evaluation order and unconditional-ness
    for most expression types, since their `_fields` are declared
    left-to-right in evaluation order (BinOp.left before .right, Call.func
    before .args before .keywords, etc.) and don't skip children at
    runtime. The exceptions handled explicitly here:
    - `ast.Dict`: `_fields` are `('keys', 'values')` — every key, then
      every value — but Python evaluates each key/value *pair* together,
      interleaved (e.g. `{"a": f(), x: 1}` evaluates "a", f(), x, 1 — not
      "a", x, f(), 1). A `None` key marks `**unpacking`, which evaluates
      only the paired value.
    - `ast.Assign`: `_fields` are `('targets', 'value', ...)`, but Python
      evaluates the RHS `value` *before* the target(s) (relevant when a
      target is `obj.attr` or `obj[key]`, whose base expression `obj` is
      itself evaluated then, after `value`).
    - `ast.IfExp` (ternary): `test` always evaluates, but exactly one of
      `body`/`orelse` does — never both, and never unconditionally.
    - `ast.BoolOp` (`and`/`or`): short-circuits, so only the first operand
      is guaranteed to evaluate; the rest are conditional on earlier ones.
    """
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if key is not None:
                yield key, False
            yield value, False
        return
    if isinstance(node, ast.Assign):
        yield node.value, False
        for assign_target in node.targets:
            yield assign_target, False
        return
    if isinstance(node, ast.IfExp):
        yield node.test, False
        yield node.body, True
        yield node.orelse, True
        return
    if isinstance(node, ast.BoolOp):
        for index, value in enumerate(node.values):
            yield value, index > 0
        return
    for child in ast.iter_child_nodes(node):
        yield child, False


def _call_precedes_target(
    node: ast.AST,
    target: ast.AST,
    effect_types: tuple[type, ...] = _POTENTIALLY_EFFECTFUL_NODE_TYPES,
) -> tuple[bool, bool, bool]:
    """Walk `node`'s children in evaluation order looking for `target`.

    It's AST-based rather than text/line-based specifically so it stays
    correct across multi-line statements, where a sibling operand's
    physical line/column says nothing about evaluation order. `target` is
    matched by identity, not structural equality.

    Returns a (found, effect_before_target, node_is_or_contains_effect) triple:
    - found: whether `target` is `node` itself or within its subtree
    - effect_before_target: whether a node matching `effect_types` (see
      `_POTENTIALLY_EFFECTFUL_NODE_TYPES`) fully evaluated before reaching
      `target`, OR `target` is only conditionally reachable (see
      `_evaluation_order_children`) — only meaningful when `found` is True
    - node_is_or_contains_effect: whether `node` itself matches (or
      contains a node matching) `effect_types` that has fully evaluated —
      only meaningful when `found` is False, since a call containing
      `target` among its own arguments doesn't fire until after `target`
      (and everything else in it) is evaluated
    """
    if node is target:
        return True, False, False

    seen_effect = False
    for child, is_conditional in _evaluation_order_children(node):
        found, effect_before, child_has_effect = _call_precedes_target(child, target, effect_types)
        if found:
            return True, seen_effect or effect_before or is_conditional, False
        if child_has_effect:
            seen_effect = True

    return (
        False,
        False,
        seen_effect or isinstance(node, effect_types),
    )


def is_preceded_by_call(use: UsageInfo) -> bool:
    """Check if a potentially effectful expression evaluates before `use`.

    Lazily walks `use.enclosing_stmt` looking for `use.node` — deferred to
    call time (rather than computed eagerly per Name-load during the AST
    walk) since it's O(statement size); running it for every Name-load
    would be quadratic for wide expressions (e.g. a large tuple/list/dict
    literal with many names).

    Used to decide whether inlining a call in place of `use` could reorder
    side effects relative to a sibling expression — see
    `redundant_assignment.semantic.should_autofix`'s zero-arg-call carve-out
    for why this matters (e.g. inlining `value` into
    `sink(side_effect(), value)` would run the inlined call *after*
    `side_effect()`, reversed from the original assign-then-use order).

    Returns True conservatively when `use.node`/`use.enclosing_stmt` is
    unavailable, since safety can't be verified in that case.
    """
    if use.node is None or use.enclosing_stmt is None:
        return True
    _found, effect_before, _ = _call_precedes_target(use.enclosing_stmt, use.node)
    return effect_before


_SUSPENSION_NODE_TYPES = (ast.Yield, ast.YieldFrom, ast.Await)


def _suspension_precedes_use(use: UsageInfo) -> bool:
    """Same evaluation-order walk as `is_preceded_by_call`, filtered to
    suspension points only — for a same-statement case like `return (await
    refresh(), value)[1]`, `value`'s own stmt_index ties with the await's,
    so the coarse per-statement check in `_suspension_point_between` can't
    tell the two apart; this resolves it by real evaluation order instead.
    """
    if use.node is None or use.enclosing_stmt is None:
        return True
    _found, effect_before, _ = _call_precedes_target(use.enclosing_stmt, use.node, _SUSPENSION_NODE_TYPES)
    return effect_before


class VariableTracker(ast.NodeVisitor):
    """Builds a map of variable lifecycles: where each variable is assigned and where it's used, across scopes."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.source_lines = source.splitlines()
        # For _get_source_segment only: split on the same line boundaries
        # ast's own lineno/end_lineno use, unlike self.source_lines above
        # (see split_lines_like_ast).
        self._ast_lines = split_lines_like_ast(source)
        # Computed once per file (not per assignment) since it tokenizes
        # the whole source — see AssignmentInfo.has_comment_above/
        # has_inline_comment for why a tokenize-based classification is
        # needed instead of a naive text scan.
        self._comment_only_lines, self._trailing_comment_lines = classify_comment_lines(source)

        self.current_scope_id = 0
        self.scope_stack: list[int] = [0]  # 0 = module scope
        self.stmt_index_stack: list[int] = [0]
        self.assignments: dict[tuple[int, str], list[AssignmentInfo]] = {}
        self.uses: dict[tuple[int, str], list[UsageInfo]] = {}
        # scope_id -> (line, stmt_index, col, enclosing_stmt) of each
        # yield/yield from/await, in visit order — see
        # _suspension_point_between.
        self.suspension_points: dict[int, list[tuple[int, int, int, ast.stmt | None]]] = {}
        self.global_vars: set[tuple[int, str]] = set()
        self.nonlocal_vars: set[tuple[int, str]] = set()

        # So the LHS of an assignment is never itself treated as a use.
        self.currently_assigning: set[str] = set()

        self.loop_depth = 0

        # if/try/with/match — excludes loops, tracked separately above.
        self.control_flow_depth = 0

        self.comprehension_depth = 0

        # A use inside a lambda body executes later (and possibly
        # repeatedly, or never) at call time, not once at the point the
        # lambda is defined.
        self.lambda_depth = 0

        self.parent_stack: list[ast.AST] = []

        # Innermost enclosing statement of whatever node is currently being
        # visited, updated in visit() below. Stored on each UsageInfo (not
        # searched here) so is_preceded_by_call's evaluation-order walk can
        # run lazily, later, only for the rare usages that need it.
        self.current_stmt: ast.stmt | None = None

        # child_scope_id -> parent_scope_id, for closure detection.
        self.scope_parents: dict[int, int] = {}

    def _enter_scope(self) -> None:
        parent_scope_id = self._get_current_scope_id()
        self.current_scope_id += 1
        child_scope_id = self.current_scope_id

        self.scope_parents[child_scope_id] = parent_scope_id

        self.scope_stack.append(child_scope_id)
        self.stmt_index_stack.append(0)

    def _exit_scope(self) -> None:
        self.scope_stack.pop()
        self.stmt_index_stack.pop()

    def _increment_stmt_index(self) -> None:
        # stmt_index_stack is initialized with [0] and only ever grows/shrinks
        # in balanced pairs via _enter_scope/_exit_scope, so it's never empty.
        self.stmt_index_stack[-1] += 1

    def _get_current_scope_id(self) -> int:
        return self.scope_stack[-1] if self.scope_stack else 0

    def _get_current_stmt_index(self) -> int:
        return self.stmt_index_stack[-1] if self.stmt_index_stack else 0

    def _get_child_scopes(self, scope_id: int) -> list[int]:
        """All direct and indirect child scopes of `scope_id`, to detect closures —
        variables assigned in an outer scope but used in nested function scopes.
        """
        children = []
        for child_id, parent_id in self.scope_parents.items():
            if parent_id == scope_id:
                children.append(child_id)
                children.extend(self._get_child_scopes(child_id))
        return children

    def _get_source_segment(self, node: ast.expr) -> str:
        """Reuses self._ast_lines (computed once in __init__) via
        fast_get_source_segment instead of ast.get_source_segment's own
        per-call re-split of the whole file — called once per assignment
        across the whole file, so the difference is O(source size) total
        instead of O(assignments x source size).
        """
        try:
            return fast_get_source_segment(self.source, self._ast_lines, node) or ""
        # Defensive: fast_get_source_segment slices source by byte offset
        # and decodes it, which could raise (ValueError/UnicodeDecodeError,
        # or TypeError) if a node's position were ever inconsistent with
        # this source — not expected for a node resolved against its own
        # tree.
        except ValueError, TypeError:  # pragma: no cover
            return ""

    def _is_simple_name_target(self, target: ast.expr) -> bool:
        return isinstance(target, ast.Name)

    def visit_Global(self, node: ast.Global) -> None:
        scope_id = self._get_current_scope_id()
        for name in node.names:
            self.global_vars.add((scope_id, name))
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        scope_id = self._get_current_scope_id()
        for name in node.names:
            self.nonlocal_vars.add((scope_id, name))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Decorators are evaluated in the outer (enclosing) scope before the
        function body, so they must be visited before entering the new scope.
        """
        for decorator in node.decorator_list:
            self.visit(decorator)

        self._enter_scope()

        for stmt in node.body:
            self.visit(stmt)
            self._increment_stmt_index()

        self._exit_scope()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        # node.iter runs once, not per-iteration; node.orelse runs only if
        # the loop completes without `break` — conditional like an if/try
        # branch, not unconditional like ordinary code after the loop.
        self._record_compound_target_rebindings(node.target, self._get_current_stmt_index())
        self.visit(node.iter)
        self.loop_depth += 1
        self.visit(node.target)
        for stmt in node.body:
            self.visit(stmt)
        self.loop_depth -= 1
        self.control_flow_depth += 1
        for stmt in node.orelse:
            self.visit(stmt)
        self.control_flow_depth -= 1

    visit_AsyncFor = visit_For  # noqa: N815

    def visit_While(self, node: ast.While) -> None:
        # node.test repeats every iteration, unlike node.orelse (see
        # visit_For above for both).
        self.loop_depth += 1
        self.visit(node.test)
        for stmt in node.body:
            self.visit(stmt)
        self.loop_depth -= 1
        self.control_flow_depth += 1
        for stmt in node.orelse:
            self.visit(stmt)
        self.control_flow_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        """`node.test` always evaluates unconditionally when the `If`
        statement itself is reached — only `body`/`orelse` run
        conditionally. Visiting it outside the incremented
        `control_flow_depth` keeps a later use in the condition itself
        (e.g. a with-block assignment whose only use is `if that_var:`)
        from being wrongly treated as "inside control flow" (issue #73).
        """
        self.parent_stack.append(node)
        self.visit(node.test)
        self.control_flow_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)
        self.control_flow_depth -= 1
        self.parent_stack.pop()

    def visit_Try(self, node: ast.Try) -> None:
        self.control_flow_depth += 1
        self.generic_visit(node)
        self.control_flow_depth -= 1

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        # Each item's optional_vars (`as target`) rebinds on entry, same
        # hazard as a for-loop target above.
        self.control_flow_depth += 1
        stmt_index = self._get_current_stmt_index()
        for item in node.items:
            if item.optional_vars is not None:
                self._record_compound_target_rebindings(item.optional_vars, stmt_index)
        self.generic_visit(node)
        self.control_flow_depth -= 1

    visit_AsyncWith = visit_With  # noqa: N815

    def visit_Match(self, node: ast.Match) -> None:
        """`node.subject` always evaluates unconditionally when the `Match`
        statement itself is reached — only each case's pattern/guard/body
        runs conditionally (if its pattern/guard matches), same as an
        if/elif branch. See visit_If's docstring (issue #73) for why the
        subject must be visited outside the incremented
        `control_flow_depth`.
        """
        self.parent_stack.append(node)
        self.visit(node.subject)
        self.control_flow_depth += 1
        for case in node.cases:
            self.visit(case)
        self.control_flow_depth -= 1
        self.parent_stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """A lambda's body doesn't execute where it's defined — it executes
        later, whenever (and however many times, including zero) the
        lambda is called. Uses inside it must never be treated as
        "the same execution point" as the surrounding statement.
        """
        self.lambda_depth += 1
        self.generic_visit(node)
        self.lambda_depth -= 1

    def visit_Yield(self, node: ast.Yield) -> None:
        self._record_suspension_point(node.lineno, node.col_offset)
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self._record_suspension_point(node.lineno, node.col_offset)
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self._record_suspension_point(node.lineno, node.col_offset)
        self.generic_visit(node)

    def _record_suspension_point(self, line: int, col: int) -> None:
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()
        self.suspension_points.setdefault(scope_id, []).append((line, stmt_index, col, self.current_stmt))

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    ) -> None:
        self.comprehension_depth += 1
        self.generic_visit(node)
        self.comprehension_depth -= 1

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """We skip class-level assignments as they're attributes, not local
        variables. Decorators are evaluated in the outer scope before the
        class body.
        """
        for decorator in node.decorator_list:
            self.visit(decorator)

        self._enter_scope()

        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self.visit(stmt)

        self._exit_scope()

    def visit_Assign(self, node: ast.Assign) -> None:
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        # Skip multiple assignments on a single line (e.g., a = b = c = value)
        # as reportable candidates — these patterns often intentionally
        # assign intermediate variables and avoid re-reading class
        # attributes — but each target's Name(s) still rebind, so they're
        # recorded via _record_compound_target_rebindings below.
        if len(node.targets) > 1:
            for target in node.targets:
                self._record_compound_target_rebindings(target, stmt_index)
            self.visit(node.value)
            return

        # Only track simple name assignments (not tuple unpacking, attributes, etc.)
        for target in node.targets:
            if self._is_simple_name_target(target):
                assert isinstance(target, ast.Name)  # Type narrowing
                var_name = target.id

                if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                    continue

                self.currently_assigning.add(var_name)
                rhs_source = self._get_source_segment(node.value)

                assignment = AssignmentInfo(
                    var_name=var_name,
                    line=node.lineno,
                    col=node.col_offset,
                    stmt_index=stmt_index,
                    rhs_node=node.value,
                    rhs_source=rhs_source,
                    scope_id=scope_id,
                    has_type_annotation=False,
                    in_loop=self.loop_depth > 0,
                    in_control_flow=self.control_flow_depth > 0,
                    in_global_scope=(scope_id == 0),
                    has_comment_above=(node.lineno - 1) in self._comment_only_lines,
                    has_inline_comment=node.lineno in self._trailing_comment_lines,
                    rhs_has_await=_has_await_expression(node.value),
                )

                key = (scope_id, var_name)
                if key not in self.assignments:
                    self.assignments[key] = []
                self.assignments[key].append(assignment)
            else:
                # Attribute/Subscript, or a compound target (tuple/list
                # unpacking, possibly with a Starred element) —
                # _record_compound_target_rebindings dispatches each shape.
                self._record_compound_target_rebindings(target, stmt_index)

        self.visit(node.value)
        self.currently_assigning.clear()

    def _record_compound_target_rebindings(self, target: ast.expr, stmt_index: int) -> None:
        """Registers every Name a compound target rebinds — tuple/list
        unpacking, a chained assignment, a for-loop target, or a with-as
        target — none of which visit_Assign's simple-name path tracks as a
        real AssignmentInfo. Each is recorded via a marker
        (AssignmentInfo.is_rebinding_marker), not a full candidate: its
        line/col may point at a statement that also binds sibling names,
        which apply_fixes has no business touching.
        """
        scope_id = self._get_current_scope_id()

        if isinstance(target, ast.Name):
            var_name = target.id
            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                return
            marker = AssignmentInfo(
                var_name=var_name,
                line=target.lineno,
                col=target.col_offset,
                stmt_index=stmt_index,
                rhs_node=target,
                rhs_source="",
                scope_id=scope_id,
                is_rebinding_marker=True,
            )
            key = (scope_id, var_name)
            if key not in self.assignments:
                self.assignments[key] = []
            self.assignments[key].append(marker)
        elif isinstance(target, ast.Tuple | ast.List):
            for elt in target.elts:
                self._record_compound_target_rebindings(elt, stmt_index)
        elif isinstance(target, ast.Starred):
            self._record_compound_target_rebindings(target.value, stmt_index)
        elif isinstance(target, ast.Attribute | ast.Subscript):  # pragma: no branch
            # Python's grammar limits an assignment target to exactly these
            # five shapes, so this dispatch is exhaustive — there's no
            # "else" case to reach.
            self._track_attribute_or_subscript_base_usage(target, stmt_index)

    def _track_attribute_or_subscript_base_usage(self, node: ast.Attribute | ast.Subscript, stmt_index: int) -> None:
        """In `obj.attr = value` or `obj[key] = value`, `obj` is being read, so it counts as a usage."""
        scope_id = self._get_current_scope_id()

        base = _unwind_to_base_name(node)

        if base is not None:
            var_name = base.id

            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                return

            usage = UsageInfo(
                var_name=var_name,
                line=base.lineno,
                col=base.col_offset,
                stmt_index=stmt_index,
                context="attribute_or_subscript_assignment",
                scope_id=scope_id,
                in_control_flow=self.control_flow_depth > 0,
                in_loop=self.loop_depth > 0,
                in_lambda=self.lambda_depth > 0,
                node=base,
                enclosing_stmt=self.current_stmt,
            )
            key = (scope_id, var_name)
            if key not in self.uses:
                self.uses[key] = []
            self.uses[key].append(usage)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        if self._is_simple_name_target(node.target) and node.value is not None:
            assert isinstance(node.target, ast.Name)  # Type narrowing
            var_name = node.target.id

            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                return

            self.currently_assigning.add(var_name)
            rhs_source = self._get_source_segment(node.value)

            assignment = AssignmentInfo(
                var_name=var_name,
                line=node.lineno,
                col=node.col_offset,
                stmt_index=stmt_index,
                rhs_node=node.value,
                rhs_source=rhs_source,
                scope_id=scope_id,
                has_type_annotation=True,
                in_loop=self.loop_depth > 0,
                in_control_flow=self.control_flow_depth > 0,
                in_global_scope=(scope_id == 0),
                has_comment_above=(node.lineno - 1) in self._comment_only_lines,
                has_inline_comment=node.lineno in self._trailing_comment_lines,
                rhs_has_await=_has_await_expression(node.value),
            )

            key = (scope_id, var_name)
            if key not in self.assignments:
                self.assignments[key] = []
            self.assignments[key].append(assignment)

            self.visit(node.value)
            self.currently_assigning.clear()

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """`x += 1` reads `x` (to get its current value) and then mutates it in
        place, so the read is tracked as a usage — this prevents false
        positives for patterns like:
            if condition:
                msg = "foo"
            else:
                msg = "bar"
            msg += " suffix"  # This USES the conditional value

        It's not tracked as a new assignment: it mutates an existing
        variable rather than producing a fresh one that could be inlined.
        """
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        if self._is_simple_name_target(node.target):
            assert isinstance(node.target, ast.Name)  # Type narrowing
            var_name = node.target.id

            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                self.generic_visit(node)
                return

            self._track_rebinding_use(var_name, node.lineno, node.col_offset, scope_id, stmt_index)
        else:
            # AugAssign.target is Name | Attribute | Subscript; the Name
            # case is handled above.
            assert isinstance(node.target, ast.Attribute | ast.Subscript)  # Type narrowing
            # `obj.attr += 1` both reads and reassigns `obj.attr`, same as
            # `obj.attr = value` — track it the same way so a snapshot
            # taken via `old = obj.attr` (see _rhs_reference_reassigned)
            # sees this as a reassignment of the reference it read from.
            self._track_attribute_or_subscript_base_usage(node.target, stmt_index)

        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """`x := value` (walrus) evaluates `value`, then rebinds `x` to it —
        the same reassignment hazard `visit_AugAssign` guards against for
        `_rhs_reference_reassigned`'s snapshot-before-reassignment check
        (issue #74), just spelled as an expression instead of a statement.
        `old = x; return (x := 2, old)` must not be inlined into `return
        (x := 2, x)`, which would read the just-rebound value instead of
        the one captured at assignment time.
        """
        self.parent_stack.append(node)
        self.visit(node.value)
        self.parent_stack.pop()

        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()
        var_name = node.target.id

        if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
            return

        self._track_rebinding_use(var_name, node.target.lineno, node.target.col_offset, scope_id, stmt_index)

    def _track_rebinding_use(self, var_name: str, line: int, col: int, scope_id: int, stmt_index: int) -> None:
        """Records that `var_name` was rebound (augmented-assignment or
        walrus) at this point — not a fresh `AssignmentInfo` (neither is a
        standalone statement TRI005 could itself flag as redundant), but a
        "use" `_rhs_reference_reassigned` recognizes as disqualifying a
        snapshot taken from `var_name` earlier in the same scope.
        """
        usage = UsageInfo(
            var_name=var_name,
            line=line,
            col=col,
            stmt_index=stmt_index,
            context="augmented_assignment",
            scope_id=scope_id,
            in_control_flow=self.control_flow_depth > 0,
        )
        key = (scope_id, var_name)
        if key not in self.uses:
            self.uses[key] = []
        self.uses[key].append(usage)

    def visit(self, node: ast.AST) -> None:
        """Dispatch to the type-specific visit_* method, tracking current_stmt.

        Many statement visitors below (visit_Assign, visit_AnnAssign, ...)
        don't call self.generic_visit(node) on themselves — they manually
        visit only the parts they care about — so parent_stack alone can't
        reconstruct "the enclosing statement" for an arbitrary node. This
        override catches every ast.stmt as it's dispatched, regardless of
        which visit_* method (or none) handles it next.
        """
        if isinstance(node, ast.stmt):
            self.current_stmt = node
        super().visit(node)

    def generic_visit(self, node: ast.AST) -> None:
        self.parent_stack.append(node)
        super().generic_visit(node)
        self.parent_stack.pop()

    def visit_Name(self, node: ast.Name) -> None:
        # Only track loads (uses), not stores (assignments)
        if not isinstance(node.ctx, ast.Load):
            return

        # Skip if we're currently assigning to this variable
        # (to avoid treating LHS as a use in `x = x + 1`)
        if node.id in self.currently_assigning:
            return

        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        usage_has_await = any(isinstance(parent, ast.Await) for parent in self.parent_stack)
        in_fstring_expression = any(isinstance(parent, ast.FormattedValue) for parent in self.parent_stack)

        fstring_field_span: tuple[int, int] | None = None
        immediate_parent = self.parent_stack[-1] if self.parent_stack else None
        if (
            isinstance(immediate_parent, ast.FormattedValue)
            and immediate_parent.value is node
            and immediate_parent.conversion == -1
            and immediate_parent.format_spec is None
            and immediate_parent.lineno == immediate_parent.end_lineno == node.lineno
            and immediate_parent.end_col_offset is not None
        ):
            fstring_field_span = (immediate_parent.col_offset, immediate_parent.end_col_offset)

        # Name-load context isn't resolved to anything more specific than
        # "unknown" — that would need walking parent nodes with a real
        # parent-tracking system. Only the other two UsageContext values
        # (set by the assignment-visiting methods above) are ever compared.
        context: UsageContext = "unknown"

        usage = UsageInfo(
            var_name=node.id,
            line=node.lineno,
            col=node.col_offset,
            stmt_index=stmt_index,
            context=context,
            scope_id=scope_id,
            usage_has_await=usage_has_await,
            in_control_flow=self.control_flow_depth > 0,
            in_loop=self.loop_depth > 0,
            in_lambda=self.lambda_depth > 0,
            in_comprehension=self.comprehension_depth > 0,
            node=node,
            enclosing_stmt=self.current_stmt,
            in_fstring_expression=in_fstring_expression,
            fstring_field_span=fstring_field_span,
        )

        key = (scope_id, node.id)
        if key not in self.uses:
            self.uses[key] = []
        self.uses[key].append(usage)

    def build_lifecycles(self) -> list[VariableLifecycle]:
        lifecycles: list[VariableLifecycle] = []

        for (scope_id, var_name), assignment_list in self.assignments.items():
            for assignment in assignment_list:
                key = (scope_id, var_name)
                all_uses = self.uses.get(key, [])
                relevant_uses = [use for use in all_uses if use.stmt_index >= assignment.stmt_index]

                # Variables captured by closures should not be marked as redundant.
                child_scopes = self._get_child_scopes(scope_id)

                # A child scope's `nonlocal` declaration means the closure
                # captures and potentially modifies this variable, so the
                # outer assignment must not be flagged as redundant.
                is_captured_by_nonlocal = any(
                    (child_scope_id, var_name) in self.nonlocal_vars for child_scope_id in child_scopes
                )
                if is_captured_by_nonlocal:
                    continue

                for child_scope_id in child_scopes:
                    child_key = (child_scope_id, var_name)
                    child_uses = self.uses.get(child_key, [])
                    relevant_uses.extend(child_uses)

                # If there's a subsequent assignment to the same variable,
                # only include uses up to that assignment
                next_assignment = None
                for other_assignment in assignment_list:
                    if other_assignment.stmt_index > assignment.stmt_index and (
                        next_assignment is None or other_assignment.stmt_index < next_assignment.stmt_index
                    ):
                        next_assignment = other_assignment

                if next_assignment:
                    # Keep ALL child scope uses since they're closures, even
                    # past the next assignment.
                    relevant_uses = [
                        use
                        for use in relevant_uses
                        if use.stmt_index < next_assignment.stmt_index or use.scope_id in child_scopes
                    ]

                rhs_reference_reassigned_before_use = len(relevant_uses) == 1 and self._rhs_reference_reassigned(
                    assignment, relevant_uses[0], scope_id
                )

                lifecycle = VariableLifecycle(
                    assignment=assignment,
                    uses=relevant_uses,
                    rhs_reference_reassigned_before_use=rhs_reference_reassigned_before_use,
                )
                lifecycles.append(lifecycle)

        return lifecycles

    def _rhs_reference_reassigned(
        self,
        assignment: AssignmentInfo,
        use: UsageInfo,
        scope_id: int,
    ) -> bool:
        """True when `assignment`'s RHS reads a Name/Attribute reference
        that is itself reassigned (or augmented-assigned) between
        `assignment` and `use` — the "snapshot the old value before
        reassigning it" hazard from issue #74. Inlining the RHS text at
        `use` in that case would read the reference's new state instead of
        the one captured at assignment time, silently changing behavior.

        For a Name RHS (`old = x`), only rebinding `x` itself is unsafe —
        mutating the object `x` refers to (e.g. `x.attr = ...`) doesn't
        create this hazard, since `old` and `x` still alias the same
        object either way. For an Attribute RHS (`old = obj.attr`), both
        rebinding `obj` itself and reassigning any attribute/subscript of
        `obj` are treated as unsafe — the latter conservatively, since this
        tracker doesn't record *which* attribute was reassigned.

        See `_suspension_point_between` for the Call/Attribute suspension
        hazard and the method-call branch below for the receiver hazard.
        """
        rhs_node = assignment.rhs_node

        if isinstance(rhs_node, ast.Name):
            return self._reference_reassigned_in_range(
                rhs_node.id,
                scope_id,
                assignment.line,
                assignment.stmt_index,
                use.stmt_index,
                use.line,
                include_attribute_mutation=False,
            )

        if isinstance(rhs_node, ast.Attribute | ast.Call) and self._suspension_point_between(
            scope_id, assignment.line, assignment.stmt_index, assignment.col, use
        ):
            return True

        if isinstance(rhs_node, ast.Attribute):
            base = _unwind_to_base_name(rhs_node)
            if base is not None:
                return self._reference_reassigned_in_range(
                    base.id,
                    scope_id,
                    assignment.line,
                    assignment.stmt_index,
                    use.stmt_index,
                    use.line,
                    include_attribute_mutation=True,
                )

        if isinstance(rhs_node, ast.Call) and isinstance(rhs_node.func, ast.Attribute):
            base = _unwind_to_base_name(rhs_node.func)
            if base is not None:
                # Bisect from the RHS's own end line, not the assignment's
                # start line, and exclude the use's own statement (see
                # exclude_enclosing_stmt below) — both can reference `base`
                # again without that being a mutation.
                rhs_end_line = rhs_node.end_lineno if rhs_node.end_lineno is not None else assignment.line
                return self._reference_reassigned_in_range(
                    base.id,
                    scope_id,
                    rhs_end_line,
                    assignment.stmt_index,
                    use.stmt_index,
                    use.line,
                    include_attribute_mutation=True,
                    include_any_usage=True,
                    exclude_enclosing_stmt=use.enclosing_stmt,
                )

        return False

    def _suspension_point_between(
        self,
        scope_id: int,
        assign_line: int,
        assign_stmt_index: int,
        assign_col: int,
        use: UsageInfo,
    ) -> bool:
        """True if a yield/yield from/await occurs strictly after the
        assignment and before the use.

        `stmt_index` alone can't order two entries that tie on it, and a tie
        arises two different ways:

        - The suspension point sits in the use's own statement (e.g.
          `return (await refresh(), value)[1]`), where it could still
          evaluate before or after the use within that one statement — that
          case is resolved by `_suspension_precedes_use`'s real
          evaluation-order walk instead of assumed safe.
        - The suspension point sits in a *different*, known statement that's
          merely nested in the same coarse block as the use (an if/with/try
          body doesn't get its own stmt_index — see
          VariableTracker.suspension_points), so `_suspension_precedes_use`
          can't see it at all: that walk only looks inside the use's own
          statement. Ordinary top-to-bottom source position (line, then
          column) settles this instead — the point and the use are two
          distinct, unconditionally-sequenced statements within the same
          straight-line block, so whichever comes first textually also runs
          first (loop bodies that could revisit this point on a later
          iteration are excluded earlier, via `assignment.in_loop` in
          `should_report_violation`).

        A suspension point in a genuinely earlier statement (`stmt_index`
        strictly less) needs neither check: unlike a tie, nothing about
        *that* statement's evaluation order relative to the use is in
        question. And when `use.enclosing_stmt` itself is unknown (some
        UsageInfo variants, e.g. an augmented-assignment rebinding marker,
        never record it), same-vs-different can't be told apart, nor can
        source position stand in for it (the marker's own position is the
        rebound name, not necessarily where the hazard actually occurs), so
        this defers to `_suspension_precedes_use`'s own conservative
        fallback rather than guessing.

        The bisect boundary itself includes `assign_col`, not just
        `(assign_line, assign_stmt_index)` — semicolon-separated statements
        nested in the same coarse block (e.g. `cached = obj.attr; await
        other(); return cached`) can share both line and stmt_index with
        the assignment, and `bisect_right` would otherwise skip straight
        past a point that ties on both, the same way `line` alone once hid
        a same-line reassignment (see `_reference_reassigned_in_range`).
        """
        points = self.suspension_points.get(scope_id)
        if not points:
            return False
        start = bisect.bisect_right(
            points, (assign_line, assign_stmt_index, assign_col), key=lambda p: (p[0], p[1], p[2])
        )
        if start >= len(points):
            return False
        point_line, point_stmt_index, point_col, point_stmt = points[start]
        if point_stmt_index < use.stmt_index:
            return True
        if point_stmt_index == use.stmt_index:
            if use.enclosing_stmt is not None and point_stmt is not use.enclosing_stmt:
                return (point_line, point_col) < (use.line, use.col)
            return _suspension_precedes_use(use)
        return False

    def _reference_reassigned_in_range(
        self,
        name: str,
        scope_id: int,
        assign_line: int,
        assign_stmt_index: int,
        end_stmt_index: int,
        use_line: int,
        *,
        include_attribute_mutation: bool,
        include_any_usage: bool = False,
        exclude_enclosing_stmt: ast.stmt | None = None,
    ) -> bool:
        """`self.assignments[key]`/`self.uses[key]` are each built by a
        single top-to-bottom AST walk within one scope, so entries for a
        fixed `key` already arrive in non-decreasing `stmt_index` *and*
        `line` order — bisecting to the range start avoids rescanning
        every earlier assignment/use of `name`, which would otherwise make
        a long chain of single-use aliases to the same shared name (e.g.
        `v0 = shared` through `vN = shared`) quadratic in the number of
        aliases.

        The bisect start boundary is `(assign_line, assign_stmt_index)`,
        not either alone — `line` and `stmt_index` can each tie while the
        other still orders two entries correctly (see test_detect_redundancy's
        "sharing-coarse-stmt-index" and "sharing-a-physical-line" cases), so
        only the composite excludes exactly the assignment's own position
        (and nothing genuinely later) regardless of which one ties.

        `end_stmt_index` is still checked alongside `line` at the end of
        the range, as before: it excludes a candidate in a later, unrelated
        top-level statement even in the rare case `line` alone wouldn't.
        This stays a heuristic (may under-report/under-fix, never
        mis-fixes), not full control-flow analysis.

        `include_any_usage` treats *any* in-range use of `name` — not just
        an augmented-assignment or attribute/subscript-assignment context —
        as disqualifying. Used for a method-call RHS (`buf.tell()`), where
        the receiver can be mutated by another method call with no
        assignment syntax at all.

        `exclude_enclosing_stmt` skips a use belonging to that exact
        statement — the use's own statement reads `name` again just to
        reach the use (e.g. `obj` in `obj.consume(value)`, read to look up
        `.consume`), the same way the assignment's own statement reads it
        to reach the RHS. Neither is an intervening mutation.
        """
        key = (scope_id, name)
        start_key = (assign_line, assign_stmt_index)

        # Indexed iteration, not `some_list[start:]` — a slice eagerly
        # copies the entire remaining tail before the loop even starts,
        # which would silently reintroduce the O(N) cost per lookup (and
        # O(N^2) overall across N aliases) that bisecting to `start` was
        # meant to avoid, even though the loop itself `break`s almost
        # immediately in the common case.
        assignments = self.assignments.get(key)
        if assignments:
            start = bisect.bisect_right(assignments, start_key, key=lambda a: (a.line, a.stmt_index))
            for i in range(start, len(assignments)):
                other_assignment = assignments[i]
                if other_assignment.stmt_index > end_stmt_index or other_assignment.line > use_line:
                    break
                return True

        # Unlike `assignments` above, `uses` is never actually empty here:
        # visiting the RHS expression that got us into this method always
        # records at least a self-referential use of `name` (e.g. `value`
        # in `old = value`, or `obj` in `old = obj.attr`) at the
        # assignment's own line/stmt_index — bisected past below, but
        # enough to guarantee the list itself is non-empty.
        uses = self.uses.get(key, [])
        start = bisect.bisect_right(uses, start_key, key=lambda u: (u.line, u.stmt_index))
        for i in range(start, len(uses)):
            other_use = uses[i]
            if other_use.stmt_index > end_stmt_index or other_use.line > use_line:
                break
            if include_any_usage:
                if other_use.enclosing_stmt is exclude_enclosing_stmt:
                    continue
                return True
            if other_use.context == "augmented_assignment":
                return True
            if include_attribute_mutation and other_use.context == "attribute_or_subscript_assignment":
                return True

        return False


def detect_redundancy(lifecycle: VariableLifecycle) -> PatternType | None:
    if not lifecycle.is_single_use:
        return None

    # Variables captured by closures should NEVER be considered redundant.
    for use in lifecycle.uses:
        if use.scope_id != lifecycle.assignment.scope_id:
            return None

    # A Call/Attribute RHS with its single textual use inside a loop the
    # assignment itself isn't part of is reusing a value hoisted out of
    # the loop, not merely forwarding it once — inlining would turn one
    # evaluation into N (or zero). A Constant/Name RHS has no such risk
    # (re-evaluating either gives the identical value every time), so it's
    # excluded here the same way should_autofix already treats those two
    # kinds as unconditionally safe to inline everywhere.
    if (
        isinstance(lifecycle.assignment.rhs_node, ast.Attribute | ast.Call)
        and lifecycle.uses[0].in_loop
        and not lifecycle.assignment.in_loop
    ):
        return None

    # Augmented-assignment targets (x += 1) can never be inlined: the "use"
    # IS an assignment target, and replacing it with the RHS expression
    # produces invalid syntax (`x = 5; x += 1` -> `5 += 1`). This also isn't
    # the read-then-pass-through pattern TRI005 targets — the variable is
    # being mutated, not merely forwarded.
    for use in lifecycle.uses:
        if use.context == "augmented_assignment":
            return None

    # A "mutation-only" use (`state.attr = ...` or `state[key] = ...`) never
    # reads the assigned value at all, so it isn't a redundant pass-through
    # either — unlike the augmented-assignment case above, inlining it would
    # stay syntactically valid (`me.state(State).attr = ...`), but silently
    # change behavior whenever the RHS isn't guaranteed to return the same
    # object on a second evaluation (e.g. a factory/accessor rather than a
    # cached singleton) — something this tracker has no way to verify.
    for use in lifecycle.uses:
        if use.context == "attribute_or_subscript_assignment":
            return None

    # "Snapshot the old value before reassigning it" (issue #74): the RHS's
    # reference is itself reassigned/mutated between the assignment and its
    # use, so the two accesses observe genuinely different states — this
    # isn't a redundant assignment forwarding the same value at all.
    if lifecycle.rhs_reference_reassigned_before_use:
        return None

    # Pattern 3: Literal identity (e.g., foo = "foo")
    if _is_literal_identity(lifecycle):
        return PatternType.LITERAL_IDENTITY

    # Pattern 1: Immediate single use
    if lifecycle.is_immediate_use:
        return PatternType.IMMEDIATE_SINGLE_USE

    # Pattern 2: Single use anywhere
    return PatternType.SINGLE_USE


def _is_literal_identity(lifecycle: VariableLifecycle) -> bool:
    """True for a literal identity assignment, e.g. `foo = "foo"` (case- and underscore-insensitive)."""
    assignment = lifecycle.assignment
    rhs_node = assignment.rhs_node

    if isinstance(rhs_node, ast.Constant) and isinstance(rhs_node.value, str):
        var_name = assignment.var_name.lower()
        literal_value = rhs_node.value.lower()

        if var_name == literal_value:
            return True

        # Allow underscore differences too, e.g. variable FOO vs literal "foo"
        if var_name.replace("_", "") == literal_value.replace("_", ""):
            return True

    return False
