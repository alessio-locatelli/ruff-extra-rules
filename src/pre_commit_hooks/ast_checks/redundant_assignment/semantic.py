"""Semantic value analysis for variable names in TRI005."""

from __future__ import annotations

import ast
from enum import Enum, auto
from typing import TYPE_CHECKING

from .analysis import PatternType, UsageInfo, VariableLifecycle, is_preceded_by_call

if TYPE_CHECKING:
    from pathlib import Path


class AggressivenessLevel(Enum):
    """See `should_report_violation`."""

    CONSERVATIVE = auto()
    PERMISSIVE = auto()


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
    """A module-level SCREAMING_SNAKE_CASE name (PEP 8's constant
    convention, leading underscores stripped first) reads as a deliberate,
    reusable declaration even for a string RHS. Unlike
    `_is_named_constant_pattern` above, this can't reuse its
    "underscore-separated part count" check: every candidate reaching here
    already has a leading underscore (Rule 1 in `should_report_violation`),
    which alone satisfies that test regardless of casing. Requiring the
    stripped name to be all-uppercase is what actually distinguishes a
    constant from an ordinary private variable.
    """
    if not isinstance(rhs_node, ast.Constant):
        return False

    if not isinstance(rhs_node.value, str):
        return False

    return var_name.lstrip("_").isupper()


def should_report_violation(
    lifecycle: VariableLifecycle,
    pattern: PatternType,
    filepath: Path | None = None,
    level: AggressivenessLevel = AggressivenessLevel.CONSERVATIVE,
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

    # Rule 11 (conservative level only): a Call RHS returns some domain
    # object the tracker can't inspect, so a name that isn't a generic
    # placeholder (`result`, `value`, ...) or a restatement of the callee
    # itself (`check = ForbidVarsCheck()`) is presumed to be the author
    # deliberately documenting what that call returns — e.g. `warning =
    # conn.recv()` or `ci_headers = CIMultiDict(headers)` — rather than a
    # redundant restatement of it. Doesn't apply to the permissive level,
    # which reports the same broader set TRI005 always used to.
    if (
        level is AggressivenessLevel.CONSERVATIVE
        and isinstance(assignment.rhs_node, ast.Call)
        and not _is_generic_call_result_name(assignment.var_name, assignment.rhs_node)
    ):
        return False

    # Rule 12 (conservative level only): a module-level string constant
    # named with the same convention Rule 7 already exempts for numeric
    # values (`_GREY = "rgb(201, 203, 207)"`) reads as a deliberate,
    # reusable declaration even when this file happens to use it only
    # once — unlike a local `x = "foo"` used once, which is exactly the
    # redundant pattern this check targets regardless of name shape.
    # Doesn't apply to the permissive level, which still flags these like
    # any other single-use string assignment.
    if (
        level is AggressivenessLevel.CONSERVATIVE
        and assignment.in_global_scope
        and _is_named_string_constant_pattern(assignment.var_name, assignment.rhs_node)
    ):
        return False

    semantic_score = calculate_semantic_value(
        var_name=assignment.var_name,
        rhs_source=assignment.rhs_source,
        rhs_node=assignment.rhs_node,
        has_type_annotation=assignment.has_type_annotation,
        filepath=filepath,
    )

    return semantic_score <= _report_score_ceiling(level, pattern)


# Placeholder names that add no descriptive value of their own — matching
# the generic-name sets already used elsewhere in this module (Pattern 4 of
# `_adds_verbosity_or_context`, `_is_named_constant_pattern`) plus a handful
# of common synonyms, including `tree` (an `ast.parse()`-style result has no
# more specific conventional name). A name this short (<=2 characters, e.g.
# `x`) is treated as equally generic — too short to carry any domain
# meaning either way.
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
    """True when `var_name` adds no descriptive value beyond what
    `rhs_node`'s call already conveys — either a placeholder generic enough
    to carry no domain meaning on its own, or a name that just restates the
    callee it's assigned from (`check = ForbidVarsCheck()`, `state =
    me.state(...)`) rather than describing the domain value the call
    returns (`warning = conn.recv()`).
    """
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


# Score ceiling a violation's `calculate_semantic_value` must be at or under
# to be reported. The conservative-level ceilings reuse the exact numbers
# `should_autofix` used to gate autofix eligibility on before issue #76 — so
# the conservative level now reports only what the old default used to
# auto-fix, and reporting alone (no separate, softer autofix-specific
# ceiling) decides what's eligible for --fix. The permissive ceiling (49,
# i.e. score < 50) reproduces TRI005's old, single default reporting bar.
_CONSERVATIVE_REPORT_CEILING = {
    PatternType.IMMEDIATE_SINGLE_USE: 10,
    PatternType.LITERAL_IDENTITY: 10,
    PatternType.SINGLE_USE: 20,
}
_PERMISSIVE_REPORT_CEILING = 49


def _report_score_ceiling(level: AggressivenessLevel, pattern: PatternType) -> int:
    if level is AggressivenessLevel.PERMISSIVE:
        return _PERMISSIVE_REPORT_CEILING
    return _CONSERVATIVE_REPORT_CEILING[pattern]


def _effectful_rhs_use_is_safe_to_inline(use: UsageInfo) -> bool:
    """True if inlining a Call or Attribute RHS at `use` won't change how
    often, when, or relative to what else it executes.

    Applies to both — not just Call — because attribute access can run
    arbitrary code too (a `@property` getter or `__getattr__`/descriptor),
    exactly like `_POTENTIALLY_EFFECTFUL_NODE_TYPES` already treats it for
    evaluation-order purposes elsewhere in this package. A Name/Constant
    RHS needs none of this: it gives the same value no matter when or how
    many times it's evaluated, given the single-assignment invariant (and,
    for Name, the "reference reassigned before use" exclusion) already
    enforced elsewhere in this module.

    Two independent risks:
    - Repeated/deferred execution: a use inside a loop or lambda body runs
      0, 1, or many times, at a different point than the original
      assignment (once, immediately) — e.g. `x = make(); for _ in r: f(x)`
      would turn one call into N, and `x = make(); return lambda: x` would
      defer the call to whenever (if ever) the lambda is later invoked.
    - Reordering: a sibling expression evaluated before `use` within its
      statement (see `is_preceded_by_call`) could run before the inlined
      expression, when it used to run after.
    """
    return not use.in_loop and not use.in_lambda and not is_preceded_by_call(use)


# Characters that make it unsafe to splice a string literal's raw value
# directly into an f-string's surrounding text: the two quote characters
# (would prematurely terminate — or ambiguously extend — the string,
# without knowing the specific quote style the target f-string uses), a
# backslash (would be reinterpreted as the start of an escape sequence
# instead of a literal backslash), brace characters (would open/close a new
# replacement field instead of appearing as literal text), newlines (would
# break a single-line f-string), and a NUL byte (CPython's tokenizer
# rejects any source file containing one, even though it's a perfectly
# valid character *inside* a string literal like `"\x00"` — splicing it as
# raw text would turn a fixable file into an unparsable one).
_FSTRING_SPLICE_UNSAFE_CHARS = frozenset({"'", '"', "\\", "{", "}", "\n", "\r", "\x00"})


def is_safe_to_splice_into_fstring(value: str, encoding: str = "utf-8") -> bool:
    """`encoding` defaults to "utf-8" — this codebase's overwhelmingly
    common case, and the only option available at check() time, which
    (unlike fix()) never learns a file's real PEP 263 declared encoding.
    `autofix.apply_fixes` calls this again at fix time with the real
    encoding, so a file declaring something narrower (e.g. `# -*- coding:
    ascii -*-`) still gets this validated correctly before anything is
    written — see exceeds_line_length_when_inlined's docstring for why
    this module's checks are routinely re-validated a second time, against
    real values, right before a fix is actually applied.
    """
    if any(char in _FSTRING_SPLICE_UNSAFE_CHARS for char in value):
        return False

    # A control character (e.g. "\x1b", the ANSI escape used in terminal
    # color codes) is syntactically fine to splice as raw text — none of
    # them are in the unsafe set above — but writing it as a literal,
    # unescaped byte turns a readable `\x1b` into an invisible one: the
    # resulting source line looks like the value vanished (a diff shows
    # nothing where the field used to be) even though the byte is really
    # there. str.isprintable() rejects every control character (plus
    # unassigned/surrogate/other non-printable categories), matching the
    # newline/NUL exclusions above but generalized instead of enumerated.
    if not value.isprintable():
        return False

    # A str object can legally hold an unpaired surrogate (e.g. from a
    # "\ud800" escape) even though no real text encoding can represent one
    # — splicing it as raw source text would make atomic_write_text's
    # compile()/write() crash with an uncaught UnicodeEncodeError instead
    # of writing a fixed file, exactly like a NUL byte above but for a
    # different underlying reason (unencodable rather than unparsable).
    try:
        value.encode(encoding)
    except UnicodeEncodeError:
        return False

    return True


def fstring_splice_is_safe(rhs_node: ast.expr, use: UsageInfo) -> bool | None:
    """None when this isn't the f-string-splice scenario at all — RHS isn't
    a string literal, or the use isn't inside an f-string replacement field
    — so callers should fall through to the ordinary autofix rules
    unchanged. True/False when it is: whether the literal's raw value can
    be spliced directly into the surrounding f-string text instead of
    being naively re-quoted inside `{}` (issue #72).
    """
    if not (isinstance(rhs_node, ast.Constant) and isinstance(rhs_node.value, str)):
        return None
    if not use.in_fstring_expression:
        return None
    if use.fstring_field_span is None:
        # Inside an f-string field, but not as its whole expression (e.g.
        # `{x.attr}` or `{x!r}`) — no safe way to remove the braces and
        # splice raw text without changing what the field expression does.
        return False
    return is_safe_to_splice_into_fstring(rhs_node.value)


def should_autofix(
    lifecycle: VariableLifecycle,
    # Source lines of the file being analyzed (no line endings), used to
    # check the *actual* usage line's length so the `[FIXABLE]` label
    # matches what `apply_fixes` will really do. When omitted (e.g. direct
    # unit tests), falls back to the conservative RHS-length estimate.
    source_lines: list[str] | None = None,
) -> bool:
    """Whether a *reported* violation (see `should_report_violation`) is
    also mechanically safe to inline. This is the only gate on autofix
    eligibility (issue #76) — pattern-independent, and with no separate,
    softer semantic-score ceiling narrowing it further below whatever was
    already reported. "Mechanically safe" means:
    - NOT inside a loop or other control flow
    - RHS is a single line, and inlining it doesn't exceed the line length
      on the actual usage line
    - RHS is a constant or name (always safe — see
      `_effectful_rhs_use_is_safe_to_inline`), or an attribute (any chain
      depth) or call whose use is the very next statement after the
      assignment, runs exactly once at the point the assignment already
      runs — never inside a loop/lambda, and with nothing effectful
      evaluating before it within its statement (see
      `_effectful_rhs_use_is_safe_to_inline`) — with a call additionally
      capped at 2 positional args and no keywords, or vice versa, so
      inlining doesn't turn the use site into a visually complex expression
    - No f-string-splice hazard (issue #72); the RHS-aliasing hazard (issue
      #74) is already ruled out upstream by `detect_redundancy` refusing to
      report that pattern at all, so it never reaches this function
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

    if fstring_splice_is_safe(assignment.rhs_node, lifecycle.uses[0]) is False:
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

    rhs_node = assignment.rhs_node

    if isinstance(rhs_node, ast.Constant | ast.Name):
        return True

    # Attribute and Call RHS can both run arbitrary code when evaluated
    # (see _effectful_rhs_use_is_safe_to_inline), so inlining either one is
    # only safe when the use is the very next statement after the
    # assignment (lifecycle.is_immediate_use) — otherwise an intervening
    # statement's own effects could end up running before or after the
    # inlined expression's, when they didn't originally. Example:
    # `result_data = pop(queue); queue.clear(); return result_data` must
    # not become `queue.clear(); return pop(queue)`, which pops after
    # clearing instead of before.
    if not lifecycle.is_immediate_use:
        return False

    if isinstance(rhs_node, ast.Attribute):
        return _effectful_rhs_use_is_safe_to_inline(lifecycle.uses[0])

    # Only when the use runs exactly once at the same point the call
    # already runs (see _effectful_rhs_use_is_safe_to_inline) — otherwise
    # inlining can change how often the call executes, e.g. moving it from
    # once (at the assignment) into a loop body that runs N times.
    # Example: datetime.now(UTC), str(value), len(items)
    if isinstance(rhs_node, ast.Call) and _effectful_rhs_use_is_safe_to_inline(lifecycle.uses[0]):
        if len(rhs_node.args) <= 2 and not rhs_node.keywords:
            return True
        if len(rhs_node.args) == 0 and len(rhs_node.keywords) <= 2:
            return True

    return False
