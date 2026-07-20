from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
    _call_name,
    _get_base_name,
    analyze_function,
    attach_parents,
    decorator_name,
    derive_entity_from_name,
    extract_first_verb,
    first_docstring_line,
    is_decorator_override_or_abstract,
    is_simple_accessor,
    process_file,
    suggest_name_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "validate_function_name"


def _func(source: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(source)
    attach_parents(tree)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def test_attach_parents_handles_deeply_nested_source_without_recursion_error() -> None:
    # A recursive implementation hits Python's default recursion limit
    # (1000) around this depth, even though ast.parse itself accepts
    # source nested far deeper than this as ordinary, valid Python.
    source = "x = " + "not " * 1500 + "True\n"
    tree = ast.parse(source)

    attach_parents(tree)

    deepest: ast.AST = tree
    while (child := next(ast.iter_child_nodes(deepest), None)) is not None:
        deepest = child
    assert deepest.parent is not None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("source", "func_name", "flags"),
    [
        (
            # Regression case from .cache/bug_report.md: get_or_create
            # updating a module-level cache must not be flagged as mutation.
            """
import structlog
from structlog.typing import FilteringBoundLogger

logger: FilteringBoundLogger = structlog.getLogger("app")
_logger_per_query: dict[str, FilteringBoundLogger] = {}

def get_or_create_bound_logger(query) -> FilteringBoundLogger:
    '''Get or create a bound logger for a query.'''
    try:
        return _logger_per_query[query.id]
    except KeyError:
        log = logger.bind(depot_place_id=query.place_of_living)
        _logger_per_query[query.id] = log
        return log
""",
            "get_or_create_bound_logger",
            {"mutates_args": False},
        ),
        (
            """
def get_users(database, filters):
    '''Get users and update filters dict.'''
    filters['processed'] = True  # Mutating argument!
    return database.query(filters)
""",
            "get_users",
            {"mutates_args": True},
        ),
        (
            """
class Cache:
    def get_value(self, key):
        '''Get value and update internal state.'''
        value = self._cache.get(key)
        self.last_accessed = key  # Mutating self
        return value
""",
            "get_value",
            {"mutates_args": True},
        ),
        (
            """
def get_items(container, new_item):
    '''Get items and append to container.'''
    container.append(new_item)  # Mutating argument!
    return container
""",
            "get_items",
            {"mutates_args": True},
        ),
        (
            """
def get_items(source):
    '''Get items from source.'''
    results = []
    for item in source:
        results.append(item)
    return results
""",
            "get_items",
            {"mutates_args": False, "collects": True},
        ),
        (
            """
def get_total(amount):
    '''Get total with tax added.'''
    amount += 10  # Modifying parameter (unusual but possible)
    return amount
""",
            "get_total",
            {"mutates_args": True},
        ),
        (
            """
_cache = []

def get_cached_item(key):
    '''Get item from cache.'''
    _cache.append(key)  # Updating module global, not a parameter
    return _cache[-1]
""",
            "get_cached_item",
            {"mutates_args": False},
        ),
        (
            """
def get_data(regular, /, posonly, *args, kwonly=None, **kwargs):
    '''Get data and potentially mutate various param types.'''
    regular.update({"key": "value"})  # Should flag
    posonly.append(1)  # Should flag
    args[0] = "modified"  # Should flag (if possible)
    kwonly["key"] = "value"  # Should flag
    kwargs["key"] = "value"  # Should flag
    return None
""",
            "get_data",
            {"mutates_args": True},
        ),
        (
            # Limitation: only checks the first attribute level
            # (settings.database, not deeper).
            """
def get_config(settings):
    '''Get config and update nested attributes.'''
    settings.database.connection_string = "new_value"
    return settings
""",
            "get_config",
            {"mutates_args": True},
        ),
        (
            "class Foo:\n    @cached.property\n    def get_data(self):\n        return self._data\n",
            "get_data",
            {"is_property": True},
        ),
        (
            "def get_items(items):\n    for item in items:\n        yield item\n",
            "get_items",
            {"yields": True},
        ),
        (
            "def get_data(path, content):\n    with open(path, 'w') as f:\n        f.write(content)\n",
            "get_data",
            {"disk_write": True},
        ),
        ("def get_data(raw):\n    return json.loads(raw)\n", "get_data", {"parses": True}),
        ("def get_data(obj):\n    return json.dumps(obj)\n", "get_data", {"renders": True}),
        (
            "def get_data(url, payload):\n    return httpx.post(url, payload)\n",
            "get_data",
            {"network_write": True, "network_read": False},
        ),
        (
            # A networking call whose verb isn't in the read or write verb
            # lists (e.g. DELETE) doesn't set either flag.
            "def get_data(url):\n    return httpx.delete(url)\n",
            "get_data",
            {"network_read": False, "network_write": False},
        ),
        ("def get_data(x):\n    print(x)\n    return x\n", "get_data", {"outputs": True}),
        ("def get_data(x):\n    logger.info(x)\n    return x\n", "get_data", {"outputs": True}),
        ("def get_total(values):\n    return sum(values)\n", "get_total", {"aggregates": True}),
        (
            "def get_index(items, target):\n    return items.index(target)\n",
            "get_index",
            {"searches": True},
        ),
        ("def get_data(form):\n    return form.is_valid()\n", "get_data", {"validates": True}),
        ("def get_data(items):\n    return items.transform()\n", "get_data", {"transforms": True}),
        (
            # Returning a variable that was assigned from a get_* call
            # counts as delegation, same as returning the get_* call
            # directly.
            "def get_wrapper(key):\n    value = get_value(key)\n    return value\n",
            "get_wrapper",
            {"delegates_get": True},
        ),
        (
            "def get_items(source):\n"
            "    items = list()\n"
            "    for x in source:\n"
            "        items.append(x)\n"
            "    return items\n",
            "get_items",
            {"collects": True},
        ),
        (
            # Assigning to an attribute of something that isn't a parameter
            # (or self) isn't argument mutation.
            "def get_data():\n    some_module.CONFIG = {}\n    return 1\n",
            "get_data",
            {"mutates_args": False},
        ),
        ("def get_data(arg):\n    arg.count += 1\n    return arg\n", "get_data", {"mutates_args": True}),
        (
            "def get_data():\n    counters.count += 1\n    return 1\n",
            "get_data",
            {"mutates_args": False},
        ),
        (
            "def get_total(total):\n    total += 1\n    return total\n",
            "get_total",
            {"mutates_args": True},
        ),
        (
            "def get_total():\n    total = 0\n    total += 1\n    return total\n",
            "get_total",
            {"mutates_args": False},
        ),
        (
            # A while-loop that calls .exists() (the find_root pattern) is
            # treated as a search/find heuristic.
            "def get_root(path):\n    while not path.exists():\n        path = path.parent\n    return path\n",
            "get_root",
            {"searches": True},
        ),
        (
            "def get_data(form):\n    errors = []\n    return errors\n",
            "get_data",
            {"validates": True},
        ),
        (
            "class Foo:\n    @staticmethod\n    def get_data():\n        return 1\n",
            "get_data",
            {"is_property": False},
        ),
        (
            "def get_wrapper():\n    return get_value()\n",
            "get_wrapper",
            {"delegates_get": True},
        ),
        (
            # Calling a get_* function directly in a return (not first
            # assigned to a variable) doesn't register a delegation-tracked
            # variable, though the direct-return check still detects the
            # delegation itself.
            "def get_wrapper():\n    print(get_value())\n    return get_value()\n",
            "get_wrapper",
            {"delegates_get": True},
        ),
        (
            # A tuple-unpacking assignment target isn't tracked as a
            # delegated variable name, so 'a' was never registered — even
            # though it holds the result of get_raw_pair() — and returning
            # it isn't detected as delegation.
            "def get_pair():\n    a, b = get_raw_pair()\n    return a\n",
            "get_pair",
            {"delegates_get": False},
        ),
        (
            # A mutation-verb call whose func isn't `obj.verb(...)` (e.g. a
            # bare name call) isn't attributed to any variable.
            "def get_data(x):\n    update(x)\n    return x\n",
            "get_data",
            {"mutates_args": False},
        ),
        (
            "def get_value(counter):\n"
            "    while counter < 10:\n"
            "        counter = process(counter)\n"
            "    return counter\n",
            "get_value",
            {"searches": False},
        ),
        (
            "def get_class(name):\n    return type(name, (), {})\n",
            "get_class",
            {"returns_class": True},
        ),
    ],
    ids=[
        "get-or-create-module-cache-not-mutation",
        "argument-mutation-flagged",
        "self-mutation-flagged",
        "argument-append-flagged",
        "local-list-append-not-flagged",
        "augmented-assignment-to-param",
        "module-global-append-not-flagged",
        "all-arg-types-mutation",
        "nested-attribute-access-mutation",
        "is-property-via-attribute-decorator",
        "yields-for-generator",
        "disk-write",
        "parses-json-loads",
        "renders-json-dumps",
        "network-write",
        "network-call-matching-neither-verb",
        "outputs-print",
        "outputs-logger-call",
        "aggregates",
        "searches",
        "validates",
        "transforms",
        "delegates-get-via-assigned-variable",
        "collects-list-call-container",
        "mutates-args-false-non-param-attribute-target",
        "mutates-args-true-augmented-attribute-assignment",
        "mutates-args-false-augmented-attribute-non-param",
        "mutates-args-true-augmented-name-param",
        "mutates-args-false-augmented-local-variable",
        "searches-via-exists-loop",
        "validates-errors-variable",
        "is-property-false-non-property-decorator",
        "delegates-get-direct-return",
        "delegation-tracking-skips-non-assign-parent",
        "delegation-tracking-skips-non-name-assignment-target",
        "mutation-detection-skips-unresolvable-call-name",
        "exists-loop-scan-skips-non-exists-call",
        "returns-class-for-type-call",
    ],
)
def test_analyze_function_flags(source: str, func_name: str, flags: dict[str, bool]) -> None:
    # FunctionBehavior's keys aren't literals here, since `flags` is a
    # dynamic per-case mapping; TypedDict is a plain dict at runtime.
    analysis = cast("dict[str, bool]", analyze_function(_func(source, func_name)))

    for flag, expected in flags.items():
        assert analysis[flag] is expected, flag


def test_process_file_with_get_or_create_cache_pattern(tmp_path: Path) -> None:
    source = """
_cache = {}

def get_or_create_item(key):
    '''Get or create an item in cache.'''
    if key not in _cache:
        _cache[key] = {"data": key}
    return _cache[key]
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    for suggestion in process_file(filepath):
        assert not suggestion.suggested_name.startswith("update_"), (
            f"Should not suggest update_ for cache pattern, got: {suggestion.suggested_name}"
        )


def test_get_function_returning_class_is_not_flagged(tmp_path: Path) -> None:
    source = """
def get_placeholder_backend(original_exception):
    '''Create a placeholder backend class.'''
    class PlaceholderBackend:
        def __init__(*args, **kwargs):
            raise original_exception
    return PlaceholderBackend
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    assert process_file(filepath) == [], "Functions returning classes should keep get_ prefix"


def test_docstring_verb_combine_detected(tmp_path: Path) -> None:
    source = """
def get_combined_revision(*functions):
    '''Combine the parameters of all revisions into a single revision.'''
    params = {}
    for func in functions:
        params.update(func.params)
    return params
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    suggestions = process_file(filepath)

    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "combine_combined_revision"
    assert "combine" in suggestions[0].reason.lower()


def test_mock_creation_suggests_create(tmp_path: Path) -> None:
    source = """
from unittest.mock import MagicMock

def get_mock_response(**kwargs):
    '''Get a mock response for testing.'''
    response_kwargs = {'url': 'http://test.com', 'status': 200}
    response_kwargs.update(kwargs)
    return MagicMock(spec=object, **response_kwargs)
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    suggestions = process_file(filepath)

    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "create_mock_response"
    assert "mock" in suggestions[0].reason.lower()


def test_async_get_function_is_flagged(tmp_path: Path) -> None:
    # Async get_* functions must be flagged, not just sync ones.
    source = """
import requests

class Fetcher:
    async def get_api_data(self, url: str):
        '''Fetch data from API.'''
        return requests.get(url).json()
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    suggestions = process_file(filepath)

    assert len(suggestions) == 1
    assert suggestions[0].func_name == "get_api_data"
    assert suggestions[0].suggested_name == "fetch_api_data"


@pytest.mark.parametrize(
    "expr",
    ["factory()()", "funcs[0]()"],
    ids=["call-result-as-func", "subscript-as-func"],
)
def test_call_name_returns_none_for_non_name_non_attribute_func(expr: str) -> None:
    # A call whose func is neither a Name nor an Attribute (e.g. the result
    # of another call, or a subscript) has no readable dotted name.
    node = ast.parse(expr, mode="eval").body
    assert isinstance(node, ast.Call)
    assert _call_name(node.func) is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("@factory()()\ndef get_data():\n    pass\n", None),
        ("@abc.abstractmethod\ndef get_data():\n    pass\n", "abc.abstractmethod"),
        ("@override\ndef get_data():\n    pass\n", "override"),
        ("@get_deco().method\ndef get_data():\n    pass\n", None),
    ],
    ids=[
        # A decorator that's a call whose own func isn't Name/Attribute
        # (e.g. `@factory()()`) can't be resolved to a name.
        "call-with-unresolvable-func",
        "attribute-form",
        "name-form",
        # An Attribute decorator whose base isn't a plain Name (e.g. the
        # result of a call) can't be resolved to a dotted name.
        "attribute-with-unresolvable-base",
    ],
)
def test_decorator_name(source: str, expected: str | None) -> None:
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert decorator_name(func_node.decorator_list[0]) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("@factory()()\ndef get_data():\n    pass\n", False),
        ("@abc.abstractmethod\ndef get_data():\n    pass\n", True),
        ("@staticmethod\n@abc.abstractmethod\ndef get_data():\n    pass\n", True),
    ],
    ids=[
        # A decorator that isn't a Name/Attribute/resolvable Call is
        # skipped (continue) rather than crashing, and doesn't count as
        # override/abstract.
        "unresolvable-decorator-skipped",
        "attribute-form-detected",
        "continues-past-non-matching-decorator",
    ],
)
def test_is_decorator_override_or_abstract(source: str, *, expected: bool) -> None:
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert is_decorator_override_or_abstract(func_node) is expected


def test_get_base_name_returns_none_for_unsupported_expression() -> None:
    node = ast.parse("a + b", mode="eval").body
    assert _get_base_name(node) is None


@pytest.mark.parametrize(
    "source",
    [
        'def get_data():\n    """Just a docstring, no return."""\n',
        "def get_data():\n    pass\n",
        "def get_data():\n    return\n",
        "def get_data():\n    return compute_stuff()\n",
    ],
    ids=[
        # No return statement at all, so nothing to suggest a rename for.
        "docstring-only-no-return",
        "non-return-single-statement",
        "bare-return",
        "non-get-call-return",
    ],
)
def test_is_simple_accessor_returns_false(source: str) -> None:
    func_node = _func(source, "get_data")
    assert is_simple_accessor(func_node) is False


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "bad").glob("*.py")),
    ids=lambda p: p.name,
)
def test_bad_fixtures_are_flagged(fixture_path: Path) -> None:
    assert process_file(fixture_path)


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "good").glob("*.py")),
    ids=lambda p: p.name,
)
def test_good_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert process_file(fixture_path) == []


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "ignore").glob("*.py")),
    ids=lambda p: p.name,
)
def test_ignore_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert process_file(fixture_path) == []


def _write_bad_syntax(tmp_path: Path) -> Path:
    filepath = tmp_path / "bad_syntax.py"
    filepath.write_text("def get_data(:\n")
    return filepath


@pytest.mark.parametrize(
    "make_path",
    [lambda tmp_path: tmp_path / "does_not_exist.py", _write_bad_syntax],
    ids=["missing-file", "syntax-error"],
)
def test_process_file_error_returns_empty(tmp_path: Path, make_path: Callable[[Path], Path]) -> None:
    assert process_file(make_path(tmp_path)) == []


@pytest.mark.parametrize(
    ("func_name", "entity"),
    [
        ("process_data", "process_data"),
        ("get_data", "data"),
    ],
    ids=["no-get-prefix", "get-prefix-stripped"],
)
def test_derive_entity_from_name(func_name: str, entity: str) -> None:
    assert derive_entity_from_name(func_name) == entity


@pytest.mark.parametrize(
    ("docstring_line", "verb"),
    [
        ("", None),
        ("   ", None),
        ("The", None),
        ("The value is set.", "value"),
        ("Combine the parameters.", "combine"),
    ],
    ids=["empty-string", "whitespace-only", "article-alone", "article-then-word", "leading-verb"],
)
def test_extract_first_verb(docstring_line: str, verb: str | None) -> None:
    assert extract_first_verb(docstring_line) == verb


@pytest.mark.parametrize(
    ("source", "func_name", "suggested_name", "reason"),
    [
        # collect_suggestions only ever calls suggest_name_for with
        # get_-prefixed names (test_ and get_ prefixes are mutually
        # exclusive), so the test_-prefix guard is exercised directly here.
        (
            "def test_something():\n    pass\n",
            "test_something",
            "test_something",
            "function looks like a test",
        ),
        # Likewise, collect_suggestions already filters out
        # override/abstractmethod-decorated functions before calling
        # suggest_name_for, so that guard is exercised directly here too.
        (
            "@override\ndef get_data():\n    pass\n",
            "get_data",
            "get_data",
            "skip: decorated with @override or @abstractmethod",
        ),
        (
            "def get_data(text):\n    items = []\n    items.append(json.loads(text))\n    return items\n",
            "get_data",
            "parse_data",
            "parses/collects structured data from a source",
        ),
        (
            "def get_status(x):\n    print(x)\n    return x\n",
            "get_status",
            "print_status",
            "outputs data to stdout/log",
        ),
        (
            "def get_valid(form):\n    return form.is_valid()\n",
            "get_valid",
            "validate_valid",
            "performs validation and returns errors",
        ),
        (
            "def get_data(items):\n    return items.transform()\n",
            "get_data",
            "transform_data",
            "performs a transformation",
        ),
    ],
    ids=[
        "test-prefixed-untouched",
        "decorated-untouched",
        "parses-and-collects-prefers-parse",
        "outputs-only-suggests-print",
        "validates-only-suggests-validate",
        "transforms-only-suggests-transform",
    ],
)
def test_suggest_name_for(source: str, func_name: str, suggested_name: str, reason: str) -> None:
    func_node = _func(source, func_name)
    analysis = analyze_function(func_node)

    suggested, actual_reason = suggest_name_for(func_node, analysis)

    assert suggested == suggested_name
    assert actual_reason == reason


def test_first_docstring_line_returns_first_stripped_line() -> None:
    func_node = _func(
        'def get_data():\n    """First line.\n    Second line.\n    """\n    return 1\n',
        "get_data",
    )
    assert first_docstring_line(func_node) == "First line."
