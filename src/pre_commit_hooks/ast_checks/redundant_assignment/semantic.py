"""Semantic value analysis for variable names in TRI005."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from .analysis import PatternType, UsageInfo, VariableLifecycle, is_preceded_by_call

if TYPE_CHECKING:
    from pathlib import Path


def _is_test_file(filepath: Path | None) -> bool:
    if filepath is None:
        return False

    parts = filepath.parts
    if "tests" in parts or "test" in parts:
        return True

    filename = filepath.name
    return filename.startswith("test_") or filename.endswith("_test.py")


# Transformative verbs that indicate semantic value
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

# Boolean/descriptive prefixes that indicate semantic value
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

# Descriptive suffixes that indicate semantic value
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
    """Examples:
    foo.bar.baz -> 2 (two attribute accesses)
    obj[x][y][z] -> 3 (three subscripts)
    func()[key].attr -> 2 (subscript + attribute)
    """
    count = 0
    current = node

    while True:
        if isinstance(current, ast.Subscript | ast.Attribute):
            count += 1
            current = current.value
        elif isinstance(current, ast.Call):
            # Only count the call if it's chained with something else.
            if count > 0:
                count += 1
            current = current.func
        else:
            # Reached the base of the chain.
            break

    return count


def _adds_verbosity_or_context(var_name: str, rhs_source: str, rhs_node: ast.expr) -> bool:
    """True when the variable name provides more descriptive or domain-specific
    information than the RHS expression conveys on its own.

    Examples that add verbosity/context:
        raw_headers = kwargs.get("headers")  # "raw_" prefix adds meaning
        translations = orjson.loads(f.read())  # describes what data is
        firestore_client = db.client()  # more specific than "client"
        user_email = data["email"]  # more verbose than just "email"
    """
    var_lower = var_name.lower()
    rhs_lower = rhs_source.lower()

    # Pattern 1: Variable has descriptive prefix not in RHS
    # Examples: raw_headers, parsed_data, validated_input
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

    # Pattern 2: Variable name is more verbose/explicit than dict/kwargs access
    # Examples:
    #   raw_headers = kwargs.get("headers")  # adds "raw_" prefix  # noqa: ERA001
    #   user_email = data["email"]  # adds "user_" prefix  # noqa: ERA001
    #   firestore_client = db.client()  # more specific type name  # noqa: ERA001
    if isinstance(rhs_node, ast.Subscript | ast.Call):
        rhs_key_or_method = None

        if isinstance(rhs_node, ast.Subscript):
            if isinstance(rhs_node.slice, ast.Constant):
                rhs_key_or_method = str(rhs_node.slice.value).lower()
        # Must be ast.Call — the outer guard ensures Subscript | Call.
        elif isinstance(rhs_node.func, ast.Attribute):
            rhs_key_or_method = rhs_node.func.attr.lower()
        elif isinstance(rhs_node.func, ast.Name):
            rhs_key_or_method = rhs_node.func.id.lower()

        # Example: "raw_headers" contains "headers" but adds "raw_"
        # Example: "firestore_client" contains "client" but adds "firestore_"
        if rhs_key_or_method and rhs_key_or_method in var_lower and var_lower != rhs_key_or_method:
            return True

    # Pattern 3: Variable name is a .get() call with more context
    # Example: raw_headers = kwargs.get("headers")  # noqa: ERA001
    if (
        isinstance(rhs_node, ast.Call)
        and isinstance(rhs_node.func, ast.Attribute)
        and rhs_node.func.attr == "get"
        and rhs_node.args
        and isinstance(rhs_node.args[0], ast.Constant)
    ):
        # Likely kwargs.get() or dict.get().
        key_name = str(rhs_node.args[0].value).lower()
        # If var name contains the key but is longer/different, it adds context.
        if key_name in var_lower and len(var_name) > len(key_name):
            return True

    # Pattern 4: Generic parsing/loading functions with descriptive variable names
    # Examples: translations = orjson.loads(...), config = json.load(...)
    # The variable name describes WHAT the data is (domain/semantics)
    # while the RHS just shows HOW it's loaded (generic operation)
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

        # If it's a generic parse function and variable name is multi-part or long,
        # and not a generic placeholder name like "data" or "result"
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
    is_test_context: bool = False,
    filepath: Path | None = None,
) -> int:
    """The score ranges from 0-100:
    - 0-20: No semantic value (redundant assignment, can auto-fix)
    - 21-49: Marginal value (report but don't auto-fix)
    - 50-100: Clear value (skip entirely)
    """
    score = 0

    # Test code benefits more from named intermediate variables for clarity,
    # so apply a higher semantic value to descriptive variables here.
    if is_test_context or (filepath and _is_test_file(filepath)):
        if len(var_name.split("_")) >= 2:
            # e.g. "camel_case_sample", "duffel_route", "mock_image"
            score += 30

        test_semantic_words = {
            "mock",
            "fake",
            "sample",
            "expected",
            "actual",
            "result",
            "fixture",
            "data",
            "template",
            "response",
            "request",
            "some",
            "example",
            "test",
        }
        var_lower = var_name.lower()
        if any(word in var_lower for word in test_semantic_words):
            score += 25

        # A common pattern for making assertions clearer.
        # Example: result = landmark.__eq__(None); assert result is NotImplemented  # noqa: ERA001
        if isinstance(rhs_node, ast.Call) and var_lower in {
            "result",
            "output",
            "value",
            "response",
            "landmark",
        }:
            score += 30

        # Example: some_european_airports = ["AES", "BYJ", "BTS"]  # noqa: ERA001
        if isinstance(rhs_node, ast.List | ast.Dict | ast.Set):
            score += 25

        # Example: days_with_routes_in_a_row = range(70)  # noqa: ERA001
        if isinstance(rhs_node, ast.Call) and isinstance(rhs_node.func, ast.Name) and rhs_node.func.id == "range":
            score += 25

    # e.g. "raw_headers = kwargs.get('headers')".
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
    # RHS length (chars) above which inlining is considered risky regardless
    # of the variable name's length.
    absolute_threshold: int = 25,
) -> bool:
    """A conservative estimate based on RHS length and variable name, used
    only when the actual usage line isn't available to the caller (see
    `exceeds_line_length_when_inlined` for the exact check used when it is).
    Two call sites use different thresholds: reporting a violation is
    lenient (25 chars), while deciding whether to *auto-fix* is stricter (40
    chars) since a wrong autofix is more costly than a missed report.
    """
    assignment = lifecycle.assignment
    var_name = assignment.var_name
    rhs_source = assignment.rhs_source.strip()

    # A long RHS (e.g. a tuple/list literal, a moderately complex
    # expression) risks exceeding the line length even if the variable name
    # is also long.
    if len(rhs_source) >= absolute_threshold:
        return True

    len_diff = len(rhs_source) - len(var_name)
    return len_diff > 20


def exceeds_line_length_when_inlined(
    var_name: str,
    rhs_source: str,
    use_line: str,
    *,
    max_length: int = 79,  # PEP 8 default
) -> bool:
    """True if replacing `var_name` with `rhs_source` on `use_line` would exceed `max_length`.

    This is the exact check (given the real usage line) shared by
    `should_autofix` (deciding whether to report `[FIXABLE]`) and
    `autofix.apply_fixes`'s `_can_safely_inline` (deciding whether to actually
    apply the fix). Both must agree, or a violation can be reported fixable
    and then silently skipped by `--fix`.
    """
    len_diff = len(rhs_source) - len(var_name)
    new_line_len = len(use_line.rstrip("\n\r")) + len_diff
    return new_line_len > max_length


def _would_require_parentheses(rhs_node: ast.expr) -> bool:
    """True when the RHS contains operations that would need parentheses if
    inlined into a subscript, attribute access, or other typical usage
    context.

    Examples that need parentheses:
        len_prefix = len(x) + 1  # Used in subscript: arr[len(x) + 1]
        result = a or b          # Used in subscript: dict[a or b]
        value = x and y          # Used in call: func(x and y)
    """
    # Binary operations (+, -, *, /, //, %, **, <<, >>, |, ^, &, @)
    # Need parentheses when used in subscripts, attribute access, or calls
    if isinstance(rhs_node, ast.BinOp):
        return True

    # Boolean operations (and, or) need parentheses in most contexts
    if isinstance(rhs_node, ast.BoolOp):
        return True

    # Comparison operations (==, !=, <, >, <=, >=, is, is not, in, not in)
    # Need parentheses in most contexts
    return isinstance(rhs_node, ast.Compare)


def _contains_nondeterministic_call(node: ast.expr) -> bool:
    """Non-deterministic functions (time-related, random, UUID, etc.) return
    different values on each call, so inlining them can change program
    semantics.
    """
    # Known non-deterministic function/method names
    nondeterministic_names = {
        # time module functions
        "time",
        "perf_counter",
        "perf_counter_ns",
        "monotonic",
        "monotonic_ns",
        "process_time",
        "process_time_ns",
        "thread_time",
        "thread_time_ns",
        # datetime functions
        "now",
        "today",
        "utcnow",
        # random module functions
        "random",
        "randint",
        "choice",
        "sample",
        "shuffle",
        "randrange",
        "uniform",
        "gauss",
        # uuid functions
        "uuid",
        "uuid1",
        "uuid4",
        # system functions
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
    """A "named constant" pattern gives semantic meaning to an otherwise-magic
    numeric literal, and should not be flagged:
        max_search_depth = 10  # explains what 10 means
        line_spacing = 1.2  # explains what 1.2 represents
        user_id = 101749141  # gives meaning to the ID
    """
    if not isinstance(rhs_node, ast.Constant):
        return False

    if not isinstance(rhs_node.value, int | float):
        return False

    var_parts = var_name.split("_")
    if len(var_parts) >= 2:
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


def should_report_violation(
    lifecycle: VariableLifecycle,
    filepath: Path | None = None,
) -> bool:
    assignment = lifecycle.assignment

    # Don't report assignments inside loops - they often accumulate/track state
    # across iterations even if they appear to have single use per iteration
    if assignment.in_loop:
        return False

    # Skip if there's a comment right above the assignment (any scope)
    # Comments above variables indicate documentation/explanation intent
    if assignment.has_comment_above:
        return False

    # Skip if there's an inline comment on the assignment line
    # Inline comments (e.g., type: ignore) indicate intentional code
    if assignment.has_inline_comment:
        return False

    # Rule 1: Don't report global scope variables unless prefixed with `_`
    if assignment.in_global_scope and not assignment.var_name.startswith("_"):
        return False

    # Rule 2: Don't report if RHS has await expression
    # Inlining await expressions requires parentheses which is bulky:
    #   json_resp = await resp.json(); return json_resp['key']  # noqa: ERA001
    # Would become: return (await resp.json())['key']  # ugly
    if assignment.rhs_has_await:
        return False

    # Rule 3: Don't report if inlining would likely exceed the line length
    if _would_exceed_line_length(lifecycle):
        return False

    # Rule 4: Don't report if-else ternary operators
    if isinstance(assignment.rhs_node, ast.IfExp):
        return False

    # Rule 5: Don't report if inlining would require parentheses
    # Example: len_prefix = len(x) + 1; arr[len_prefix:] would need arr[(len(x) + 1):]
    if _would_require_parentheses(assignment.rhs_node):
        return False

    # Rule 6: Don't report if RHS contains non-deterministic function calls
    # Functions like time.time(), random.random(), etc. return different values
    # on each call, so inlining them can change program semantics
    if _contains_nondeterministic_call(assignment.rhs_node):
        return False

    # Rule 7: Don't report "magic number" patterns - numeric/simple literals
    # where the variable name provides semantic meaning
    # Examples: max_search_depth = 10, line_spacing = 1.2, user_id = 101749141
    if _is_named_constant_pattern(assignment.var_name, assignment.rhs_node):
        return False

    # Rule 8: Don't report when assignment is outside control flow but usage is inside
    # This handles pytest.raises pattern where setup is intentionally separated:
    #   sample_class = SampleClass()  # setup outside  # noqa: ERA001
    #   with pytest.raises(Error):     # usage inside
    #       sample_class.method()  # noqa: ERA001
    if not assignment.in_control_flow and lifecycle.uses and all(use.in_control_flow for use in lifecycle.uses):
        return False

    # Rule 9: Don't report when assignment is inside control flow but usage is outside
    # This handles context manager pattern to reduce nesting:
    #   with file.open() as f:
    #       config = load(f)  # assignment inside  # noqa: ERA001
    #   # Use config outside to avoid deep nesting
    #   data = config.get(...)  # noqa: ERA001
    if assignment.in_control_flow and lifecycle.uses and all(not use.in_control_flow for use in lifecycle.uses):
        return False

    # Rule 10: Don't report when all usages are inside a comprehension
    # Inlining would re-evaluate the RHS expression on every iteration,
    # causing a performance regression. For example:
    #   iso_country = obj.iso_country          # cached once  # noqa: ERA001
    #   result = [x for x in items if x.country == iso_country]  # O(1) lookup  # noqa: ERA001
    # Inlining would become O(n) attribute lookups inside the comprehension.
    if lifecycle.uses and all(use.in_comprehension for use in lifecycle.uses):
        return False

    semantic_score = calculate_semantic_value(
        var_name=assignment.var_name,
        rhs_source=assignment.rhs_source,
        rhs_node=assignment.rhs_node,
        has_type_annotation=assignment.has_type_annotation,
        filepath=filepath,
    )

    return semantic_score < 50


def _call_use_is_safe_to_inline(use: UsageInfo) -> bool:
    """True if inlining a Call RHS at `use` won't change how often, when, or
    relative to what else the call executes.

    Two independent risks, both specific to Call RHS (a Name/Attribute/
    Constant RHS gives the same value no matter when or how many times it's
    evaluated, given the single-assignment invariant already enforced
    elsewhere in this module — a Call doesn't):
    - Repeated/deferred execution: a use inside a loop or lambda body runs
      0, 1, or many times, at a different point than the original
      assignment (once, immediately) — e.g. `x = make(); for _ in r: f(x)`
      would turn one call into N, and `x = make(); return lambda: x` would
      defer the call to whenever (if ever) the lambda is later invoked.
    - Reordering: a sibling expression evaluated before `use` within its
      statement (see `is_preceded_by_call`) could run before the inlined
      call, when it used to run after.
    """
    return not use.in_loop and not use.in_lambda and not is_preceded_by_call(use)


def should_autofix(
    lifecycle: VariableLifecycle,
    pattern: PatternType,
    filepath: Path | None = None,
    # Source lines of the file being analyzed (no line endings), used to
    # check the *actual* usage line's length so the `[FIXABLE]` label
    # matches what `apply_fixes` will really do. When omitted (e.g. direct
    # unit tests), falls back to the conservative RHS-length estimate.
    source_lines: list[str] | None = None,
) -> bool:
    """Auto-fix criteria:
    1. For IMMEDIATE_SINGLE_USE or LITERAL_IDENTITY patterns (conservative):
       - NOT inside loops or control flow
       - Semantic score ≤ 10 (extremely low semantic value)
       - Variable name ≤ 10 chars
       - RHS must be simple: constant, name, simple attribute, or zero-arg
         call with no ast.Call evaluating before its use within the use's
         statement (see analysis.is_preceded_by_call) — otherwise inlining
         could reorder the call's side effects relative to a sibling
         expression
       - Inlining must not exceed line length
       - RHS must not be multiline

    2. For SINGLE_USE patterns (more aggressive for function-scope single use):
       - NOT inside loops or control flow
       - Semantic score ≤ 20 (low semantic value)
       - RHS must be reasonably simple: constant, name, attribute, or simple call
       - Inlining must not exceed line length
       - RHS must not be multiline
    """
    assignment = lifecycle.assignment

    # A loop body can accumulate/track state across iterations even with a
    # single textual use; control flow means it's unclear which branch runs.
    if assignment.in_loop:
        return False
    if assignment.in_control_flow:
        return False

    rhs_source = assignment.rhs_source
    if "\n" in rhs_source or "\r" in rhs_source:
        return False

    # Prefer the exact check against the real usage line; fall back to the
    # conservative RHS-length estimate (stricter threshold than reporting,
    # since a wrong autofix is more costly than a missed report) when the
    # usage line isn't available.
    use_line_idx = lifecycle.uses[0].line - 1
    if source_lines is not None and 0 <= use_line_idx < len(source_lines):
        if exceeds_line_length_when_inlined(assignment.var_name, rhs_source, source_lines[use_line_idx]):
            return False
    elif _would_exceed_line_length(lifecycle, absolute_threshold=40):
        return False

    semantic_score = calculate_semantic_value(
        var_name=assignment.var_name,
        rhs_source=assignment.rhs_source,
        rhs_node=assignment.rhs_node,
        has_type_annotation=assignment.has_type_annotation,
        filepath=filepath,
    )

    rhs_node = assignment.rhs_node

    if pattern in {PatternType.IMMEDIATE_SINGLE_USE, PatternType.LITERAL_IDENTITY}:
        if semantic_score > 10:
            return False

        if len(assignment.var_name) > 10:
            return False

        if isinstance(rhs_node, ast.Constant | ast.Name):
            return True

        # Only single-level attribute access (obj.attr), not a chain (obj.x.y.z).
        if isinstance(rhs_node, ast.Attribute) and isinstance(rhs_node.value, ast.Name):
            return True

        # Zero-arg calls (e.g. `ForbidVarsCheck()`), but only when the use
        # runs exactly once, at the same point the call already runs — not
        # inside a loop/lambda (see _call_use_is_safe_to_inline) or preceded
        # by another call/effect within its statement (see
        # analysis.is_preceded_by_call), either of which could change how
        # often, or in what order, the call's side effects occur.
        if isinstance(rhs_node, ast.Call) and not rhs_node.args and not rhs_node.keywords:
            return _call_use_is_safe_to_inline(lifecycle.uses[0])

        return False

    # All other patterns already returned above, so we're always in
    # SINGLE_USE here — used once in the entire function, not just
    # immediately after assignment, so it gets a higher score ceiling.
    if semantic_score > 20:
        return False

    if isinstance(rhs_node, ast.Constant | ast.Name):
        return True

    # Any attribute chain is allowed here (obj.attr or obj.x.y.z), unlike
    # the single-level-only rule above.
    if isinstance(rhs_node, ast.Attribute):
        return True

    # Only when the use runs exactly once at the same point the call
    # already runs (see _call_use_is_safe_to_inline) — otherwise inlining
    # can change how often the call executes, e.g. moving it from once (at
    # the assignment) into a loop body that runs N times.
    # Example: datetime.now(UTC), str(value), len(items)
    if isinstance(rhs_node, ast.Call) and _call_use_is_safe_to_inline(lifecycle.uses[0]):
        if len(rhs_node.args) <= 2 and not rhs_node.keywords:
            return True
        if len(rhs_node.args) == 0 and len(rhs_node.keywords) <= 2:
            return True

    return False
