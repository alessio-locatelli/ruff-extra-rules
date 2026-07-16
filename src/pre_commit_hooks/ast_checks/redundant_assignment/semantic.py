"""Semantic value analysis for variable names in TRI005."""

from __future__ import annotations

import ast
from pathlib import Path

from .analysis import PatternType, VariableLifecycle


def _is_test_file(filepath: Path | None) -> bool:
    """Check if a file is a test file.

    Args:
        filepath: Path to the file being analyzed

    Returns:
        True if this is a test file
    """
    if filepath is None:
        return False

    # Check if file is in a test directory
    parts = filepath.parts
    if "tests" in parts or "test" in parts:
        return True

    # Check if filename follows test naming convention
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
    """Count the number of chained operations (subscripts, attributes, calls).

    Examples:
        foo.bar.baz -> 2 (two attribute accesses)
        obj[x][y][z] -> 3 (three subscripts)
        func()[key].attr -> 2 (subscript + attribute)

    Args:
        node: AST expression node

    Returns:
        Number of chained operations
    """
    count = 0
    current = node

    while True:
        if isinstance(current, ast.Subscript | ast.Attribute):
            count += 1
            current = current.value
        elif isinstance(current, ast.Call):
            # Count the call itself if it's part of a chain
            if count > 0:  # Only count if it's chained with something
                count += 1
            current = current.func
        else:
            # Reached the base of the chain
            break

    return count


def _adds_verbosity_or_context(
    var_name: str, rhs_source: str, rhs_node: ast.expr
) -> bool:
    """Check if variable name adds verbosity or context beyond the RHS.

    This detects cases where the variable name provides more descriptive or
    domain-specific information than what the RHS expression conveys.

    Examples that add verbosity/context:
        raw_headers = kwargs.get("headers")  # "raw_" prefix adds meaning
        translations = orjson.loads(f.read())  # describes what data is
        firestore_client = db.client()  # more specific than "client"
        user_email = data["email"]  # more verbose than just "email"

    Args:
        var_name: Variable name
        rhs_source: Right-hand side source code
        rhs_node: Right-hand side AST node

    Returns:
        True if variable name adds verbosity/context
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
        # Check if first part is a descriptive prefix not found in RHS
        if first_part in descriptive_word_prefixes and first_part not in rhs_lower:
            return True

    # Pattern 2: Variable name is more verbose/explicit than dict/kwargs access
    # Examples:
    #   raw_headers = kwargs.get("headers")  # adds "raw_" prefix
    #   user_email = data["email"]  # adds "user_" prefix
    #   firestore_client = db.client()  # more specific type name
    if isinstance(rhs_node, ast.Subscript | ast.Call):
        # Extract key/method name from RHS
        rhs_key_or_method = None

        if isinstance(rhs_node, ast.Subscript):
            # For subscript: obj["key"] or obj[key]
            if isinstance(rhs_node.slice, ast.Constant):
                rhs_key_or_method = str(rhs_node.slice.value).lower()
        else:
            # Must be ast.Call — the outer guard ensures Subscript | Call
            # For calls: obj.method() or obj.method(args)
            if isinstance(rhs_node.func, ast.Attribute):
                rhs_key_or_method = rhs_node.func.attr.lower()
            elif isinstance(rhs_node.func, ast.Name):
                rhs_key_or_method = rhs_node.func.id.lower()

        # Check if variable name contains the RHS key/method but with additional context
        # Example: "raw_headers" contains "headers" but adds "raw_"
        # Example: "firestore_client" contains "client" but adds "firestore_"
        if (
            rhs_key_or_method
            and rhs_key_or_method in var_lower
            and var_lower != rhs_key_or_method
        ):
            # Variable contains the RHS key but is more verbose
            return True

    # Pattern 3: Variable name is a .get() call with more context
    # Example: raw_headers = kwargs.get("headers")
    if (
        isinstance(rhs_node, ast.Call)
        and isinstance(rhs_node.func, ast.Attribute)
        and rhs_node.func.attr == "get"
        and rhs_node.args
        and isinstance(rhs_node.args[0], ast.Constant)
    ):
        # This is a .get() call - likely kwargs.get() or dict.get()
        # Check if variable adds context beyond the key name
        key_name = str(rhs_node.args[0].value).lower()
        # If var name contains the key but is longer/different, it adds context
        if key_name in var_lower and len(var_name) > len(key_name):
            return True

    # Pattern 4: Generic parsing/loading functions with descriptive variable names
    # Examples: translations = orjson.loads(...), config = json.load(...)
    # The variable name describes WHAT the data is (domain/semantics)
    # while the RHS just shows HOW it's loaded (generic operation)
    if isinstance(rhs_node, ast.Call):
        # Check if RHS is a generic parsing/loading function
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
    has_type_annotation: bool = False,
    is_test_context: bool = False,
    filepath: Path | None = None,
) -> int:
    """Calculate semantic value score for a variable name.

    The score ranges from 0-100:
    - 0-20: No semantic value (redundant assignment, can auto-fix)
    - 21-49: Marginal value (report but don't auto-fix)
    - 50-100: Clear value (skip entirely)

    Args:
        var_name: Variable name
        rhs_source: Right-hand side source code
        rhs_node: Right-hand side AST node
        has_type_annotation: Whether assignment has type annotation
        is_test_context: Whether this is in a test file/function
        filepath: Path to file being analyzed (for test detection)

    Returns:
        Semantic value score (0-100)
    """
    score = 0

    # In test contexts, apply higher semantic value to descriptive variables
    # Test code benefits more from named intermediate variables for clarity
    if is_test_context or (filepath and _is_test_file(filepath)):
        # Multi-part names in tests are highly valuable for test readability
        var_parts = var_name.split("_")
        if len(var_parts) >= 2:
            # Variables like "camel_case_sample", "duffel_route", "mock_image"
            # provide essential context in test code
            score += 30

        # Variables that clearly describe test data or results
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

        # Variables storing function/method call results before assertions
        # Example: result = landmark.__eq__(None); assert result is NotImplemented
        if isinstance(rhs_node, ast.Call) and var_lower in {
            "result",
            "output",
            "value",
            "response",
            "landmark",
        }:
            # If variable is called "result", "output", "value" in test context,
            # it's a common pattern for making assertions clearer
            score += 30

        # List/dict literals with semantic names in tests
        # Example: some_european_airports = ["AES", "BYJ", "BTS"]
        if isinstance(rhs_node, ast.List | ast.Dict | ast.Set):
            score += 25

        # Range objects with descriptive names
        # Example: days_with_routes_in_a_row = range(70)
        if (
            isinstance(rhs_node, ast.Call)
            and isinstance(rhs_node.func, ast.Name)
            and rhs_node.func.id == "range"
        ):
            score += 25

    # Check if variable adds verbosity or context (+50 points)
    # This catches cases like "raw_headers = kwargs.get('headers')"
    if _adds_verbosity_or_context(var_name, rhs_source, rhs_node):
        score += 50

    # Check for transformative verbs (+60 points - strong signal of semantic value)
    var_lower = var_name.lower()
    if any(verb in var_lower for verb in TRANSFORMATIVE_VERBS):
        score += 60

    # Check for descriptive boolean prefixes (+50 points - strong signal)
    if any(var_lower.startswith(prefix) for prefix in DESCRIPTIVE_PREFIXES):
        score += 50

    # Check for descriptive suffixes (+40 points)
    if any(var_lower.endswith(suffix) for suffix in DESCRIPTIVE_SUFFIXES):
        score += 40

    # Expression complexity scoring
    if isinstance(
        rhs_node, ast.ListComp | ast.DictComp | ast.SetComp | ast.GeneratorExp
    ):
        # Comprehensions benefit from naming (+30)
        score += 30
    elif isinstance(rhs_node, ast.BinOp):
        # Binary operations (+15)
        score += 15
    elif isinstance(rhs_node, ast.UnaryOp):
        # Unary operations (+10)
        score += 10
    elif isinstance(rhs_node, ast.IfExp):
        # Ternary expressions (+20)
        score += 20
    elif isinstance(rhs_node, ast.Lambda):
        # Lambda expressions (+25)
        score += 25

    # Chained operations benefit significantly from naming
    # Examples: obj[x][y], foo.bar.baz, func()[key].attr
    chain_count = _count_chained_operations(rhs_node)
    if chain_count >= 3:
        # 3+ chained operations are hard to read inline (+30)
        score += 30
    elif chain_count == 2:
        # 2 chained operations benefit from naming (+20)
        score += 20

    # Long expressions benefit from naming (progressive scoring)
    if len(rhs_source) > 80:
        # Very long expressions (80+) strongly benefit from naming
        score += 35
    elif len(rhs_source) > 60:
        # Long expressions (60-80) benefit from naming
        score += 25
    elif len(rhs_source) > 40:
        # Medium expressions (40-60) somewhat benefit
        score += 10

    # Multi-part names often represent domain concepts
    name_parts = var_name.split("_")
    if len(name_parts) >= 3:
        # 3+ parts suggests domain-specific naming
        score += 20
    elif len(name_parts) == 2:
        # 2 parts is moderate
        score += 10

    # Variable name significantly longer than expression
    if len(var_name) > len(rhs_source) * 1.3:
        score += 15
    elif len(var_name) > len(rhs_source) * 1.1:
        score += 5

    # Type annotations add clarity
    if has_type_annotation:
        score += 15

    # Cap at 100
    return min(score, 100)


def _would_exceed_line_length(
    lifecycle: VariableLifecycle,
    *,
    absolute_threshold: int = 25,
) -> bool:
    """Check if inlining would likely cause usage lines to exceed line length.

    This is a conservative estimate based on RHS length and variable name,
    not the actual usage line (which we don't have here). Two call sites use
    different thresholds: reporting a violation is lenient (25 chars), while
    deciding whether to *auto-fix* is stricter (40 chars) since a wrong
    autofix is more costly than a missed report.

    Args:
        lifecycle: Variable lifecycle
        absolute_threshold: RHS length (chars) above which inlining is
            considered risky regardless of the variable name's length

    Returns:
        True if inlining would likely exceed the line length
    """
    assignment = lifecycle.assignment
    var_name = assignment.var_name
    rhs_source = assignment.rhs_source.strip()

    # If the RHS itself is long, inlining it is likely to cause line length
    # issues, even if the variable name is also long
    # Example: tuple/list literals, moderately complex expressions
    if len(rhs_source) >= absolute_threshold:
        return True

    # If the RHS is significantly longer than the variable name,
    # it's likely to cause line length issues
    # Use threshold: if len_diff > 20, assume it might exceed
    len_diff = len(rhs_source) - len(var_name)
    return len_diff > 20


def _would_require_parentheses(rhs_node: ast.expr) -> bool:
    """Check if inlining the RHS would require parentheses in typical usage contexts.

    This detects cases where the RHS contains operations that would need
    parentheses when used in subscripts, attribute access, or other contexts.

    Examples that need parentheses:
        len_prefix = len(x) + 1  # Used in subscript: arr[len(x) + 1]
        result = a or b          # Used in subscript: dict[a or b]
        value = x and y          # Used in call: func(x and y)

    Args:
        rhs_node: Right-hand side AST node

    Returns:
        True if inlining would likely require parentheses
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
    """Check if an AST node contains calls to non-deterministic functions.

    Non-deterministic functions include time-related, random, UUID, etc.
    These functions return different values on each call, so inlining them
    can change program semantics.

    Args:
        node: AST expression node

    Returns:
        True if node contains non-deterministic function calls
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
            # Check if the function name suggests non-determinism
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name.lower() in nondeterministic_names:
                self.has_nondeterministic_call = True
                return

            # Continue visiting child nodes
            self.generic_visit(node)

    detector = NonDeterministicCallDetector()
    detector.visit(node)
    return detector.has_nondeterministic_call


def _is_named_constant_pattern(var_name: str, rhs_node: ast.expr) -> bool:
    """Check if this is a "named constant" pattern avoiding magic numbers.

    Magic numbers are raw numeric literals that lack context. Using a descriptive
    variable name gives semantic meaning to the value.

    Examples that should NOT be flagged:
        max_search_depth = 10  # explains what 10 means
        line_spacing = 1.2  # explains what 1.2 represents
        user_id = 101749141  # gives meaning to the ID

    Args:
        var_name: Variable name
        rhs_node: Right-hand side AST node

    Returns:
        True if this is a named constant pattern
    """
    # Only applies to numeric literals (int, float)
    if not isinstance(rhs_node, ast.Constant):
        return False

    if not isinstance(rhs_node.value, int | float):
        return False

    # Variable name must provide semantic meaning
    # Check for multi-part names (with underscore) or sufficiently descriptive names
    var_parts = var_name.split("_")

    # Multi-part names like "max_search_depth", "line_spacing", "user_id"
    if len(var_parts) >= 2:
        return True

    # Single-part names that are descriptive (> 6 chars) and not generic
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
    pattern: PatternType,
    filepath: Path | None = None,
) -> bool:
    """Determine if a violation should be reported based on semantic analysis.

    Args:
        lifecycle: Variable lifecycle
        pattern: Detected pattern type
        filepath: Path to file being analyzed (for test detection)

    Returns:
        True if violation should be reported
    """
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
    #   json_resp = await resp.json(); return json_resp['key']
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
    #   sample_class = SampleClass()  # setup outside
    #   with pytest.raises(Error):     # usage inside
    #       sample_class.method()
    if (
        not assignment.in_control_flow
        and lifecycle.uses
        and all(use.in_control_flow for use in lifecycle.uses)
    ):
        return False

    # Rule 9: Don't report when assignment is inside control flow but usage is outside
    # This handles context manager pattern to reduce nesting:
    #   with file.open() as f:
    #       config = load(f)  # assignment inside
    #   # Use config outside to avoid deep nesting
    #   data = config.get(...)
    if (
        assignment.in_control_flow
        and lifecycle.uses
        and all(not use.in_control_flow for use in lifecycle.uses)
    ):
        return False

    # Rule 10: Don't report when all usages are inside a comprehension
    # Inlining would re-evaluate the RHS expression on every iteration,
    # causing a performance regression. For example:
    #   iso_country = obj.iso_country          # cached once
    #   result = [x for x in items if x.country == iso_country]  # O(1) lookup
    # Inlining would become O(n) attribute lookups inside the comprehension.
    if lifecycle.uses and all(use.in_comprehension for use in lifecycle.uses):
        return False

    # Calculate semantic value
    semantic_score = calculate_semantic_value(
        var_name=assignment.var_name,
        rhs_source=assignment.rhs_source,
        rhs_node=assignment.rhs_node,
        has_type_annotation=assignment.has_type_annotation,
        filepath=filepath,
    )

    # Report violations with low semantic value (< 50)
    # Score 50+ indicates the variable adds meaningful clarity
    return semantic_score < 50


def should_autofix(
    lifecycle: VariableLifecycle,
    pattern: PatternType,
    filepath: Path | None = None,
) -> bool:
    """Determine if a violation should be auto-fixed.

    Auto-fix criteria:
    1. For IMMEDIATE_SINGLE_USE or LITERAL_IDENTITY patterns (conservative):
       - NOT inside loops or control flow
       - Semantic score ≤ 10 (extremely low semantic value)
       - Variable name ≤ 10 chars
       - RHS must be simple: constant, name, or simple attribute
       - Inlining must not exceed line length
       - RHS must not be multiline

    2. For SINGLE_USE patterns (more aggressive for function-scope single use):
       - NOT inside loops or control flow
       - Semantic score ≤ 20 (low semantic value)
       - RHS must be reasonably simple: constant, name, attribute, or simple call
       - Inlining must not exceed line length
       - RHS must not be multiline

    Args:
        lifecycle: Variable lifecycle
        pattern: Detected pattern type
        filepath: Path to file being analyzed (for test detection)

    Returns:
        True if should auto-fix
    """
    assignment = lifecycle.assignment

    # Never auto-fix inside loops (state accumulation pattern)
    if assignment.in_loop:
        return False

    # Never auto-fix inside control flow (conditional logic)
    if assignment.in_control_flow:
        return False

    # Check for multiline RHS (can't auto-fix)
    rhs_source = assignment.rhs_source
    if "\n" in rhs_source or "\r" in rhs_source:
        return False

    # Check if inlining would likely exceed line length (stricter threshold
    # than reporting: a wrong autofix is more costly than a missed report)
    if _would_exceed_line_length(lifecycle, absolute_threshold=40):
        return False

    # Calculate semantic value
    semantic_score = calculate_semantic_value(
        var_name=assignment.var_name,
        rhs_source=assignment.rhs_source,
        rhs_node=assignment.rhs_node,
        has_type_annotation=assignment.has_type_annotation,
        filepath=filepath,
    )

    rhs_node = assignment.rhs_node

    # Conservative auto-fix for immediate use or literal identity
    if pattern in {PatternType.IMMEDIATE_SINGLE_USE, PatternType.LITERAL_IDENTITY}:
        # Only auto-fix if semantic value is EXTREMELY low (≤ 10)
        if semantic_score > 10:
            return False

        # Variable name must be short (≤ 10 chars)
        if len(assignment.var_name) > 10:
            return False

        # Only auto-fix VERY simple RHS expressions
        # Allow: constants, simple names
        if isinstance(rhs_node, ast.Constant | ast.Name):
            return True

        # Allow: simple attribute access (obj.attr, not obj.x.y.z)
        return isinstance(rhs_node, ast.Attribute) and isinstance(
            rhs_node.value, ast.Name
        )

    # More aggressive auto-fix for single-use variables (used once in entire function).
    # All other patterns already returned above, so we are always in SINGLE_USE here.
    # Only auto-fix if semantic value is low (≤ 20)
    # This allows slightly more meaningful names to be inlined
    if semantic_score > 20:
        return False

    # Allow simple expressions
    if isinstance(rhs_node, ast.Constant | ast.Name):
        return True

    # Allow: attribute access (obj.attr or obj.x.y.z)
    if isinstance(rhs_node, ast.Attribute):
        return True

    # Allow: simple calls with no complex arguments
    # Example: datetime.now(UTC), str(value), len(items)
    if isinstance(rhs_node, ast.Call):
        # Only allow calls with simple arguments (no keyword unpacking, etc.)
        # and no more than 2 positional args
        if len(rhs_node.args) <= 2 and not rhs_node.keywords:
            return True
        # Also allow calls with only simple keyword arguments
        if len(rhs_node.args) == 0 and len(rhs_node.keywords) <= 2:
            return True

    return False
