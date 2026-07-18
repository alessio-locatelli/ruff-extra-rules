"""Variable tracking and redundancy pattern detection for TRI005."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class PatternType(Enum):
    """Types of redundant assignment patterns."""

    IMMEDIATE_SINGLE_USE = auto()  # x = "foo"; func(x=x)
    SINGLE_USE = auto()  # x = calc(); return x
    LITERAL_IDENTITY = auto()  # foo = "foo"


@dataclass
class AssignmentInfo:
    """Tracks a variable assignment.

    Attributes:
        var_name: Variable name
        line: Line number of assignment
        col: Column offset of assignment
        stmt_index: Position in scope body (for distance calculation)
        rhs_node: Right-hand side AST node
        rhs_source: Right-hand side source code
        scope_id: Scope identifier for isolation
        has_type_annotation: Whether assignment has type annotation
        in_loop: Whether assignment is inside a loop
        in_control_flow: Whether assignment is inside control flow (if/try/with)
        in_global_scope: Whether assignment is in global/module scope
        has_comment_above: Whether there's a comment right above the assignment
        has_inline_comment: Whether there's an inline comment on the assignment line
        rhs_has_await: Whether the RHS contains an await expression
    """

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


@dataclass
class UsageInfo:
    """Tracks a variable usage.

    Attributes:
        var_name: Variable name
        line: Line number of usage
        col: Column offset of usage
        stmt_index: Position in scope body
        context: Usage context ('return', 'call', 'operation', etc.')
        scope_id: Scope identifier for isolation
        usage_has_await: Whether the usage is wrapped in an await expression
        in_control_flow: Whether usage is inside control flow (if/try/with/match)
        in_loop: Whether usage is inside a loop (for/while), independent of
            whether the *assignment* is — used to reject inlining a call
            whose single textual use would actually execute repeatedly
        in_lambda: Whether usage is inside a lambda body — used to reject
            inlining a call whose single textual use would actually execute
            later (and possibly repeatedly) at call time, not once at the
            original assignment point
        node: The exact AST node for this use (identity, not just position).
            None for usage kinds that don't track it.
        enclosing_stmt: The statement containing `node`, for
            `analysis.is_preceded_by_call`. Stored (not eagerly evaluated)
            so the O(statement size) evaluation-order walk only runs for the
            rare usages that actually need it (should_autofix's
            zero-arg-call carve-out) instead of every Name-load in the file.
    """

    var_name: str
    line: int
    col: int
    stmt_index: int
    context: str
    scope_id: int
    usage_has_await: bool = False
    in_control_flow: bool = False
    in_loop: bool = False
    in_lambda: bool = False
    in_comprehension: bool = False
    node: ast.expr | None = None
    enclosing_stmt: ast.stmt | None = None


@dataclass
class VariableLifecycle:
    """Complete lifecycle of a variable in its scope.

    Attributes:
        assignment: Assignment information
        uses: List of all uses of the variable
    """

    assignment: AssignmentInfo
    uses: list[UsageInfo]

    @property
    def is_single_use(self) -> bool:
        """Check if variable is used exactly once."""
        return len(self.uses) == 1

    @property
    def is_immediate_use(self) -> bool:
        """Check if first use is within 0-1 statements from assignment.

        Uses in different scopes (closures) are never considered immediate,
        even if their statement index appears close, because they're in
        nested functions and the variable is captured by the closure.
        """
        if not self.uses:
            return False
        first_use = self.uses[0]

        # If the use is in a different scope, it's a closure - not immediate
        if first_use.scope_id != self.assignment.scope_id:
            return False

        # Immediate = same statement or next statement, same scope
        return first_use.stmt_index <= self.assignment.stmt_index + 1


def _has_await_expression(node: ast.expr) -> bool:
    """Check if an AST node contains an await expression.

    Args:
        node: AST expression node

    Returns:
        True if node contains await expression
    """

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

    Args:
        node: Node whose children to yield

    Returns:
        (child, is_conditional) pairs in true evaluation order
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


def _call_precedes_target(node: ast.AST, target: ast.AST) -> tuple[bool, bool, bool]:
    """Walk `node`'s children in evaluation order looking for `target`.

    It's AST-based rather than text/line-based specifically so it stays
    correct across multi-line statements, where a sibling operand's
    physical line/column says nothing about evaluation order.

    Args:
        node: Subtree to search
        target: The exact node instance being searched for (identity, not
            structural equality)

    Returns:
        A (found, effect_before_target, node_is_or_contains_effect) triple:
        - found: whether `target` is `node` itself or within its subtree
        - effect_before_target: whether a potentially effectful node (see
          `_POTENTIALLY_EFFECTFUL_NODE_TYPES`) fully evaluated before
          reaching `target`, OR `target` is only conditionally reachable
          (see `_evaluation_order_children`) — only meaningful when
          `found` is True
        - node_is_or_contains_effect: whether `node` itself is (or
          contains) a potentially effectful node that has fully evaluated —
          only meaningful when `found` is False, since a call containing
          `target` among its own arguments doesn't fire until after
          `target` (and everything else in it) is evaluated
    """
    if node is target:
        return True, False, False

    seen_effect = False
    for child, is_conditional in _evaluation_order_children(node):
        found, effect_before, child_has_effect = _call_precedes_target(child, target)
        if found:
            return True, seen_effect or effect_before or is_conditional, False
        if child_has_effect:
            seen_effect = True

    return (
        False,
        False,
        seen_effect or isinstance(node, _POTENTIALLY_EFFECTFUL_NODE_TYPES),
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

    Args:
        use: The usage to check

    Returns:
        True if something potentially effectful precedes `use`, or
        `use.node`/`use.enclosing_stmt` is unavailable so safety can't be
        verified (conservative default)
    """
    if use.node is None or use.enclosing_stmt is None:
        return True
    _found, effect_before, _ = _call_precedes_target(use.enclosing_stmt, use.node)
    return effect_before


def _has_comment_above(line_number: int, source_lines: list[str]) -> bool:
    """Check if there's a comment right above the given line.

    Args:
        line_number: Line number (1-indexed)
        source_lines: List of source code lines

    Returns:
        True if there's a comment on the line directly above
    """
    if line_number <= 1 or line_number > len(source_lines):
        return False

    # Check the line above (convert to 0-indexed)
    prev_line = source_lines[line_number - 2].strip()

    # Check if it's a comment line (starts with #)
    return prev_line.startswith("#")


def _has_inline_comment(line_number: int, source_lines: list[str]) -> bool:
    """Check if there's an inline comment on the given line.

    Args:
        line_number: Line number (1-indexed)
        source_lines: List of source code lines

    Returns:
        True if there's an inline comment on the line
    """
    if line_number < 1 or line_number > len(source_lines):
        return False

    # Get the line (convert to 0-indexed)
    line = source_lines[line_number - 1]

    # Simple check: look for # that's not inside a string
    # This is a heuristic - a full solution would need tokenization
    # but for our purposes, checking if '#' appears after code is sufficient
    in_string = False
    string_char = None
    for i, char in enumerate(line):
        if char in ('"', "'") and (i == 0 or line[i - 1] != "\\"):
            if not in_string:
                in_string = True
                string_char = char
            elif char == string_char:
                in_string = False
        elif char == "#" and not in_string:
            return True

    return False


class VariableTracker(ast.NodeVisitor):
    """Tracks variable assignments and uses across scopes.

    This visitor traverses the AST and builds a comprehensive map of variable
    lifecycles, tracking where variables are assigned and where they're used.
    """

    def __init__(self, source: str) -> None:
        """Initialize the tracker.

        Args:
            source: Source code being analyzed
        """
        self.source = source
        self.source_lines = source.splitlines()

        # Scope tracking
        self.current_scope_id = 0
        self.scope_stack: list[int] = [0]  # Start with module scope

        # Statement index tracking within current scope
        self.stmt_index_stack: list[int] = [0]

        # Track assignments: (scope_id, var_name) -> list[AssignmentInfo]
        self.assignments: dict[tuple[int, str], list[AssignmentInfo]] = {}

        # Track uses: (scope_id, var_name) -> list[UsageInfo]
        self.uses: dict[tuple[int, str], list[UsageInfo]] = {}

        # Track global/nonlocal declarations to skip them
        self.global_vars: set[tuple[int, str]] = set()
        self.nonlocal_vars: set[tuple[int, str]] = set()

        # Track which names are currently being assigned (to avoid treating
        # the LHS of an assignment as a use)
        self.currently_assigning: set[str] = set()

        # Track if we're inside a loop (for, while)
        self.loop_depth = 0

        # Track if we're inside control flow (if, try, with, match)
        self.control_flow_depth = 0

        # Track if we're inside a comprehension (list/set/dict/generator)
        self.comprehension_depth = 0

        # Track if we're inside a lambda body — a use here executes later
        # (and possibly repeatedly, or never) at call time, not once at the
        # point the lambda is defined
        self.lambda_depth = 0

        # Track parent nodes for context detection
        self.parent_stack: list[ast.AST] = []

        # Innermost enclosing statement of whatever node is currently being
        # visited, updated in visit() below. Stored on each UsageInfo (not
        # searched here) so is_preceded_by_call's evaluation-order walk can
        # run lazily, later, only for the rare usages that need it.
        self.current_stmt: ast.stmt | None = None

        # Track parent-child scope relationships for closure detection
        # Maps child_scope_id -> parent_scope_id
        self.scope_parents: dict[int, int] = {}

    def _enter_scope(self) -> None:
        """Enter a new scope (function, class, etc.)."""
        parent_scope_id = self._get_current_scope_id()
        self.current_scope_id += 1
        child_scope_id = self.current_scope_id

        # Track parent-child relationship
        self.scope_parents[child_scope_id] = parent_scope_id

        self.scope_stack.append(child_scope_id)
        self.stmt_index_stack.append(0)

    def _exit_scope(self) -> None:
        """Exit the current scope."""
        self.scope_stack.pop()
        self.stmt_index_stack.pop()

    def _increment_stmt_index(self) -> None:
        """Increment the statement index in the current scope.

        stmt_index_stack is initialized with [0] and only ever grows/shrinks
        in balanced pairs via _enter_scope/_exit_scope, so it's never empty.
        """
        self.stmt_index_stack[-1] += 1

    def _get_current_scope_id(self) -> int:
        """Get the current scope ID."""
        return self.scope_stack[-1] if self.scope_stack else 0

    def _get_current_stmt_index(self) -> int:
        """Get the current statement index."""
        return self.stmt_index_stack[-1] if self.stmt_index_stack else 0

    def _get_child_scopes(self, scope_id: int) -> list[int]:
        """Get all direct and indirect child scopes of a given scope.

        This is used to detect closures - variables assigned in an outer scope
        but used in nested function scopes.

        Args:
            scope_id: Parent scope ID

        Returns:
            List of all descendant scope IDs
        """
        children = []
        # Find all direct children
        for child_id, parent_id in self.scope_parents.items():
            if parent_id == scope_id:
                children.append(child_id)
                # Recursively get grandchildren
                children.extend(self._get_child_scopes(child_id))
        return children

    def _get_source_segment(self, node: ast.expr) -> str:
        """Get source code for an AST node.

        Args:
            node: AST node

        Returns:
            Source code string, or empty string if unavailable
        """
        try:
            return ast.get_source_segment(self.source, node) or ""
        # Defensive: get_source_segment slices source by byte offset and
        # decodes it, which could raise (ValueError/UnicodeDecodeError, or
        # TypeError) if a node's position were ever inconsistent with this
        # source — not expected for a node resolved against its own tree.
        except ValueError, TypeError:  # pragma: no cover
            return ""

    def _is_simple_name_target(self, target: ast.expr) -> bool:
        """Check if assignment target is a simple name (not tuple, attribute, etc.).

        Args:
            target: Assignment target node

        Returns:
            True if target is a simple name
        """
        return isinstance(target, ast.Name)

    def visit_Global(self, node: ast.Global) -> None:
        """Track global declarations."""
        scope_id = self._get_current_scope_id()
        for name in node.names:
            self.global_vars.add((scope_id, name))
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        """Track nonlocal declarations."""
        scope_id = self._get_current_scope_id()
        for name in node.names:
            self.nonlocal_vars.add((scope_id, name))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Visit function definition (enter new scope).

        Decorators are evaluated in the outer (enclosing) scope before the
        function body, so they must be visited before entering the new scope.

        Args:
            node: Function definition node
        """
        # Visit decorators in the OUTER scope — they're evaluated there
        for decorator in node.decorator_list:
            self.visit(decorator)

        self._enter_scope()

        # Visit function body
        for stmt in node.body:
            self.visit(stmt)
            self._increment_stmt_index()

        self._exit_scope()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_For(self, node: ast.For) -> None:
        """Visit for loop (track loop depth).

        Args:
            node: For loop node
        """
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_While(self, node: ast.While) -> None:
        """Visit while loop (track loop depth).

        Args:
            node: While loop node
        """
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """Visit async for loop (track loop depth).

        Args:
            node: Async for loop node
        """
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        """Visit if statement (track control flow depth).

        Args:
            node: If statement node
        """
        self.control_flow_depth += 1
        self.generic_visit(node)
        self.control_flow_depth -= 1

    def visit_Try(self, node: ast.Try) -> None:
        """Visit try statement (track control flow depth).

        Args:
            node: Try statement node
        """
        self.control_flow_depth += 1
        self.generic_visit(node)
        self.control_flow_depth -= 1

    def visit_With(self, node: ast.With) -> None:
        """Visit with statement (track control flow depth).

        Args:
            node: With statement node
        """
        self.control_flow_depth += 1
        self.generic_visit(node)
        self.control_flow_depth -= 1

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """Visit async with statement (track control flow depth).

        Args:
            node: Async with statement node
        """
        self.control_flow_depth += 1
        self.generic_visit(node)
        self.control_flow_depth -= 1

    def visit_Match(self, node: ast.Match) -> None:
        """Visit match statement (track control flow depth).

        Each case body only runs conditionally (if its pattern/guard
        matches), same as an if/elif branch.

        Args:
            node: Match statement node
        """
        self.control_flow_depth += 1
        self.generic_visit(node)
        self.control_flow_depth -= 1

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Visit lambda expression (track lambda depth).

        A lambda's body doesn't execute where it's defined — it executes
        later, whenever (and however many times, including zero) the
        lambda is called. Uses inside it must never be treated as
        "the same execution point" as the surrounding statement.

        Args:
            node: Lambda expression node
        """
        self.lambda_depth += 1
        self.generic_visit(node)
        self.lambda_depth -= 1

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    ) -> None:
        """Visit a comprehension node (track comprehension depth).

        Args:
            node: Comprehension node (list, set, dict, or generator)
        """
        self.comprehension_depth += 1
        self.generic_visit(node)
        self.comprehension_depth -= 1

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """Visit list comprehension.

        Args:
            node: List comprehension node
        """
        self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """Visit set comprehension.

        Args:
            node: Set comprehension node
        """
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """Visit generator expression.

        Args:
            node: Generator expression node
        """
        self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """Visit dict comprehension.

        Args:
            node: Dict comprehension node
        """
        self._visit_comprehension(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit class definition (enter new scope).

        We skip class-level assignments as they're attributes, not local variables.
        Decorators are evaluated in the outer scope before the class body.

        Args:
            node: Class definition node
        """
        # Visit decorators in the OUTER scope — they're evaluated there
        for decorator in node.decorator_list:
            self.visit(decorator)

        self._enter_scope()

        # Visit class body but don't track assignments at class level
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self.visit(stmt)
            # Skip direct assignments in class body (class attributes)

        self._exit_scope()

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track simple assignments.

        Args:
            node: Assignment node
        """
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        # Skip multiple assignments on a single line (e.g., a = b = c = value)
        # These patterns often intentionally assign intermediate variables
        # and avoid re-reading class attributes
        if len(node.targets) > 1:
            # Still visit RHS to track any variable uses
            self.visit(node.value)
            return

        # Only track simple name assignments (not tuple unpacking, attributes, etc.)
        for target in node.targets:
            if self._is_simple_name_target(target):
                assert isinstance(target, ast.Name)  # Type narrowing
                var_name = target.id

                # Skip global/nonlocal variables
                if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                    continue

                # Mark as currently assigning to avoid treating LHS as use
                self.currently_assigning.add(var_name)

                # Get RHS source
                rhs_source = self._get_source_segment(node.value)

                # Create assignment info
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
                    has_comment_above=_has_comment_above(node.lineno, self.source_lines),
                    has_inline_comment=_has_inline_comment(node.lineno, self.source_lines),
                    rhs_has_await=_has_await_expression(node.value),
                )

                # Store assignment
                key = (scope_id, var_name)
                if key not in self.assignments:
                    self.assignments[key] = []
                self.assignments[key].append(assignment)
            elif isinstance(target, ast.Attribute | ast.Subscript):
                # Track attribute/subscript assignments
                # (e.g., obj.attr = value, obj[key] = value)
                # The base object is being USED, so we need to track it
                self._track_attribute_or_subscript_base_usage(target, stmt_index)

        # Visit RHS to track any variable uses
        self.visit(node.value)

        # Clear currently assigning
        self.currently_assigning.clear()

    def _track_attribute_or_subscript_base_usage(self, node: ast.Attribute | ast.Subscript, stmt_index: int) -> None:
        """Track usage of base variable in attribute/subscript assignments.

        When we have `obj.attr = value` or `obj[key] = value`, the `obj` variable
        is being USED (read), so we need to track it as a usage.

        Args:
            node: Attribute or Subscript node
            stmt_index: Current statement index
        """
        scope_id = self._get_current_scope_id()

        # Find the base variable
        base: ast.expr = node
        while isinstance(base, ast.Attribute | ast.Subscript):
            # Both Attribute and Subscript have .value as the base
            base = base.value

        # If the base is a simple name, track it as a usage
        if isinstance(base, ast.Name):
            var_name = base.id

            # Skip if this is a global/nonlocal variable
            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                return

            # Track as usage
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
        """Track annotated assignments.

        Args:
            node: Annotated assignment node
        """
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        # Only track simple name assignments
        if self._is_simple_name_target(node.target) and node.value is not None:
            assert isinstance(node.target, ast.Name)  # Type narrowing
            var_name = node.target.id

            # Skip global/nonlocal variables
            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                return

            # Mark as currently assigning
            self.currently_assigning.add(var_name)

            # Get RHS source
            rhs_source = self._get_source_segment(node.value)

            # Create assignment info
            assignment = AssignmentInfo(
                var_name=var_name,
                line=node.lineno,
                col=node.col_offset,
                stmt_index=stmt_index,
                rhs_node=node.value,
                rhs_source=rhs_source,
                scope_id=scope_id,
                has_type_annotation=True,  # This is an annotated assignment
                in_loop=self.loop_depth > 0,
                in_control_flow=self.control_flow_depth > 0,
                in_global_scope=(scope_id == 0),
                has_comment_above=_has_comment_above(node.lineno, self.source_lines),
                has_inline_comment=_has_inline_comment(node.lineno, self.source_lines),
                rhs_has_await=_has_await_expression(node.value),
            )

            # Store assignment
            key = (scope_id, var_name)
            if key not in self.assignments:
                self.assignments[key] = []
            self.assignments[key].append(assignment)

            # Visit RHS
            self.visit(node.value)

            # Clear currently assigning
            self.currently_assigning.clear()

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """Track augmented assignments (+=, -=, etc.).

        Augmented assignments READ the variable (to get current value) and then
        mutate it in place. We track the READ as a usage, which prevents false
        positives for patterns like:
            if condition:
                msg = "foo"
            else:
                msg = "bar"
            msg += " suffix"  # This USES the conditional value

        We don't track augmented assignments as NEW assignments because they're
        mutations of existing variables, not fresh assignments that could be
        inlined.

        Args:
            node: Augmented assignment node
        """
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        # Only track simple name targets
        if self._is_simple_name_target(node.target):
            assert isinstance(node.target, ast.Name)  # Type narrowing
            var_name = node.target.id

            # Skip global/nonlocal variables
            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                self.generic_visit(node)
                return

            # Track the READ (use) of the current value
            usage = UsageInfo(
                var_name=var_name,
                line=node.lineno,
                col=node.col_offset,
                stmt_index=stmt_index,
                context="augmented_assignment",
                scope_id=scope_id,
                in_control_flow=self.control_flow_depth > 0,
            )
            key = (scope_id, var_name)
            if key not in self.uses:
                self.uses[key] = []
            self.uses[key].append(usage)

        # Visit RHS to track any uses of other variables
        self.visit(node.value)

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
        """Visit a node and track parent context."""
        self.parent_stack.append(node)
        super().generic_visit(node)
        self.parent_stack.pop()

    def visit_Name(self, node: ast.Name) -> None:
        """Track variable uses (loads).

        Args:
            node: Name node
        """
        # Only track loads (uses), not stores (assignments)
        if not isinstance(node.ctx, ast.Load):
            return

        # Skip if we're currently assigning to this variable
        # (to avoid treating LHS as a use in `x = x + 1`)
        if node.id in self.currently_assigning:
            return

        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        # Check if this usage is wrapped in an await expression
        usage_has_await = any(isinstance(parent, ast.Await) for parent in self.parent_stack)

        # Determine context
        context = "unknown"
        # Walk up parent nodes to determine context
        # (This is a simplified version - a full implementation would use
        # a parent tracking system)

        # Create usage info
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
        )

        # Store usage
        key = (scope_id, node.id)
        if key not in self.uses:
            self.uses[key] = []
        self.uses[key].append(usage)

    def build_lifecycles(self) -> list[VariableLifecycle]:
        """Build variable lifecycles from tracked assignments and uses.

        Returns:
            List of variable lifecycles
        """
        lifecycles: list[VariableLifecycle] = []

        # For each assignment, find its corresponding uses
        for (scope_id, var_name), assignment_list in self.assignments.items():
            # For each assignment to this variable
            for assignment in assignment_list:
                # Find uses of this variable in the same scope after this assignment
                key = (scope_id, var_name)
                all_uses = self.uses.get(key, [])

                # Filter uses that come after this assignment
                # (by comparing statement indices)
                relevant_uses = [use for use in all_uses if use.stmt_index >= assignment.stmt_index]

                # CLOSURE DETECTION: Also check for uses in nested scopes
                # Variables captured by closures should not be marked as redundant
                child_scopes = self._get_child_scopes(scope_id)

                # Check if variable is declared nonlocal in any child scope
                # This means the closure captures and potentially modifies it,
                # so we should not flag the outer assignment as redundant
                is_captured_by_nonlocal = any(
                    (child_scope_id, var_name) in self.nonlocal_vars for child_scope_id in child_scopes
                )
                if is_captured_by_nonlocal:
                    # Skip this assignment entirely - it's captured by a closure
                    continue

                for child_scope_id in child_scopes:
                    child_key = (child_scope_id, var_name)
                    child_uses = self.uses.get(child_key, [])
                    # Add all uses from child scopes (closures)
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
                    # Only include uses before the next assignment
                    # (but keep ALL child scope uses since they're closures)
                    relevant_uses = [
                        use
                        for use in relevant_uses
                        if use.stmt_index < next_assignment.stmt_index or use.scope_id in child_scopes
                    ]

                # Create lifecycle
                lifecycle = VariableLifecycle(
                    assignment=assignment,
                    uses=relevant_uses,
                )
                lifecycles.append(lifecycle)

        return lifecycles


def detect_redundancy(lifecycle: VariableLifecycle) -> PatternType | None:
    """Detect if a variable lifecycle represents a redundant assignment.

    Args:
        lifecycle: Variable lifecycle to analyze

    Returns:
        Pattern type if redundant, None otherwise
    """
    # Must be single use
    if not lifecycle.is_single_use:
        return None

    # Check if variable is used in a closure (different scope)
    # Variables captured by closures should NEVER be considered redundant
    for use in lifecycle.uses:
        if use.scope_id != lifecycle.assignment.scope_id:
            # This is a closure - variable is captured by nested function
            return None

    # Augmented-assignment targets (x += 1) can never be inlined: the "use"
    # IS an assignment target, and replacing it with the RHS expression
    # produces invalid syntax (`x = 5; x += 1` -> `5 += 1`). This also isn't
    # the read-then-pass-through pattern TRI005 targets — the variable is
    # being mutated, not merely forwarded.
    for use in lifecycle.uses:
        if use.context == "augmented_assignment":
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
    """Check if assignment is a literal identity (e.g., foo = "foo").

    Args:
        lifecycle: Variable lifecycle to check

    Returns:
        True if literal identity
    """
    assignment = lifecycle.assignment
    rhs_node = assignment.rhs_node

    # Check if RHS is a string literal
    if isinstance(rhs_node, ast.Constant) and isinstance(rhs_node.value, str):
        # Check if variable name matches literal value (case-insensitive)
        var_name = assignment.var_name.lower()
        literal_value = rhs_node.value.lower()

        # Direct match or variable name matches literal
        if var_name == literal_value:
            return True

        # Also check if variable name is literal with case changes
        # (e.g., FOO = "foo")
        if var_name.replace("_", "") == literal_value.replace("_", ""):
            return True

    return False
