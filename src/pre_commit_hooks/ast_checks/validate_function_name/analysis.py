"""AST analysis and behavior detection for function naming."""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass

from .._base import find_ignored_lines, ignore_pattern_for, read_source_with_encoding
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("validate_function_name")

# Format: # pytriage: ignore=TRI004
IGNORE_PATTERN = ignore_pattern_for("TRI004")
GET_PREFIX = "get_"


@dataclass(slots=True)
class Suggestion:
    """A naming suggestion for a function."""

    path: Path
    func_name: str
    lineno: int
    suggested_name: str
    reason: str


def read_source(path: Path) -> str:
    """Read source code from a file, honoring a PEP 263 encoding declaration."""
    source, _encoding = read_source_with_encoding(path)
    return source


def _call_name(node: ast.AST) -> str | None:
    """Return a readable dotted name for a call (e.g., 'requests.get' or 'open')."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return None


def decorator_name(d: ast.AST) -> str | None:
    """Extract decorator name from decorator node."""
    if isinstance(d, ast.Name):
        return d.id
    if isinstance(d, ast.Attribute):
        # e.g. abc.abstractmethod
        name = _call_name(d)
        return name
    return None


def is_decorator_override_or_abstract(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Check if function is decorated with @override or @abstractmethod."""
    for d in func_node.decorator_list:
        name = decorator_name(d)
        if not name:
            continue
        lname = name.lower()
        if lname.endswith("override") or lname.endswith("abstractmethod"):
            return True
    return False


def analyze_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, bool]:
    """Analyze function behavior and return detected patterns.

    Returns:
        Dictionary with boolean flags for detected behaviors:
        - is_property: Function is decorated with @property
        - disk_read: Reads from disk (open, read_text, etc.)
        - disk_write: Writes to disk (write, save, dump, etc.)
        - network_read: Reads from network (requests.get, etc.)
        - network_write: Writes to network (requests.post, etc.)
        - outputs: Prints or logs output
        - returns_bool: Returns boolean (based on annotation)
        - aggregates: Performs aggregation (sum, min, max, mean, etc.)
        - creates_object: Creates new objects (calls constructors)
        - mutates_args: Mutates arguments or state
        - yields: Generator function
        - parses: Parses structured data (JSON, YAML, etc.)
        - renders: Renders/serializes data to string
        - searches: Searches or finds items
        - validates: Validates input and returns errors
        - transforms: Transforms data
        - delegates_get: Delegates to another get_* function
        - collects: Collects data into a list/dict
        - returns_class: Returns a class object (not instance)
    """
    flags: dict[str, bool] = {
        "is_property": False,
        "disk_read": False,
        "disk_write": False,
        "network_read": False,
        "network_write": False,
        "outputs": False,
        "returns_bool": False,
        "aggregates": False,
        "creates_object": False,
        "mutates_args": False,
        "yields": False,
        "parses": False,
        "renders": False,
        "searches": False,
        "validates": False,
        "transforms": False,
        "delegates_get": False,
        "collects": False,
        "returns_class": False,
    }

    # Collect parameter names to distinguish argument mutation from cache updates
    param_names: set[str] = set()
    for arg in func_node.args.args:
        param_names.add(arg.arg)
    for arg in func_node.args.posonlyargs:
        param_names.add(arg.arg)
    for arg in func_node.args.kwonlyargs:
        param_names.add(arg.arg)
    if func_node.args.vararg:
        param_names.add(func_node.args.vararg.arg)
    if func_node.args.kwarg:
        param_names.add(func_node.args.kwarg.arg)

    # property
    for deco in func_node.decorator_list:
        if (isinstance(deco, ast.Name) and deco.id == "property") or (
            isinstance(deco, ast.Attribute) and deco.attr.endswith("property")
        ):
            flags["is_property"] = True

    # returns_bool: rely only on annotation (do not inspect return values)
    if func_node.returns is not None:
        ann = func_node.returns
        if isinstance(ann, ast.Name) and ann.id == "bool":
            flags["returns_bool"] = True

    # track variables assigned from get_* calls (delegation detection)
    get_assigned_vars: set[str] = set()

    # track local containers that are created and appended to
    created_containers: set[str] = set()
    appended_to: set[str] = set()

    # Quick scan for 'while' loops checking .exists() or using .parent
    # (heuristic for find_root)
    has_loop_checking_exists_or_parent = False

    # Track classes defined inside the function (for returns_class detection)
    defined_classes: set[str] = set()
    for stmt in func_node.body:
        if isinstance(stmt, ast.ClassDef):
            defined_classes.add(stmt.name)

    # walk nodes
    for node in ast.walk(func_node):
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            flags["yields"] = True
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if not name:
                continue
            lname = name.lower()
            # disk read/write
            if (
                lname in ("open",)
                or lname.endswith(".read")
                or lname.endswith(".read_text")
                or lname.endswith(".read_bytes")
                or lname.endswith(".load")
            ):
                flags["disk_read"] = True
            if (
                lname.endswith(".write")
                or lname.endswith(".save")
                or lname.endswith(".dump")
            ):
                flags["disk_write"] = True
            # json/yaml loads/dumps
            if (
                lname.endswith("json.loads")
                or lname.endswith("yaml.safe_load")
                or lname.endswith("yaml.load")
            ):
                flags["parses"] = True
            if lname.endswith("json.dumps") or lname.endswith("yaml.dump"):
                flags["renders"] = True
            # network
            if any(
                lib in lname
                for lib in ("requests", "httpx", "urllib", "aiohttp", "socket", "grpc")
            ):
                if any(
                    verb in lname
                    for verb in ("get", "fetch", "download", "read", "recv", "request")
                ):
                    flags["network_read"] = True
                if any(
                    verb in lname for verb in ("post", "put", "send", "upload", "patch")
                ):
                    flags["network_write"] = True
            # logger / print
            if lname in ("print",) or (
                lname.endswith(".write") and ("stdout" in lname or "stderr" in lname)
            ):
                flags["outputs"] = True
            if (
                lname.endswith(".info")
                or lname.endswith(".debug")
                or lname.endswith(".warning")
                or lname.endswith(".error")
            ):
                flags["outputs"] = True
            # aggregate helpers
            if lname in (
                "sum",
                "min",
                "max",
                "statistics.mean",
                "statistics.median",
            ) or lname.endswith(".aggregate"):
                flags["aggregates"] = True
            # search/match
            if (
                lname.endswith(".find")
                or lname.endswith(".search")
                or lname.endswith(".index")
            ):
                flags["searches"] = True
            # validation
            if lname.endswith(".is_valid") or lname.endswith(".validate"):
                flags["validates"] = True
            # transform detection
            if lname.endswith(".transform") or lname.endswith(".map"):
                flags["transforms"] = True
            # object creation: constructor calls heuristic
            if (
                isinstance(node.func, ast.Name)
                and node.func.id
                and node.func.id[0].isupper()
            ):
                flags["creates_object"] = True

            # detect calls to other get_ functions and assignments (delegation)
            if isinstance(node.func, ast.Name) and node.func.id.startswith(GET_PREFIX):
                parent = getattr(node, "parent", None)
                if parent and isinstance(parent, ast.Assign):
                    for t in parent.targets:
                        if isinstance(t, ast.Name):
                            get_assigned_vars.add(t.id)
                # direct return of call handled later

        # mutation: attribute assignments or calls to .append/.extend/.add
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    # detect list/dict creation: x = [] or x = {} or x = list() / dict()
                    if isinstance(node.value, (ast.List, ast.Dict)):
                        created_containers.add(target.id)
                    if (
                        isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id in ("list", "dict", "set")
                    ):
                        created_containers.add(target.id)

                # Only flag mutation if we're modifying a parameter or its attributes
                # (not module-level caches/globals like _cache[key] = value)
                if isinstance(target, ast.Attribute):
                    # Check if we're mutating an argument (handles nested: arg.x.y.z)
                    base_name = _get_base_name(target)
                    if base_name and (base_name in param_names or base_name == "self"):
                        flags["mutates_args"] = True
                if isinstance(target, ast.Subscript):
                    # Check if we're mutating an argument (handles nested: arg.x[y])
                    base_name = _get_base_name(target)
                    if base_name and (base_name in param_names or base_name == "self"):
                        flags["mutates_args"] = True
        if isinstance(node, ast.AugAssign):
            # Similar logic for augmented assignments: x += 1
            target = node.target
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                # Handle nested attributes/subscripts
                base_name = _get_base_name(target)
                if base_name and (base_name in param_names or base_name == "self"):
                    flags["mutates_args"] = True
            else:
                # An augmented assignment target is always a Name,
                # Attribute, or Subscript.
                assert isinstance(target, ast.Name)
                # x += 1 where x is a parameter (modifying mutable default argument)
                if target.id in param_names:
                    flags["mutates_args"] = True
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name and any(
                s in name
                for s in (
                    "append",
                    "extend",
                    "add",
                    "insert",
                    "remove",
                    "pop",
                    "clear",
                    "update",
                    "setdefault",
                )
            ):
                # Try to find which variable is being appended to
                parent = getattr(node, "parent", None)
                # 'x.append(...)' -> node.func is Attribute with value Name('x')
                if isinstance(node.func, ast.Attribute) and isinstance(
                    node.func.value, ast.Name
                ):
                    var_name = node.func.value.id
                    appended_to.add(var_name)
                    # Only flag mutation if appending to a parameter
                    if var_name in param_names or var_name == "self":
                        flags["mutates_args"] = True

        # detect .exists() / .parent usage inside loops (heuristic for find_root)
        if isinstance(node, ast.While):
            # look for Call nodes inside the while that call .exists()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    n = _call_name(sub.func)
                    if n and n.endswith(".exists"):
                        has_loop_checking_exists_or_parent = True
                if isinstance(sub, ast.Attribute) and sub.attr == "parent":
                    has_loop_checking_exists_or_parent = True

    # Delegation: if return returns a variable assigned by get_*
    # or returns a call to get_*
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return):
            if isinstance(node.value, ast.Call):
                call_name = _call_name(node.value.func)
                if call_name and call_name.startswith(GET_PREFIX):
                    flags["delegates_get"] = True
            if isinstance(node.value, ast.Name) and node.value.id in get_assigned_vars:
                flags["delegates_get"] = True
            # returning a collected container variable
            if isinstance(node.value, ast.Name) and node.value.id in created_containers:
                flags["collects"] = True
            # Check if returning a class defined in the function
            if isinstance(node.value, ast.Name) and node.value.id in defined_classes:
                flags["returns_class"] = True
            # Also check for type() or type[...] calls (metaclass operations)
            if isinstance(node.value, ast.Call):
                call_name = _call_name(node.value.func)
                if call_name == "type":
                    flags["returns_class"] = True

    # if we saw a variable created as a container and later appended to, mark collects
    if created_containers & appended_to:
        flags["collects"] = True

    # Final heuristic: if we saw a loop checking .exists() or .parent,
    # treat as search/find
    if has_loop_checking_exists_or_parent:
        flags["searches"] = True

    # heuristics for returns list of errors (unchanged)
    assigns = [n for n in ast.walk(func_node) if isinstance(n, ast.Assign)]
    for a in assigns:
        for t in a.targets:
            if isinstance(t, ast.Name) and t.id.lower() in (
                "errors",
                "errs",
                "error_list",
            ):
                flags["validates"] = True

    return flags


def attach_parents(node: ast.AST) -> None:
    """Attach parent references to AST nodes for better analysis."""
    for child in ast.iter_child_nodes(node):
        child.parent = node  # type: ignore[attr-defined]
        attach_parents(child)


def _get_base_name(node: ast.expr) -> str | None:
    """Get the base name from an expression (handles nested attributes).

    Examples:
        x -> "x"
        x.y -> "x"
        x.y.z -> "x"
        x[0] -> "x"
        x[0].y -> "x"
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _get_base_name(node.value)
    if isinstance(node, ast.Subscript):
        return _get_base_name(node.value)
    return None


def is_simple_accessor(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function is a trivial accessor/getter pattern.

    Returns True for:
    - Single 'return self.attr' or 'return obj[attr]'
    - Single 'return obj.get(...)'
    - Simple subscript/attribute/call to .get

    These are idiomatic getters and should not be flagged.
    """
    # A function's body is never empty (Python requires at least one
    # statement), so only the post-docstring-strip check below can be.
    body = func_node.body
    # skip leading docstring expression
    if (
        isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
        if not body:
            return False

    if len(body) != 1:
        return False
    node = body[0]
    # allow simple return statements only
    if not isinstance(node, ast.Return) or node.value is None:
        return False
    value = node.value
    # self.attr or obj.attr  # noqa: ERA001
    if isinstance(value, ast.Attribute):
        return True
    # obj['key'] or obj[expr]  # noqa: ERA001
    if isinstance(value, ast.Subscript):
        return True
    # obj.get(...) or some_dict.get(...)  # noqa: ERA001
    if isinstance(value, ast.Call):
        call_name = _call_name(value.func)
        if (call_name and call_name.endswith(".get")) or (
            isinstance(value.func, ast.Name) and value.func.id == "get"
        ):
            return True
    return False


def process_file(filepath: Path) -> list[Suggestion]:
    """Read and parse a file standalone, then return naming suggestions.

    Thin convenience wrapper around `collect_suggestions` for callers (e.g.
    tests) that only have a path, not an already-parsed tree/source. The
    orchestrator-driven check() path must call `collect_suggestions` directly
    with its own shared tree/source instead of this, to avoid re-reading and
    re-parsing a file the orchestrator already parsed once.

    Args:
        filepath: Path to the Python file to analyze

    Returns:
        List of Suggestion objects for functions that should be renamed
    """
    try:
        source = read_source(filepath)
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as error:
        logger.warning("File: %s, error: %s", filepath, repr(error))
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError as syntax_error:
        logger.warning("File: %s, error: %s", filepath, repr(syntax_error))
        return []

    return collect_suggestions(filepath, tree, source)


def collect_suggestions(
    filepath: Path, tree: ast.Module, source: str
) -> list[Suggestion]:
    """Walk an already-parsed tree and return naming suggestions.

    Args:
        filepath: Path to the file (used only to tag returned Suggestions)
        tree: Parsed AST tree
        source: Source code matching `tree`

    Returns:
        List of Suggestion objects for functions that should be renamed
    """
    # attach parent links for better analysis
    attach_parents(tree)

    ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
    suggestions: list[Suggestion] = []

    for node in ast.walk(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith(GET_PREFIX):
            # skip if decorated with override/abstract
            if is_decorator_override_or_abstract(node):
                continue
            if node.lineno in ignored_lines:
                continue
            # skip simple accessors/dict-like getters (idiomatic)
            if is_simple_accessor(node):
                continue

            analysis = analyze_function(node)
            suggested_name, reason = suggest_name_for(node, analysis)

            if suggested_name != node.name:
                suggestions.append(
                    Suggestion(
                        path=filepath,
                        func_name=node.name,
                        lineno=node.lineno,
                        suggested_name=suggested_name,
                        reason=reason,
                    )
                )

    return suggestions


def derive_entity_from_name(func_name: str) -> str:
    if func_name.startswith(GET_PREFIX):
        return func_name[len(GET_PREFIX) :]
    return func_name


def first_docstring_line(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    if (
        func_node.body
        and isinstance(func_node.body[0], ast.Expr)
        and isinstance(func_node.body[0].value, (ast.Constant,))
        and isinstance(func_node.body[0].value.value, str)
    ):
        s = func_node.body[0].value.value.strip()
        return s.splitlines()[0].strip() if s else None
    return None


def extract_first_verb(docstring_line: str) -> str | None:
    """
    Examples:
        "Combine the parameters..." -> "combine"
        "Build a new instance..." -> "build"
        "Merge two dictionaries..." -> "merge"
    """
    if not docstring_line:
        return None

    # Remove common prefixes and split into words
    words = docstring_line.lower().split()
    if not words:
        return None

    # First word is usually the verb (after common articles)
    first_word = words[0]

    # Skip articles and common prefixes
    if first_word in {"a", "an", "the"}:
        if len(words) > 1:
            return words[1]
        return None

    return first_word


def suggest_name_for(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef, analysis: dict[str, bool]
) -> tuple[str, str]:
    """Suggest a better name for a function based on behavioral analysis.

    Args:
        func_node: The function AST node
        analysis: Dictionary of detected behaviors from analyze_function()

    Returns:
        Tuple of (suggested_name, reason)

    Suggestion priority (first match wins):
    1. Properties → noun
    2. Collection/parsing → extract_/parse_
    3. Searching → find_
    4. I/O operations → load_/save_to_/fetch_
    5. Boolean → is_
    6. Aggregation → calculate_
    7. Generator → iter_
    8. Creation → create_
    9. Mutation → update_
    10. Validation → validate_
    11. Rendering → render_
    12. Transformation → transform_
    Fallback: "no confident suggestion"
    """
    old = func_node.name
    entity = derive_entity_from_name(old)

    # Tests: if name starts with test_ -> do not touch
    if old.startswith("test_"):
        return old, "function looks like a test"

    # Skip abstract/override decorated functions
    if is_decorator_override_or_abstract(func_node):
        return old, "skip: decorated with @override or @abstractmethod"

    # If function appears to just delegate to another get_* call
    # and return that result, skip suggestion
    if analysis["delegates_get"]:
        return old, "delegates to another get_ function; skipping suggestion"

    # If function returns a class object, get_ is acceptable
    if analysis["returns_class"]:
        return old, "returns a class object; get_ prefix is acceptable"

    # docstring heuristic: 'Get or create'
    first_line = first_docstring_line(func_node)
    if first_line:
        low = first_line.lower()
        if low.startswith("get or create") or "get or create" in low:
            suggested = f"get_or_create_{entity}" if entity else "get_or_create"
            return suggested, "docstring: 'get or create'"

    # Extract first verb from docstring for better suggestions
    docstring_verb = None
    if first_line:
        docstring_verb = extract_first_verb(first_line)

    # Check if docstring verb matches a recognized pattern
    # Verbs like combine/compose/merge/build should be used directly
    recognized_verbs = {
        "combine",
        "compose",
        "merge",
        "build",
        "assemble",
        "construct",
        "join",
        "concat",
        "concatenate",
        "aggregate",
        "group",
        "union",
    }

    if docstring_verb and docstring_verb in recognized_verbs:
        suggested = f"{docstring_verb}_{entity}" if entity else docstring_verb
        return suggested, f"docstring indicates '{docstring_verb}' operation"

    if analysis["is_property"]:
        suggested = entity or old
        reason = "@property: prefer noun name rather than verb"
        return suggested, reason

    # Detect mock/factory/fixture patterns (test utilities)
    # Functions that create test objects should use create_ prefix
    test_patterns = {
        "mock",
        "stub",
        "fake",
        "dummy",
        "fixture",
        "factory",
        "builder",
    }

    entity_lower = entity.lower() if entity else ""
    old_lower = old.lower()

    # Check if the function name or entity contains test patterns and creates object
    if (
        any(
            pattern in entity_lower or pattern in old_lower for pattern in test_patterns
        )
        and analysis["creates_object"]
    ):
        suggested = f"create_{entity}" if entity else "create"
        return suggested, "creates test object/mock/fixture"

    # collection/parsing/extraction (prefer these before create/update)
    if analysis["collects"]:
        # if parsing was detected (json.loads etc.), prefer parse_ otherwise extract_
        if analysis["parses"]:
            suggested = f"parse_{entity}" if entity else "parse"
            reason = "parses/collects structured data from a source"
            return suggested, reason
        suggested = f"extract_{entity}" if entity else "extract"
        reason = "extracts/collects data (returns list/dict)"
        return suggested, reason

    if analysis["parses"]:
        suggested = f"parse_{entity}" if entity else "parse"
        reason = "parses input (json/yaml/...)"
        return suggested, reason

    # searches/finding patterns (e.g., find_root)
    if analysis["searches"]:
        suggested = f"find_{entity}" if entity else "find"
        reason = "searches or finds an item (filesystem or structure)"
        return suggested, reason

    # disk/network priority
    if analysis["disk_write"] or analysis["network_write"]:
        verb = "save_to" if analysis["disk_write"] else "send"
        suggested = f"{verb}_{entity}" if entity else f"{verb}"
        reason = "persists or sends data (write)"
        return suggested, reason

    if analysis["network_read"]:
        suggested = f"fetch_{entity}" if entity else "fetch"
        reason = "fetches data over network"
        return suggested, reason

    if analysis["disk_read"]:
        suggested = f"load_{entity}" if entity else "load"
        reason = "reads data from disk"
        return suggested, reason

    if analysis["outputs"]:
        suggested = f"print_{entity}" if entity else "print"
        reason = "outputs data to stdout/log"
        return suggested, reason

    if analysis["returns_bool"]:
        suggested = f"is_{entity}" if entity else f"is_{old}"
        reason = "returns a boolean (annotation)"
        return suggested, reason

    if analysis["aggregates"]:
        suggested = f"calculate_{entity}" if entity else "calculate"
        reason = "aggregates or computes a summary"
        return suggested, reason

    if analysis["yields"]:
        suggested = f"iter_{entity}" if entity else "iter"
        reason = "generator/iterator"
        return suggested, reason

    if analysis["creates_object"]:
        suggested = f"create_{entity}" if entity else "create"
        reason = "creates an object"
        return suggested, reason

    if analysis["mutates_args"]:
        suggested = f"update_{entity}" if entity else f"update_{old}"
        reason = "mutates arguments or state"
        return suggested, reason

    if analysis["validates"]:
        suggested = f"validate_{entity}" if entity else f"validate_{old}"
        reason = "performs validation and returns errors"
        return suggested, reason

    if analysis["renders"]:
        suggested = f"render_{entity}" if entity else "render"
        reason = "renders/serializes data to string"
        return suggested, reason

    if analysis["transforms"]:
        suggested = f"transform_{entity}" if entity else "transform"
        reason = "performs a transformation"
        return suggested, reason

    # fallback: no confident suggestion
    return old, "no confident suggestion"
