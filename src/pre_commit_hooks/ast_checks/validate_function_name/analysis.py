"""AST analysis and behavior detection for function naming."""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from pre_commit_hooks.ast_checks._base import find_ignored_lines, ignore_pattern_for, read_source_with_encoding

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("validate_function_name")

# Format: # pytriage: ignore=TRI004
IGNORE_PATTERN = ignore_pattern_for("TRI004")
GET_PREFIX = "get_"


@dataclass(slots=True)
class Suggestion:
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
    if isinstance(d, ast.Name):
        return d.id
    if isinstance(d, ast.Attribute):
        # e.g. abc.abstractmethod
        return _call_name(d)
    return None


def is_decorator_override_or_abstract(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    for d in func_node.decorator_list:
        name = decorator_name(d)
        if not name:
            continue
        lname = name.lower()
        if lname.endswith(("override", "abstractmethod")):
            return True
    return False


class FunctionBehavior(TypedDict):
    """Detected behavior flags used by `suggest_name_for` to pick a naming pattern."""

    is_property: bool
    disk_read: bool  # open(), .read_text(), .load(), etc.
    disk_write: bool  # .write(), .save(), .dump()
    network_read: bool  # e.g. requests.get
    network_write: bool  # e.g. requests.post
    outputs: bool
    returns_bool: bool  # based on the return annotation, not the return values
    aggregates: bool  # sum/min/max/mean/etc.
    creates_object: bool
    mutates_args: bool
    yields: bool
    parses: bool  # json.loads, yaml.safe_load, etc.
    renders: bool  # json.dumps, yaml.dump, etc.
    searches: bool
    validates: bool
    transforms: bool
    delegates_get: bool  # calls, and returns, another get_* function's result
    collects: bool  # builds up a list/dict and returns it
    returns_class: bool  # returns a class object, not an instance


def analyze_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> FunctionBehavior:
    flags: FunctionBehavior = {
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

    for deco in func_node.decorator_list:
        if (isinstance(deco, ast.Name) and deco.id == "property") or (
            isinstance(deco, ast.Attribute) and deco.attr.endswith("property")
        ):
            flags["is_property"] = True

    # Rely only on the annotation, not on inspecting actual return values.
    if func_node.returns is not None:
        ann = func_node.returns
        if isinstance(ann, ast.Name) and ann.id == "bool":
            flags["returns_bool"] = True

    # Populated below, consumed by the delegation/collection detection
    # further down in this function.
    get_assigned_vars: set[str] = set()
    created_containers: set[str] = set()
    appended_to: set[str] = set()
    has_loop_checking_exists_or_parent = False

    defined_classes: set[str] = set()
    for stmt in func_node.body:
        if isinstance(stmt, ast.ClassDef):
            defined_classes.add(stmt.name)

    for node in ast.walk(func_node):
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            flags["yields"] = True
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if not name:
                continue
            lname = name.lower()
            if lname == "open" or lname.endswith((".read", ".read_text", ".read_bytes", ".load")):
                flags["disk_read"] = True
            if lname.endswith((".write", ".save", ".dump")):
                flags["disk_write"] = True
            if lname.endswith(("json.loads", "yaml.safe_load", "yaml.load")):
                flags["parses"] = True
            if lname.endswith(("json.dumps", "yaml.dump")):
                flags["renders"] = True
            if any(lib in lname for lib in ("requests", "httpx", "urllib", "aiohttp", "socket", "grpc")):
                if any(verb in lname for verb in ("get", "fetch", "download", "read", "recv", "request")):
                    flags["network_read"] = True
                if any(verb in lname for verb in ("post", "put", "send", "upload", "patch")):
                    flags["network_write"] = True
            if lname == "print" or (lname.endswith(".write") and ("stdout" in lname or "stderr" in lname)):
                flags["outputs"] = True
            if lname.endswith((".info", ".debug", ".warning", ".error")):
                flags["outputs"] = True
            if lname in (
                "sum",
                "min",
                "max",
                "statistics.mean",
                "statistics.median",
            ) or lname.endswith(".aggregate"):
                flags["aggregates"] = True
            if lname.endswith((".find", ".search", ".index")):
                flags["searches"] = True
            if lname.endswith((".is_valid", ".validate")):
                flags["validates"] = True
            if lname.endswith((".transform", ".map")):
                flags["transforms"] = True
            # Capitalized callee is a Python convention for a class/constructor.
            if isinstance(node.func, ast.Name) and node.func.id and node.func.id[0].isupper():
                flags["creates_object"] = True

            if isinstance(node.func, ast.Name) and node.func.id.startswith(GET_PREFIX):
                parent = getattr(node, "parent", None)
                if parent and isinstance(parent, ast.Assign):
                    for t in parent.targets:
                        if isinstance(t, ast.Name):
                            get_assigned_vars.add(t.id)
                # direct return of call handled later

        # Mutation detection spans this Assign branch, the AugAssign branch
        # below, and the append/extend/add Call branch further down.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    # x = [] or x = {} or x = list()/dict()
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
                    base_name = _get_base_name(target)
                    if base_name and (base_name in param_names or base_name == "self"):
                        flags["mutates_args"] = True
                if isinstance(target, ast.Subscript):
                    base_name = _get_base_name(target)
                    if base_name and (base_name in param_names or base_name == "self"):
                        flags["mutates_args"] = True
        if isinstance(node, ast.AugAssign):
            target = node.target
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                base_name = _get_base_name(target)
                if base_name and (base_name in param_names or base_name == "self"):
                    flags["mutates_args"] = True
            else:
                # An augmented assignment target is always a Name,
                # Attribute, or Subscript.
                assert isinstance(target, ast.Name)
                if target.id in param_names:
                    flags["mutates_args"] = True
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            # 'x.append(...)' -> node.func is Attribute with value Name('x')
            if (
                name
                and any(
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
                )
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
            ):
                var_name = node.func.value.id
                appended_to.add(var_name)
                if var_name in param_names or var_name == "self":
                    flags["mutates_args"] = True

        # Heuristic for a find_root-style function: a while loop polling
        # .exists()/.parent (e.g. walking up a filesystem tree).
        if isinstance(node, ast.While):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    n = _call_name(sub.func)
                    if n and n.endswith(".exists"):
                        has_loop_checking_exists_or_parent = True
                if isinstance(sub, ast.Attribute) and sub.attr == "parent":
                    has_loop_checking_exists_or_parent = True

    # Delegation: the function returns a variable assigned by get_*, or
    # returns a call to get_* directly.
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return):
            if isinstance(node.value, ast.Call):
                call_name = _call_name(node.value.func)
                if call_name and call_name.startswith(GET_PREFIX):
                    flags["delegates_get"] = True
            if isinstance(node.value, ast.Name) and node.value.id in get_assigned_vars:
                flags["delegates_get"] = True
            if isinstance(node.value, ast.Name) and node.value.id in created_containers:
                flags["collects"] = True
            if isinstance(node.value, ast.Name) and node.value.id in defined_classes:
                flags["returns_class"] = True
            # type()/type[...] calls are metaclass operations.
            if isinstance(node.value, ast.Call):
                call_name = _call_name(node.value.func)
                if call_name == "type":
                    flags["returns_class"] = True

    if created_containers & appended_to:
        flags["collects"] = True

    if has_loop_checking_exists_or_parent:
        flags["searches"] = True

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
    """Attach parent references to AST nodes for better analysis.

    Iterative (explicit stack), not recursive: this runs on every file
    `collect_suggestions` processes, unconditionally and over the whole
    tree, so its traversal depth is the file's full AST depth — a
    recursive version hits Python's default recursion limit around 1000
    levels of nesting (e.g. `not not not ... True`), well within what
    `ast.parse` itself still accepts as valid, ordinary-looking Python.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            child.parent = current  # type: ignore[attr-defined]
            stack.append(child)


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
    """True for a single 'return self.attr'/'return obj[attr]'/'return obj.get(...)'
    body — these are idiomatic getters and should not be flagged.
    """
    # A function's body is never empty (Python requires at least one
    # statement), so only the post-docstring-strip check below can be.
    body = func_node.body
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
        if (call_name and call_name.endswith(".get")) or (isinstance(value.func, ast.Name) and value.func.id == "get"):
            return True
    return False


def process_file(filepath: Path) -> list[Suggestion]:
    """Read and parse a file standalone, then return naming suggestions.

    Thin convenience wrapper around `collect_suggestions` for callers (e.g.
    tests) that only have a path, not an already-parsed tree/source. The
    orchestrator-driven check() path must call `collect_suggestions` directly
    with its own shared tree/source instead of this, to avoid re-reading and
    re-parsing a file the orchestrator already parsed once.
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


def collect_suggestions(filepath: Path, tree: ast.Module, source: str) -> list[Suggestion]:
    """`filepath` is used only to tag returned Suggestions; `tree` must already be parsed from `source`."""
    attach_parents(tree)

    ignored_lines = find_ignored_lines(source, IGNORE_PATTERN)
    suggestions: list[Suggestion] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(GET_PREFIX):
            if is_decorator_override_or_abstract(node):
                continue
            if node.lineno in ignored_lines:
                continue
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

    words = docstring_line.lower().split()
    if not words:
        return None

    # The first word is usually the verb, unless it's a leading article.
    first_word = words[0]
    if first_word in {"a", "an", "the"}:
        if len(words) > 1:
            return words[1]
        return None

    return first_word


def suggest_name_for(func_node: ast.FunctionDef | ast.AsyncFunctionDef, analysis: FunctionBehavior) -> tuple[str, str]:
    """Returns (suggested_name, reason).

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

    if old.startswith("test_"):
        return old, "function looks like a test"

    if is_decorator_override_or_abstract(func_node):
        return old, "skip: decorated with @override or @abstractmethod"

    if analysis["delegates_get"]:
        return old, "delegates to another get_ function; skipping suggestion"

    if analysis["returns_class"]:
        return old, "returns a class object; get_ prefix is acceptable"

    first_line = first_docstring_line(func_node)
    if first_line:
        low = first_line.lower()
        if low.startswith("get or create") or "get or create" in low:
            suggested = f"get_or_create_{entity}" if entity else "get_or_create"
            return suggested, "docstring: 'get or create'"

    docstring_verb = None
    if first_line:
        docstring_verb = extract_first_verb(first_line)

    # These verbs should be used directly, bypassing the analysis-based
    # heuristics below.
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

    # Functions that create test objects (mock/factory/fixture patterns)
    # should use the create_ prefix.
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

    if any(pattern in entity_lower or pattern in old_lower for pattern in test_patterns) and analysis["creates_object"]:
        suggested = f"create_{entity}" if entity else "create"
        return suggested, "creates test object/mock/fixture"

    # Collection/parsing/extraction is checked before create/update below.
    if analysis["collects"]:
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

    if analysis["searches"]:
        suggested = f"find_{entity}" if entity else "find"
        reason = "searches or finds an item (filesystem or structure)"
        return suggested, reason

    # Writes are checked before reads.
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

    return old, "no confident suggestion"
