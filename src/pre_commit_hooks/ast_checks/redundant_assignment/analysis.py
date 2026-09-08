from __future__ import annotations

import ast
import bisect
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Literal

from pre_commit_hooks.ast_checks._base import fast_get_source_segment, split_lines_like_ast

if TYPE_CHECKING:
    from collections.abc import Iterator

type UsageContext = Literal["attribute_or_subscript_assignment", "augmented_assignment", "deletion", "unknown"]


class PatternType(Enum):
    IMMEDIATE_SINGLE_USE = auto()
    SINGLE_USE = auto()
    LITERAL_IDENTITY = auto()


@dataclass(slots=True)
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
    in_try: bool = False
    in_global_scope: bool = False
    has_comment_above: bool = False
    has_inline_comment: bool = False
    rhs_has_await: bool = False
    is_rebinding_marker: bool = False


@dataclass(slots=True)
class UsageInfo:
    var_name: str
    line: int
    col: int
    stmt_index: int
    context: UsageContext
    scope_id: int
    usage_has_await: bool = False
    in_control_flow: bool = False
    in_loop: bool = False
    in_lambda: bool = False
    in_comprehension: bool = False
    node: ast.expr | None = None
    enclosing_stmt: ast.stmt | None = None
    in_fstring_expression: bool = False
    fstring_field_span: tuple[int, int] | None = None
    is_keyword_argument_echo: bool = False
    is_positional_argument_echo: bool = False
    is_call_argument_with_rebindable_callee: bool = False


@dataclass(slots=True)
class VariableLifecycle:
    assignment: AssignmentInfo
    uses: list[UsageInfo]
    rhs_reference_reassigned_before_use: bool = False

    @property
    def is_single_use(self) -> bool:
        return len(self.uses) == 1

    @property
    def is_immediate_use(self) -> bool:
        if not self.uses:
            return False
        first_use = self.uses[0]

        if first_use.scope_id != self.assignment.scope_id:
            return False

        return first_use.stmt_index <= self.assignment.stmt_index + 1


def _parameter_names(arguments: ast.arguments) -> set[str]:
    names = {arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _unwind_to_base_name(node: ast.expr) -> ast.Name | None:
    base = node
    while isinstance(base, ast.Attribute | ast.Subscript):
        base = base.value
    return base if isinstance(base, ast.Name) else None


def _unwind_attribute_chain_to_base_name(node: ast.expr) -> ast.Name | None:
    base = node
    while isinstance(base, ast.Attribute):
        base = base.value
    return base if isinstance(base, ast.Name) else None


def _has_await_expression(node: ast.expr) -> bool:
    class AwaitDetector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.has_await = False

        def visit_Await(self, node: ast.Await) -> None:  # noqa: ARG002
            self.has_await = True

    detector = AwaitDetector()
    detector.visit(node)
    return detector.has_await


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
    if use.node is None or use.enclosing_stmt is None:
        return True
    _found, effect_before, _ = _call_precedes_target(use.enclosing_stmt, use.node)
    return effect_before


_SUSPENSION_NODE_TYPES = (ast.Yield, ast.YieldFrom, ast.Await)


def _suspension_precedes_use(use: UsageInfo) -> bool:
    if use.node is None or use.enclosing_stmt is None:
        return True
    _found, effect_before, _ = _call_precedes_target(use.enclosing_stmt, use.node, _SUSPENSION_NODE_TYPES)
    return effect_before


def _collect_module_binding_facts(tree: ast.Module) -> tuple[bool, dict[str, int], set[str]]:
    has_wildcard_import = False
    function_name_counts: dict[str, int] = {}
    shadowing: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function_name_counts[node.name] = function_name_counts.get(node.name, 0) + 1
        elif isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            has_wildcard_import = True
        elif isinstance(node, ast.arg):
            shadowing.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            shadowing.add(node.id)
        elif isinstance(node, ast.alias):
            shadowing.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name is not None:
            shadowing.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            shadowing.add(node.rest)
        elif isinstance(node, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple | ast.ClassDef):  # noqa: SIM114
            shadowing.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            shadowing.add(node.name)
    return has_wildcard_import, function_name_counts, shadowing


def _index_unique_undecorated_functions(
    tree: ast.Module,
    *,
    has_wildcard_import: bool,
    all_function_name_counts: dict[str, int],
    shadowed: set[str],
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    if has_wildcard_import:
        return {}

    top_level_by_name: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            top_level_by_name.setdefault(stmt.name, []).append(stmt)

    return {
        name: nodes[0]
        for name, nodes in top_level_by_name.items()
        if len(nodes) == 1
        and not nodes[0].decorator_list
        and all_function_name_counts[name] == 1
        and name not in shadowed
    }


class VariableTracker(ast.NodeVisitor):
    def __init__(
        self, source: str, comment_only_lines: set[int], trailing_comment_lines: set[int], tree: ast.Module
    ) -> None:
        self.source = source
        self.source_lines = source.splitlines()
        self._ast_lines = split_lines_like_ast(source)
        self._comment_only_lines = comment_only_lines
        self._trailing_comment_lines = trailing_comment_lines

        has_wildcard_import, function_name_counts, shadowed_names = _collect_module_binding_facts(tree)
        self.functions = _index_unique_undecorated_functions(
            tree,
            has_wildcard_import=has_wildcard_import,
            all_function_name_counts=function_name_counts,
            shadowed=shadowed_names,
        )
        self.shadowed_names = shadowed_names
        self.has_wildcard_import = has_wildcard_import
        self.function_name_counts = function_name_counts
        self._call_positional_info: dict[int, tuple[bool, dict[int, int]]] = {}

        self.current_scope_id = 0
        self.scope_stack: list[int] = [0]
        self.stmt_index_stack: list[int] = [0]
        self.assignments: dict[tuple[int, str], list[AssignmentInfo]] = {}
        self.uses: dict[tuple[int, str], list[UsageInfo]] = {}
        self.suspension_points: dict[int, list[tuple[int, int, int, ast.stmt | None]]] = {}
        self.global_vars: set[tuple[int, str]] = set()
        self.nonlocal_vars: set[tuple[int, str]] = set()
        self.scope_locals: dict[int, set[str]] = {}

        self.currently_assigning: set[str] = set()

        self.loop_depth = 0

        self.control_flow_depth = 0

        self.try_depth = 0

        self.comprehension_depth = 0

        self.lambda_depth = 0

        self.parent_stack: list[ast.AST] = []

        self.current_stmt: ast.stmt | None = None

        self.scope_parents: dict[int, int] = {}
        self.scope_children: dict[int, list[int]] = {}
        self.class_scope_ids: set[int] = set()

    def _enter_scope(self) -> None:
        parent_scope_id = self._get_current_scope_id()
        self.current_scope_id += 1
        child_scope_id = self.current_scope_id

        self.scope_parents[child_scope_id] = parent_scope_id
        self.scope_children.setdefault(parent_scope_id, []).append(child_scope_id)

        self.scope_stack.append(child_scope_id)
        self.stmt_index_stack.append(0)

    def _exit_scope(self) -> None:
        self.scope_stack.pop()
        self.stmt_index_stack.pop()

    def _increment_stmt_index(self) -> None:
        self.stmt_index_stack[-1] += 1

    def _get_current_scope_id(self) -> int:
        return self.scope_stack[-1] if self.scope_stack else 0

    def _get_current_stmt_index(self) -> int:
        return self.stmt_index_stack[-1] if self.stmt_index_stack else 0

    def _register_local_binding(self, scope_id: int, var_name: str) -> None:
        if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
            return
        self.scope_locals.setdefault(scope_id, set()).add(var_name)

    def _scope_has_local_binding(self, scope_id: int, var_name: str) -> bool:
        if self.assignments.get((scope_id, var_name)):
            return True
        return var_name in self.scope_locals.get(scope_id, ())

    def _get_closure_reachable_scopes(self, scope_id: int, var_name: str) -> list[int]:
        reachable: list[int] = []
        frontier = list(self.scope_children.get(scope_id, ()))
        while frontier:
            next_frontier: list[int] = []
            for child_id in frontier:
                # A class body's own bindings (methods included) never shadow names for the
                # methods nested inside it -- Python's LEGB lookup skips class scopes entirely.
                is_class_scope = child_id in self.class_scope_ids
                if not is_class_scope and self._scope_has_local_binding(child_id, var_name):
                    continue
                reachable.append(child_id)
                next_frontier.extend(self.scope_children.get(child_id, ()))
            frontier = next_frontier
        return reachable

    def _get_source_segment(self, node: ast.expr) -> str:
        return fast_get_source_segment(self.source, self._ast_lines, node) or ""

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

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self._register_local_binding(self._get_current_scope_id(), node.name.id)
        self.generic_visit(node)

    def _visit_defaults(self, arguments: ast.arguments) -> None:
        for default in (*arguments.defaults, *arguments.kw_defaults):
            if default is not None:
                self.visit(default)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._register_local_binding(self._get_current_scope_id(), node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_defaults(node.args)

        self._enter_scope()
        self.scope_locals[self._get_current_scope_id()] = _parameter_names(node.args) | {
            type_param.name
            for type_param in node.type_params
            if isinstance(type_param, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
        }
        try_depth = self.try_depth
        self.try_depth = 0

        for stmt in node.body:
            self.visit(stmt)
            self._increment_stmt_index()

        self.try_depth = try_depth
        self._exit_scope()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
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
        self.parent_stack.append(node)
        self.visit(node.test)
        self.control_flow_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)
        self.control_flow_depth -= 1
        self.parent_stack.pop()

    def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
        self.parent_stack.append(node)
        self.control_flow_depth += 1
        self.try_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self.try_depth -= 1

        for handler in node.handlers:
            self.visit(handler)
        for stmt in node.orelse:
            self.visit(stmt)
        for stmt in node.finalbody:
            self.visit(stmt)

        self.control_flow_depth -= 1
        self.parent_stack.pop()

    visit_TryStar = visit_Try  # noqa: N815

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self._register_local_binding(self._get_current_scope_id(), node.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import | ast.ImportFrom) -> None:
        scope_id = self._get_current_scope_id()
        for alias in node.names:
            self._register_local_binding(scope_id, (alias.asname or alias.name).split(".")[0])
        self.generic_visit(node)

    visit_ImportFrom = visit_Import  # noqa: N815

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        self.control_flow_depth += 1
        stmt_index = self._get_current_stmt_index()
        for item in node.items:
            if item.optional_vars is not None:
                self._record_compound_target_rebindings(item.optional_vars, stmt_index)
        self.generic_visit(node)
        self.control_flow_depth -= 1

    visit_AsyncWith = visit_With  # noqa: N815

    def visit_Match(self, node: ast.Match) -> None:
        self.parent_stack.append(node)
        self.visit(node.subject)
        self.control_flow_depth += 1
        for case in node.cases:
            self.visit(case)
        self.control_flow_depth -= 1
        self.parent_stack.pop()

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self._register_local_binding(self._get_current_scope_id(), node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self._register_local_binding(self._get_current_scope_id(), node.name)
        self.generic_visit(node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self._register_local_binding(self._get_current_scope_id(), node.rest)
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
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
        self._register_local_binding(self._get_current_scope_id(), node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)

        self._enter_scope()
        self.class_scope_ids.add(self._get_current_scope_id())

        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self.visit(stmt)

        self._exit_scope()

    def visit_Assign(self, node: ast.Assign) -> None:
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        if len(node.targets) > 1:
            for target in node.targets:
                self._record_compound_target_rebindings(target, stmt_index)
            self.visit(node.value)
            return

        for target in node.targets:
            if self._is_simple_name_target(target):
                assert isinstance(target, ast.Name)
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
                    in_try=self.try_depth > 0,
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
                self._record_compound_target_rebindings(target, stmt_index)

        self.visit(node.value)
        self.currently_assigning.clear()

    def _record_compound_target_rebindings(self, target: ast.expr, stmt_index: int) -> None:
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
        else:
            assert isinstance(target, ast.Attribute | ast.Subscript)
            self._track_attribute_or_subscript_base_usage(target, stmt_index)

    def _track_attribute_or_subscript_base_usage(self, node: ast.Attribute | ast.Subscript, stmt_index: int) -> None:
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

        if self._is_simple_name_target(node.target) and node.value is None:
            assert isinstance(node.target, ast.Name)
            self._register_local_binding(scope_id, node.target.id)
            return

        if self._is_simple_name_target(node.target) and node.value is not None:
            assert isinstance(node.target, ast.Name)
            var_name = node.target.id

            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                return

            self._register_local_binding(scope_id, var_name)
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
                in_try=self.try_depth > 0,
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
        scope_id = self._get_current_scope_id()
        stmt_index = self._get_current_stmt_index()

        if self._is_simple_name_target(node.target):
            assert isinstance(node.target, ast.Name)
            var_name = node.target.id

            if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
                self.generic_visit(node)
                return

            self._track_rebinding_use(var_name, node.lineno, node.col_offset, scope_id, stmt_index)
        else:
            assert isinstance(node.target, ast.Attribute | ast.Subscript)
            self._track_attribute_or_subscript_base_usage(node.target, stmt_index)

        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        stmt_index = self._get_current_stmt_index()
        for target in node.targets:
            self._record_deletion_targets(target, stmt_index)
        self.generic_visit(node)

    def _record_deletion_targets(self, target: ast.expr, stmt_index: int) -> None:
        if isinstance(target, ast.Name):
            self._track_deletion_use(target, stmt_index)
        elif isinstance(target, ast.Tuple | ast.List):
            for elt in target.elts:
                self._record_deletion_targets(elt, stmt_index)

    def _track_deletion_use(self, target: ast.Name, stmt_index: int) -> None:
        scope_id = self._get_current_scope_id()
        var_name = target.id

        if (scope_id, var_name) in self.global_vars | self.nonlocal_vars:
            return

        self._register_local_binding(scope_id, var_name)

        usage = UsageInfo(
            var_name=var_name,
            line=target.lineno,
            col=target.col_offset,
            stmt_index=stmt_index,
            context="deletion",
            scope_id=scope_id,
            in_control_flow=self.control_flow_depth > 0,
        )
        key = (scope_id, var_name)
        if key not in self.uses:
            self.uses[key] = []
        self.uses[key].append(usage)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
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
        if self.lambda_depth == 0:
            # A walrus target inside a lambda binds to the lambda's own scope (PEP 572), which this
            # tracker doesn't model separately -- registering it here would wrongly shadow the name
            # for the enclosing function's other, unrelated uses.
            self._register_local_binding(scope_id, var_name)
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
        if isinstance(node, ast.stmt):
            self.current_stmt = node
        super().visit(node)

    def generic_visit(self, node: ast.AST) -> None:
        self.parent_stack.append(node)
        super().generic_visit(node)
        self.parent_stack.pop()

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return

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

        is_keyword_argument_echo = isinstance(immediate_parent, ast.keyword) and immediate_parent.arg == node.id
        is_positional_argument_echo = self._is_positional_argument_echo(node, immediate_parent)
        enclosing_call = self._enclosing_call_for_argument(node, immediate_parent)
        is_call_argument_with_rebindable_callee = enclosing_call is not None and self._callee_is_rebindable(
            enclosing_call
        )

        usage = UsageInfo(
            var_name=node.id,
            line=node.lineno,
            col=node.col_offset,
            stmt_index=stmt_index,
            context="unknown",
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
            is_keyword_argument_echo=is_keyword_argument_echo,
            is_positional_argument_echo=is_positional_argument_echo,
            is_call_argument_with_rebindable_callee=is_call_argument_with_rebindable_callee,
        )

        key = (scope_id, node.id)
        if key not in self.uses:
            self.uses[key] = []
        self.uses[key].append(usage)

    def _enclosing_call_for_argument(self, node: ast.Name, immediate_parent: ast.AST | None) -> ast.Call | None:
        if isinstance(immediate_parent, ast.Call) and node is not immediate_parent.func:
            return immediate_parent
        if isinstance(immediate_parent, ast.keyword | ast.Starred) and immediate_parent.value is node:
            grandparent = self.parent_stack[-2] if len(self.parent_stack) >= 2 else None
            if isinstance(grandparent, ast.Call):
                return grandparent
        return None

    def _callee_is_rebindable(self, call: ast.Call) -> bool:
        if self.has_wildcard_import:
            return True
        base = _unwind_attribute_chain_to_base_name(call.func)
        if base is None:
            return True
        if base.id in self.shadowed_names:
            return True
        if self.function_name_counts.get(base.id, 0) == 0:
            return False
        definition = self.functions.get(base.id)
        return definition is None or definition.lineno >= call.lineno

    def _positional_info_for(self, call: ast.Call) -> tuple[bool, dict[int, int]]:
        cached = self._call_positional_info.get(id(call))
        if cached is None:
            has_starred = any(isinstance(arg, ast.Starred) for arg in call.args)
            index_by_id = {id(arg): index for index, arg in enumerate(call.args)}
            cached = (has_starred, index_by_id)
            self._call_positional_info[id(call)] = cached
        return cached

    def _is_positional_argument_echo(self, node: ast.Name, immediate_parent: ast.AST | None) -> bool:
        if not isinstance(immediate_parent, ast.Call):
            return False

        call = immediate_parent
        has_starred, index_by_id = self._positional_info_for(call)
        if has_starred:
            return False

        if not isinstance(call.func, ast.Name):
            return False

        definition = self.functions.get(call.func.id)
        if definition is None:
            return False

        index = index_by_id.get(id(node))
        if index is None:
            return False

        positional_params = definition.args.posonlyargs + definition.args.args
        if index >= len(positional_params):
            return False

        return positional_params[index].arg == node.id

    def build_lifecycles(self) -> list[VariableLifecycle]:
        lifecycles: list[VariableLifecycle] = []

        for (scope_id, var_name), assignment_list in self.assignments.items():
            for assignment in assignment_list:
                key = (scope_id, var_name)
                all_uses = self.uses.get(key, [])
                relevant_uses = [use for use in all_uses if use.stmt_index >= assignment.stmt_index]

                child_scopes = self._get_closure_reachable_scopes(scope_id, var_name)

                is_captured_by_nonlocal = any(
                    (child_scope_id, var_name) in self.nonlocal_vars for child_scope_id in child_scopes
                )
                if is_captured_by_nonlocal:
                    continue

                for child_scope_id in child_scopes:
                    child_key = (child_scope_id, var_name)
                    child_uses = self.uses.get(child_key, [])
                    relevant_uses.extend(child_uses)

                next_assignment = None
                for other_assignment in assignment_list:
                    if other_assignment.stmt_index > assignment.stmt_index and (
                        next_assignment is None or other_assignment.stmt_index < next_assignment.stmt_index
                    ):
                        next_assignment = other_assignment

                if next_assignment:
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

    def _reference_reassigned_in_range(  # noqa: PLR0917
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
        key = (scope_id, name)
        start_key = (assign_line, assign_stmt_index)

        assignments = self.assignments.get(key)
        if assignments:
            start = bisect.bisect_right(assignments, start_key, key=lambda a: (a.line, a.stmt_index))
            for i in range(start, len(assignments)):
                other_assignment = assignments[i]
                if other_assignment.stmt_index > end_stmt_index or other_assignment.line > use_line:
                    break
                return True

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

    for use in lifecycle.uses:
        if use.scope_id != lifecycle.assignment.scope_id or use.in_lambda:
            return None

    if (
        isinstance(lifecycle.assignment.rhs_node, ast.Attribute | ast.Call)
        and lifecycle.uses[0].in_loop
        and not lifecycle.assignment.in_loop
    ):
        return None

    for use in lifecycle.uses:
        if use.context == "augmented_assignment":
            return None

    for use in lifecycle.uses:
        if use.context == "attribute_or_subscript_assignment":
            return None

    for use in lifecycle.uses:
        if use.context == "deletion":
            return None

    if lifecycle.rhs_reference_reassigned_before_use:
        return None

    if _is_literal_identity(lifecycle):
        return PatternType.LITERAL_IDENTITY

    if lifecycle.is_immediate_use:
        return PatternType.IMMEDIATE_SINGLE_USE

    return PatternType.SINGLE_USE


def _is_literal_identity(lifecycle: VariableLifecycle) -> bool:
    assignment = lifecycle.assignment
    rhs_node = assignment.rhs_node

    if isinstance(rhs_node, ast.Constant) and isinstance(rhs_node.value, str):
        var_name = assignment.var_name.lower()
        literal_value = rhs_node.value.lower()

        if var_name == literal_value:
            return True

        if var_name.replace("_", "") == literal_value.replace("_", ""):
            return True

    return False
