from __future__ import annotations

import ast
from enum import Enum, auto

from .analysis import PatternType, UsageInfo, VariableLifecycle, is_preceded_by_call


class AggressivenessLevel(Enum):
    CONSERVATIVE = auto()
    AGGRESSIVE = auto()


TRANSFORMATIVE_VERBS = {
    "formatted",
    "parsed",
    "calculated",
    "validated",
    "sanitized",
    "normalized",
    "converted",
    "transformed",
    "processed",
    "filtered",
    "sorted",
    "grouped",
    "aggregated",
    "extracted",
    "compiled",
    "decoded",
    "encoded",
    "serialized",
    "deserialized",
}

DESCRIPTIVE_PREFIXES = {
    "has_",
    "is_",
    "should_",
    "can_",
    "will_",
    "did_",
    "was_",
    "are_",
    "were_",
    "does_",
}

DESCRIPTIVE_SUFFIXES = {
    "_count",
    "_flag",
    "_exists",
    "_found",
    "_valid",
    "_enabled",
    "_disabled",
    "_available",
    "_ready",
    "_size",
    "_length",
    "_index",
    "_offset",
    "_id",
    "_name",
    "_path",
    "_url",
    "_key",
}


def _count_chained_operations(node: ast.expr) -> int:
    count = 0
    current = node

    while True:
        if isinstance(current, ast.Subscript | ast.Attribute):
            count += 1
            current = current.value
        elif isinstance(current, ast.Call):
            if count > 0:
                count += 1
            current = current.func
        else:
            break

    return count


def _adds_verbosity_or_context(var_name: str, rhs_source: str, rhs_node: ast.expr) -> bool:
    var_lower = var_name.lower()
    rhs_lower = rhs_source.lower()

    descriptive_word_prefixes = {
        "raw",
        "parsed",
        "validated",
        "sanitized",
        "normalized",
        "formatted",
        "processed",
        "filtered",
        "sorted",
        "cleaned",
        "decoded",
        "encoded",
        "serialized",
        "deserialized",
        "new",
        "old",
        "current",
        "previous",
        "next",
        "last",
        "first",
        "temp",
        "tmp",
        "original",
        "modified",
        "updated",
    }

    var_parts = var_name.split("_")
    if len(var_parts) >= 2:
        first_part = var_parts[0].lower()
        if first_part in descriptive_word_prefixes and first_part not in rhs_lower:
            return True

    if isinstance(rhs_node, ast.Subscript | ast.Call):
        rhs_key_or_method = None

        if isinstance(rhs_node, ast.Subscript):
            if isinstance(rhs_node.slice, ast.Constant):
                rhs_key_or_method = str(rhs_node.slice.value).lower()
        elif isinstance(rhs_node.func, ast.Attribute):
            rhs_key_or_method = rhs_node.func.attr.lower()
        elif isinstance(rhs_node.func, ast.Name):
            rhs_key_or_method = rhs_node.func.id.lower()

        if rhs_key_or_method and rhs_key_or_method in var_lower and var_lower != rhs_key_or_method:
            return True

    if (
        isinstance(rhs_node, ast.Call)
        and isinstance(rhs_node.func, ast.Attribute)
        and rhs_node.func.attr == "get"
        and rhs_node.args
        and isinstance(rhs_node.args[0], ast.Constant)
    ):
        key_name = str(rhs_node.args[0].value).lower()
        if key_name in var_lower and len(var_name) > len(key_name):
            return True

    if isinstance(rhs_node, ast.Call):
        generic_parse_functions = {
            "loads",
            "load",
            "parse",
            "decode",
            "deserialize",
            "from_json",
            "from_yaml",
            "from_xml",
            "read",
            "read_text",
        }
        func_name = None
        if isinstance(rhs_node.func, ast.Attribute):
            func_name = rhs_node.func.attr.lower()
        elif isinstance(rhs_node.func, ast.Name):
            func_name = rhs_node.func.id.lower()

        generic_names = {"data", "result", "value", "output", "obj", "dict"}
        if (
            func_name in generic_parse_functions
            and (len(var_parts) >= 2 or len(var_name) >= 8)
            and var_lower not in generic_names
        ):
            return True

    return False


def calculate_semantic_value(
    var_name: str,
    rhs_source: str,
    rhs_node: ast.expr,
    *,
    has_type_annotation: bool = False,
) -> int:
    score = 0

    if _adds_verbosity_or_context(var_name, rhs_source, rhs_node):
        score += 50

    var_lower = var_name.lower()
    if any(verb in var_lower for verb in TRANSFORMATIVE_VERBS):
        score += 60

    if any(var_lower.startswith(prefix) for prefix in DESCRIPTIVE_PREFIXES):
        score += 50

    if any(var_lower.endswith(suffix) for suffix in DESCRIPTIVE_SUFFIXES):
        score += 40

    if isinstance(rhs_node, ast.ListComp | ast.DictComp | ast.SetComp | ast.GeneratorExp):
        score += 30
    elif isinstance(rhs_node, ast.BinOp):
        score += 15
    elif isinstance(rhs_node, ast.UnaryOp):
        score += 10
    elif isinstance(rhs_node, ast.IfExp):
        score += 20
    elif isinstance(rhs_node, ast.Lambda):
        score += 25

    chain_count = _count_chained_operations(rhs_node)
    if chain_count >= 3:
        score += 30
    elif chain_count == 2:
        score += 20

    if len(rhs_source) > 80:
        score += 35
    elif len(rhs_source) > 60:
        score += 25
    elif len(rhs_source) > 40:
        score += 10

    name_parts = var_name.split("_")
    if len(name_parts) >= 3:
        score += 20
    elif len(name_parts) == 2:
        score += 10

    if len(var_name) > len(rhs_source) * 1.3:
        score += 15
    elif len(var_name) > len(rhs_source) * 1.1:
        score += 5

    if has_type_annotation:
        score += 15

    return min(score, 100)


def _would_exceed_line_length(
    lifecycle: VariableLifecycle,
    *,
    absolute_threshold: int = 25,
) -> bool:
    assignment = lifecycle.assignment
    var_name = assignment.var_name
    rhs_source = assignment.rhs_source.strip()

    if len(rhs_source) >= absolute_threshold:
        return True

    len_diff = len(rhs_source) - len(var_name)
    return len_diff > 20


def exceeds_line_length_when_inlined(
    var_name: str,
    rhs_source: str,
    use_line: str,
    *,
    max_length: int = 79,
) -> bool:
    len_diff = len(rhs_source) - len(var_name)
    new_line_len = len(use_line.rstrip("\n\r")) + len_diff
    return new_line_len > max_length


def _would_require_parentheses(rhs_node: ast.expr) -> bool:
    if isinstance(rhs_node, ast.BinOp):
        return True

    if isinstance(rhs_node, ast.BoolOp):
        return True

    return isinstance(rhs_node, ast.Compare)


def _contains_nondeterministic_call(node: ast.expr) -> bool:
    nondeterministic_names = {
        "time",
        "perf_counter",
        "perf_counter_ns",
        "monotonic",
        "monotonic_ns",
        "process_time",
        "process_time_ns",
        "thread_time",
        "thread_time_ns",
        "now",
        "today",
        "utcnow",
        "random",
        "randint",
        "choice",
        "sample",
        "shuffle",
        "randrange",
        "uniform",
        "gauss",
        "uuid",
        "uuid1",
        "uuid4",
        "getpid",
        "getppid",
    }

    class NonDeterministicCallDetector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.has_nondeterministic_call = False

        def visit_Call(self, node: ast.Call) -> None:
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name.lower() in nondeterministic_names:
                self.has_nondeterministic_call = True
                return

            self.generic_visit(node)

    detector = NonDeterministicCallDetector()
    detector.visit(node)
    return detector.has_nondeterministic_call


def _is_named_constant_pattern(var_name: str, rhs_node: ast.expr) -> bool:
    if not isinstance(rhs_node, ast.Constant):
        return False

    if not isinstance(rhs_node.value, int | float):
        return False

    if len(var_name.split("_")) >= 2:
        return True

    generic_names = {
        "value",
        "val",
        "num",
        "number",
        "count",
        "total",
        "result",
        "temp",
    }
    return len(var_name) > 6 and var_name.lower() not in generic_names


def _is_named_string_constant_pattern(var_name: str, rhs_node: ast.expr) -> bool:
    if not isinstance(rhs_node, ast.Constant):
        return False

    if not isinstance(rhs_node.value, str):
        return False

    return var_name.lstrip("_").isupper()


def _is_argument_echo(lifecycle: VariableLifecycle) -> bool:
    return lifecycle.is_single_use and (
        lifecycle.uses[0].is_keyword_argument_echo or lifecycle.uses[0].is_positional_argument_echo
    )


def should_report_violation(
    lifecycle: VariableLifecycle,
    pattern: PatternType,
    level: AggressivenessLevel = AggressivenessLevel.CONSERVATIVE,
    *,
    allow_inline_suppression: bool = False,
) -> bool:
    assignment = lifecycle.assignment
    is_argument_echo = _is_argument_echo(lifecycle)

    if assignment.in_loop:
        return False

    if assignment.in_try:
        return False

    if assignment.has_comment_above:
        return False

    if assignment.has_inline_comment and not allow_inline_suppression:
        return False

    if assignment.in_global_scope and not assignment.var_name.startswith("_"):
        return False

    if assignment.var_name.startswith("__") and assignment.var_name.endswith("__"):
        return False

    if assignment.rhs_has_await:
        return False

    if _would_exceed_line_length(lifecycle):
        return False

    if isinstance(assignment.rhs_node, ast.IfExp):
        return False

    if _would_require_parentheses(assignment.rhs_node):
        return False

    if _contains_nondeterministic_call(assignment.rhs_node):
        return False

    if not is_argument_echo and _is_named_constant_pattern(assignment.var_name, assignment.rhs_node):
        return False

    if not assignment.in_control_flow and lifecycle.uses and all(use.in_control_flow for use in lifecycle.uses):
        return False

    if assignment.in_control_flow and lifecycle.uses and all(not use.in_control_flow for use in lifecycle.uses):
        return False

    if lifecycle.uses and all(use.in_comprehension for use in lifecycle.uses):
        return False

    if is_argument_echo:
        return True

    if (
        level is AggressivenessLevel.CONSERVATIVE
        and isinstance(assignment.rhs_node, ast.Call)
        and not _is_generic_call_result_name(assignment.var_name, assignment.rhs_node)
    ):
        return False

    if assignment.in_global_scope and _is_named_string_constant_pattern(assignment.var_name, assignment.rhs_node):
        return False

    semantic_score = calculate_semantic_value(
        var_name=assignment.var_name,
        rhs_source=assignment.rhs_source,
        rhs_node=assignment.rhs_node,
        has_type_annotation=assignment.has_type_annotation,
    )

    return semantic_score <= _report_score_ceiling(level, pattern)


_GENERIC_CALL_RESULT_NAMES = frozenset(
    {
        "data",
        "result",
        "value",
        "val",
        "output",
        "obj",
        "dict",
        "num",
        "number",
        "count",
        "total",
        "temp",
        "tmp",
        "tree",
    }
)


def _is_generic_call_result_name(var_name: str, rhs_node: ast.Call) -> bool:
    if len(var_name) <= 2:
        return True

    var_lower = var_name.lower()
    if var_lower in _GENERIC_CALL_RESULT_NAMES:
        return True

    callee_name = None
    if isinstance(rhs_node.func, ast.Name):
        callee_name = rhs_node.func.id
    elif isinstance(rhs_node.func, ast.Attribute):
        callee_name = rhs_node.func.attr

    return callee_name is not None and var_lower in callee_name.lower()


_CONSERVATIVE_REPORT_CEILING = {
    PatternType.IMMEDIATE_SINGLE_USE: 10,
    PatternType.LITERAL_IDENTITY: 10,
    PatternType.SINGLE_USE: 20,
}
_AGGRESSIVE_REPORT_CEILING = 49


def _report_score_ceiling(level: AggressivenessLevel, pattern: PatternType) -> int:
    if level is AggressivenessLevel.AGGRESSIVE:
        return _AGGRESSIVE_REPORT_CEILING
    return _CONSERVATIVE_REPORT_CEILING[pattern]


def _effectful_rhs_use_is_safe_to_inline(use: UsageInfo) -> bool:
    return not use.in_loop and not use.in_lambda and not is_preceded_by_call(use)


_FSTRING_SPLICE_UNSAFE_CHARS = frozenset({"'", '"', "\\", "{", "}", "\n", "\r", "\x00"})


def is_safe_to_splice_into_fstring(value: str, encoding: str = "utf-8") -> bool:
    if any(char in _FSTRING_SPLICE_UNSAFE_CHARS for char in value):
        return False

    if not value.isprintable():
        return False

    try:
        value.encode(encoding)
    except UnicodeEncodeError:
        return False

    return True


def fstring_splice_is_safe(rhs_node: ast.expr, use: UsageInfo) -> bool | None:
    if not (isinstance(rhs_node, ast.Constant) and isinstance(rhs_node.value, str)):
        return None
    if not use.in_fstring_expression:
        return None
    if use.fstring_field_span is None:
        return False
    return is_safe_to_splice_into_fstring(rhs_node.value)


def should_autofix(
    lifecycle: VariableLifecycle,
    source_lines: list[str] | None = None,
) -> bool:
    assignment = lifecycle.assignment

    if assignment.in_loop:
        return False
    if assignment.in_control_flow:
        return False

    rhs_source = assignment.rhs_source
    if "\n" in rhs_source or "\r" in rhs_source:
        return False

    if fstring_splice_is_safe(assignment.rhs_node, lifecycle.uses[0]) is False:
        return False

    use_line_idx = lifecycle.uses[0].line - 1
    if source_lines is not None and 0 <= use_line_idx < len(source_lines):
        if exceeds_line_length_when_inlined(assignment.var_name, rhs_source, source_lines[use_line_idx]):
            return False
    elif _would_exceed_line_length(lifecycle, absolute_threshold=40):
        return False

    rhs_node = assignment.rhs_node

    if isinstance(rhs_node, ast.Constant | ast.Name):
        return True

    if not lifecycle.is_immediate_use:
        return False

    if isinstance(rhs_node, ast.Attribute):
        return _effectful_rhs_use_is_safe_to_inline(lifecycle.uses[0])

    if isinstance(rhs_node, ast.Call) and _effectful_rhs_use_is_safe_to_inline(lifecycle.uses[0]):
        if len(rhs_node.args) <= 2 and not rhs_node.keywords:
            return True
        if len(rhs_node.args) == 0 and len(rhs_node.keywords) <= 2:
            return True

    return False
