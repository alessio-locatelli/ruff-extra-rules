from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
    _call_name,
    _get_base_name,
    _is_capitalized_call,
    _iter_own_scope,
    analyze_function,
    attach_parents,
    collect_suggestions,
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
            "def get_data(url):\n    return httpx.delete(url)\n",
            "get_data",
            {"network_read": False, "network_write": False},
        ),
        ("def get_data(x):\n    print(x)\n    return x\n", "get_data", {"outputs": True}),
        ("def get_total(values):\n    return sum(values)\n", "get_total", {"aggregates": True}),
        (
            "def get_index(items, target):\n    return items.index(target)\n",
            "get_index",
            {"searches": True},
        ),
        ("def get_data(form):\n    return form.is_valid()\n", "get_data", {"validates": True}),
        ("def get_data(items):\n    return items.transform()\n", "get_data", {"transforms": True}),
        (
            "def get_wrapper(key):\n    value = get_value(key)\n    return value\n",
            "get_wrapper",
            {"delegates_get": True},
        ),
        (
            (
                "def get_items(source):\n"
                "    items = list()\n"
                "    for x in source:\n"
                "        items.append(x)\n"
                "    return items\n"
            ),
            "get_items",
            {"collects": True},
        ),
        (
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
            "def get_root(path):\n    while not path.exists():\n        path = path.parent\n    return path\n",
            "get_root",
            {"searches": True},
        ),
        (
            "def get_root(path):\n    while path.parent != path:\n        path = path.parent\n    return path\n",
            "get_root",
            {"searches": True},
        ),
        (
            (
                "def get_data(flag, path):\n"
                "    if flag:\n"
                "        while not path.exists():\n"
                "            path = path.parent\n"
                "    return path\n"
            ),
            "get_data",
            {"searches": False},
        ),
        (
            (
                "def get_data(path):\n"
                "    while True:\n"
                "        def helper():\n"
                "            return path.exists()\n"
                "        break\n"
                "    return path\n"
            ),
            "get_data",
            {"searches": False},
        ),
        (
            (
                "def get_root(path):\n"
                "    while True:\n"
                "        if debug:\n"
                "            path.exists()\n"
                "        else:\n"
                "            path = path.parent\n"
                "    return path\n"
            ),
            "get_root",
            {"searches": False},
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
            "def get_wrapper():\n    print(get_value())\n    return get_value()\n",
            "get_wrapper",
            {"delegates_get": True},
        ),
        (
            "def get_pair():\n    a, b = get_raw_pair()\n    return a\n",
            "get_pair",
            {"delegates_get": False},
        ),
        (
            "def get_data(x):\n    update(x)\n    return x\n",
            "get_data",
            {"mutates_args": False},
        ),
        (
            (
                "def get_value(counter):\n"
                "    while counter < 10:\n"
                "        counter = process(counter)\n"
                "    return counter\n"
            ),
            "get_value",
            {"searches": False},
        ),
        (
            "def get_class(name):\n    return type(name, (), {})\n",
            "get_class",
            {"returns_class": True},
        ),
        (
            (
                "def get_config():\n"
                "    if refresh:\n"
                '        with open("config.json") as f:\n'
                "            return json.load(f)\n"
                "    return _cached_config\n"
            ),
            "get_config",
            {"disk_read": False, "parses": False},
        ),
        (
            (
                "def get_value(key):\n"
                "    try:\n"
                "        return _cache[key]\n"
                "    except KeyError:\n"
                "        value = Expensive(key)\n"
                "        _cache[key] = value\n"
                "        return value\n"
            ),
            "get_value",
            {"creates_object": False},
        ),
        (
            "def get_session():\n    if _session is not None:\n        return _session\n    return Session()\n",
            "get_session",
            {"creates_object": False},
        ),
        (
            (
                "def get_value(key):\n"
                "    try:\n"
                "        return _cache[key]\n"
                "    except KeyError:\n"
                "        pass\n"
                "    return Expensive(key)\n"
            ),
            "get_value",
            {"creates_object": False},
        ),
        (
            "def get_data(should_log):\n    if should_log:\n        log()\n    return Session()\n",
            "get_data",
            {"creates_object": True},
        ),
        (
            "def get_data(n):\n    while n > 0:\n        n = process(n)\n        total = sum([n])\n    return n\n",
            "get_data",
            {"aggregates": False},
        ),
        (
            "def get_data(urls):\n    for url in urls:\n        return requests.get(url).json()\n    return None\n",
            "get_data",
            {"network_read": False},
        ),
        (
            "def get_data(should_log):\n    if should_log:\n        print('x')\n    return open('f').read()\n",
            "get_data",
            {"disk_read": True, "outputs": False},
        ),
        (
            "def get_data(path):\n    with open(path) as f:\n        return f.read()\n",
            "get_data",
            {"disk_read": True},
        ),
        (
            "def get_data(path):\n    if open(path).read():\n        return True\n    return False\n",
            "get_data",
            {"disk_read": True},
        ),
        (
            (
                "def get_data(source):\n"
                "    while source.find(1) is not None:\n"
                "        source = source.next\n"
                "    return source\n"
            ),
            "get_data",
            {"searches": True},
        ),
        (
            "def get_data(path):\n    for line in open(path):\n        print(line)\n    return None\n",
            "get_data",
            {"disk_read": True, "outputs": False},
        ),
        (
            "def get_data(existing):\n    return existing if existing is not None else Expensive()\n",
            "get_data",
            {"creates_object": False},
        ),
        (
            "def get_data(path):\n    return 'ok' if open(path).read() else 'empty'\n",
            "get_data",
            {"disk_read": True},
        ),
        (
            (
                "def get_data(x):\n"
                "    match x:\n"
                "        case 1:\n"
                "            return open('f').read()\n"
                "        case _:\n"
                "            return None\n"
            ),
            "get_data",
            {"disk_read": False},
        ),
        (
            "def get_data():\n    def helper():\n        return open('f').read()\n    return 1\n",
            "get_data",
            {"disk_read": False},
        ),
        (
            "def get_data():\n    f = lambda: open('f').read()\n    return f\n",
            "get_data",
            {"disk_read": False},
        ),
        (
            (
                "def get_data():\n"
                "    class Helper:\n"
                "        def run(self):\n"
                "            return open('f').read()\n"
                "    return Helper\n"
            ),
            "get_data",
            {"disk_read": False, "returns_class": True},
        ),
        (
            "def get_data(paths):\n    return [open(p).read() for p in paths]\n",
            "get_data",
            {"disk_read": True},
        ),
        (
            "def get_data(paths):\n    return (open(p) for p in paths)\n",
            "get_data",
            {"disk_read": False},
        ),
        (
            "def get_thing():\n    return models.User(id=1)\n",
            "get_thing",
            {"creates_object": True},
        ),
        (
            "def get_thing():\n    return self.session(id=1)\n",
            "get_thing",
            {"creates_object": False},
        ),
        (
            (
                "def get_config():\n"
                "    try:\n"
                "        pass\n"
                "    finally:\n"
                '        cfg = open("f").read()\n'
                "    return cfg\n"
            ),
            "get_config",
            {"disk_read": True},
        ),
        (
            'def get_thing():\n    def helper(x=open("f").read()):\n        pass\n    return helper\n',
            "get_thing",
            {"disk_read": True},
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
        "searches-via-parent-in-loop-test-position",
        "guarded-while-loop-search-not-unconditional",
        "nested-def-inside-while-loop-does-not-leak-search",
        "guarded-if-else-inside-while-loop-does-not-count-as-search",
        "validates-errors-variable",
        "is-property-false-non-property-decorator",
        "delegates-get-direct-return",
        "delegation-tracking-skips-non-assign-parent",
        "delegation-tracking-skips-non-name-assignment-target",
        "mutation-detection-skips-unresolvable-call-name",
        "exists-loop-scan-skips-non-exists-call",
        "returns-class-for-type-call",
        "if-guarded-disk-read-not-unconditional",
        "try-guarded-creates-object-not-unconditional",
        "if-guard-clause-no-else-narrows-scope",
        "try-except-guard-clause-narrows-scope",
        "non-diverging-guard-does-not-narrow-scope",
        "while-guarded-call-not-unconditional",
        "for-guarded-call-not-unconditional",
        "unconditional-sibling-after-unrelated-guard-still-counted",
        "with-block-not-a-guard",
        "if-test-position-not-conditional",
        "while-test-position-not-conditional",
        "for-iter-position-not-conditional",
        "ifexp-orelse-branch-is-conditional",
        "ifexp-test-position-not-conditional",
        "match-case-body-is-conditional",
        "nested-function-def-does-not-leak-effects",
        "nested-lambda-does-not-leak-effects",
        "nested-class-does-not-leak-effects",
        "comprehension-is-not-a-scope-boundary",
        "generator-expression-is-lazy-not-unconditional",
        "qualified-capitalized-attribute-call-creates-object",
        "qualified-lowercase-attribute-call-not-creates-object",
        "finally-block-always-runs-still-flagged",
        "nested-helper-default-value-still-flagged",
    ],
)
def test_analyze_function_flags(source: str, func_name: str, flags: dict[str, bool]) -> None:
    analysis = cast("dict[str, bool]", analyze_function(_func(source, func_name)))

    for flag, expected in flags.items():
        assert analysis[flag] is expected, flag


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def get_data():\n    logger.info('message')\n", True),
        ("def get_data():\n    log.warning('message')\n", True),
        ("def get_data():\n    logging.error('message')\n", True),
        ("def get_data():\n    logger.critical('message')\n", True),
        ("def get_data(self):\n    self.logger.debug('message')\n", True),
        ("def get_data(service):\n    service.log.error('message')\n", True),
        ("def get_data(self):\n    self._logger.exception('message')\n", True),
        ("def get_data(service):\n    service._log.critical('message')\n", True),
        ("def get_data():\n    logger.info('message')\n    return value\n", False),
        ("def get_data(client):\n    client.info()\n", False),
        ("def get_data(client):\n    client.status()\n", False),
        ("def get_data():\n    logging.getLogger(__name__).info('message')\n", False),
    ],
    ids=[
        "logger-name",
        "log-name",
        "logging-name",
        "critical-logger-method",
        "logger-attribute",
        "log-attribute",
        "private-logger-attribute",
        "private-log-attribute",
        "logger-call-with-return",
        "unrelated-info-method",
        "unrelated-method",
        "logger-factory-call",
    ],
)
def test_output_detection_requires_identifiable_logger(source: str, *, expected: bool) -> None:
    assert analyze_function(_func(source, "get_data"))["outputs"] is expected


def test_client_info_does_not_propose_output_rename() -> None:
    source = (
        "import asyncio\n"
        "from redis.asyncio import from_url\n"
        "\n"
        "async def get_db_info():\n"
        "    client = await from_url(DEFAULT_ADDRESS)\n"
        "    await client.info()\n"
        "    await client.aclose()\n"
        "\n"
        "def is_db_running():\n"
        "    asyncio.run(get_db_info())\n"
    )

    assert collect_suggestions(Path("test_redis.py"), ast.parse(source), source) == []


def test_lazy_async_accessors_and_context_manager_methods_are_allowed() -> None:
    source = (
        "class Backend:\n"
        "    @asynccontextmanager\n"
        "    async def get_connection(self):\n"
        "        if self.context.cls:\n"
        "            yield self.context.cls\n"
        "        else:\n"
        "            yield await self.context.__aenter__()\n"
        "\n"
        "    async def get_table(self):\n"
        "        if not self._table:\n"
        "            async with self.get_connection() as conn:\n"
        "                if self.create_if_not_exists:\n"
        "                    self._table = await self._create_table(conn)\n"
        "                else:\n"
        "                    self._table = await conn.Table(self.table_name)\n"
        "        return self._table\n"
        "\n"
        "    async def get_client(self):\n"
        "        if not self._client:\n"
        "            self._client = await from_url(self.address)\n"
        "        return self._client\n"
    )

    assert collect_suggestions(Path("backend.py"), ast.parse(source), source) == []


def test_sync_lazy_accessor_requires_property() -> None:
    source = (
        "class Backend:\n"
        "    def get_connection(self):\n"
        "        if not self._connection:\n"
        "            self._connection = connect()\n"
        "        return self._connection\n"
    )

    suggestions = collect_suggestions(Path("backend.py"), ast.parse(source), source)

    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "connection"
    assert suggestions[0].requires_property is True


def test_annotated_sync_lazy_accessor_requires_property() -> None:
    source = (
        "class Backend:\n"
        "    def get_connection(self):\n"
        "        if not self._connection:\n"
        "            self._connection: Connection = connect()\n"
        "        return self._connection\n"
    )

    suggestions = collect_suggestions(Path("backend.py"), ast.parse(source), source)

    assert len(suggestions) == 1
    assert suggestions[0].requires_property is True


def test_lazy_accessor_rejects_unrelated_state_writes() -> None:
    source = (
        "class Backend:\n"
        "    def get_connection(self):\n"
        "        if not self._connection:\n"
        "            self._connection = connect()\n"
        "        self.last_accessed = utcnow()\n"
        "        return self._connection\n"
    )

    suggestions = collect_suggestions(Path("backend.py"), ast.parse(source), source)

    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "update_connection"


def test_logging_does_not_override_response_accessor() -> None:
    source = (
        "async def get_response(self, key):\n"
        "    logger.debug('attempting lookup')\n"
        "    try:\n"
        "        response = await self.responses.read(key)\n"
        "    except KeyError:\n"
        "        response = None\n"
        "    logger.debug('lookup complete')\n"
        "    return response\n"
    )

    assert collect_suggestions(Path("base.py"), ast.parse(source), source) == []


def test_arithmetic_return_suggests_calculation_despite_logging() -> None:
    source = (
        "def get_expiration_datetime(expire_after):\n"
        "    logger.debug('determining expiration')\n"
        "    if expire_after is None:\n"
        "        return None\n"
        "    return utcnow() + expire_after\n"
    )

    suggestions = collect_suggestions(Path("cache_control.py"), ast.parse(source), source)

    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "calculate_expiration_datetime"


def test_branch_local_arithmetic_does_not_suggest_calculation() -> None:
    source = (
        "def get_expiration_datetime(expire_after):\n"
        "    if expire_after is not None:\n"
        "        return utcnow() + expire_after\n"
        "    return None\n"
    )

    assert collect_suggestions(Path("cache_control.py"), ast.parse(source), source) == []


def _conditional_by_call_name(source: str, func_name: str) -> dict[str, bool]:
    func_node = _func(source, func_name)
    return {
        name: conditional
        for node, conditional in _iter_own_scope(func_node)
        if isinstance(node, ast.Call) and (name := _call_name(node.func)) is not None
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def get_data():\n    if flag:\n        open('f')\n    return 1\n", {"open": True}),
        (
            "def get_data():\n    if flag:\n        open('f')\n    else:\n        close('f')\n    return 1\n",
            {"open": True, "close": True},
        ),
        ("def get_data():\n    while flag:\n        open('f')\n    return 1\n", {"open": True}),
        ("def get_data():\n    for x in items:\n        open('f')\n    return 1\n", {"open": True}),
        (
            "def get_data():\n    try:\n        open('f')\n    except OSError:\n        close('f')\n    return 1\n",
            {"open": True, "close": True},
        ),
        (
            "def get_data():\n    try:\n        pass\n    finally:\n        open('f')\n    return 1\n",
            {"open": False},
        ),
        (
            (
                "def get_data():\n"
                "    if flag:\n"
                "        try:\n"
                "            pass\n"
                "        finally:\n"
                "            open('f')\n"
                "    return 1\n"
            ),
            {"open": True},
        ),
        (
            "def get_data():\n    match flag:\n        case 1:\n            open('f')\n    return 1\n",
            {"open": True},
        ),
        (
            "def get_data():\n    match flag:\n        case 1 if open('f'):\n            pass\n    return 1\n",
            {"open": True},
        ),
        (
            "def get_data():\n    x = open('f') if flag else close('f')\n    return x\n",
            {"open": True, "close": True},
        ),
        (
            "def get_data():\n    with open('f') as fh:\n        read(fh)\n    return 1\n",
            {"open": False, "read": False},
        ),
        (
            (
                "def get_data():\n"
                "    with lock:\n"
                "        if cached is not None:\n"
                "            return cached\n"
                "        return Session()\n"
            ),
            {"Session": True},
        ),
        ("def get_data():\n    if open('f').read():\n        return 1\n    return 0\n", {"open": False}),
        ("def get_data():\n    while open('f').read():\n        pass\n    return 1\n", {"open": False}),
        ("def get_data():\n    for x in open('f'):\n        pass\n    return 1\n", {"open": False}),
        (
            "def get_data():\n    for container[open('f')] in items:\n        pass\n    return 1\n",
            {"open": True},
        ),
        (
            "def get_data():\n    return (open(p) for p in paths)\n",
            {"open": True},
        ),
        (
            "def get_data():\n    return (x for x in open('f'))\n",
            {"open": True},
        ),
        (
            (
                "def get_data():\n"
                "    if flag:\n"
                "        try:\n"
                "            pass\n"
                "        finally:\n"
                "            return compute()\n"
                "    return fallback()\n"
            ),
            {"fallback": True},
        ),
        (
            (
                "def get_data():\n"
                "    if flag:\n"
                "        try:\n"
                "            compute()\n"
                "        except ValueError:\n"
                "            return None\n"
                "        else:\n"
                "            return finish()\n"
                "    return fallback()\n"
            ),
            {"fallback": True},
        ),
    ],
    ids=[
        "if-body-conditional",
        "if-else-both-branches-conditional",
        "while-body-conditional",
        "for-body-conditional",
        "try-body-and-except-conditional",
        "finally-body-inherits-unconditional",
        "finally-body-inherits-conditional-from-guarded-try",
        "match-case-body-conditional",
        "match-case-guard-conditional",
        "ifexp-both-branches-conditional",
        "with-body-not-conditional",
        "guard-clause-inside-with-narrows-scope",
        "if-test-not-conditional",
        "while-test-not-conditional",
        "for-iter-not-conditional",
        "for-target-conditional",
        "generator-expression-elt-conditional",
        "generator-expression-outer-iter-conservatively-conditional",
        "nested-try-finally-diverges-narrows-outer-if-scope",
        "nested-try-handlers-and-orelse-diverge-narrows-outer-if-scope",
    ],
)
def test_iter_own_scope_conditional_tagging(source: str, expected: dict[str, bool]) -> None:
    conditional_by_call_name = _conditional_by_call_name(source, "get_data")
    for call_name, expected_conditional in expected.items():
        assert conditional_by_call_name[call_name] is expected_conditional, call_name


@pytest.mark.parametrize(
    ("source", "expected_call_names"),
    [
        ("def get_data():\n    def helper():\n        open('f')\n    return 1\n", set()),
        ("def get_data():\n    f = lambda: open('f')\n    return f\n", set()),
        (
            "def get_data():\n    class Helper:\n        def run(self):\n            open('f')\n    return Helper\n",
            set(),
        ),
        ("def get_data():\n    return [open(p) for p in paths]\n", {"open"}),
        (
            "def get_data():\n    @deco(open('f'))\n    def helper():\n        pass\n    return 1\n",
            {"deco", "open"},
        ),
        (
            "def get_data():\n    def helper(x=open('f')):\n        pass\n    return 1\n",
            {"open"},
        ),
        (
            "def get_data():\n    class Helper(Base(open('f'))):\n        pass\n    return 1\n",
            {"Base", "open"},
        ),
        (
            "def get_data():\n    class Helper:\n        x = open('f')\n    return 1\n",
            {"open"},
        ),
        (
            "def get_data():\n    def helper() -> open('f'):\n        pass\n    return 1\n",
            set(),
        ),
        (
            "def get_data():\n    def helper(x: open('f')):\n        pass\n    return 1\n",
            set(),
        ),
    ],
    ids=[
        "nested-function-def-not-descended",
        "nested-lambda-not-descended",
        "nested-class-not-descended",
        "comprehension-still-descended",
        "decorator-argument-still-descended",
        "parameter-default-still-descended",
        "class-base-and-keyword-still-descended",
        "class-body-top-level-statement-still-descended",
        "return-annotation-not-descended",
        "parameter-annotation-not-descended",
    ],
)
def test_iter_own_scope_scope_boundary(source: str, expected_call_names: set[str]) -> None:
    func_node = _func(source, "get_data")
    call_names = {
        name
        for node, _conditional in _iter_own_scope(func_node)
        if isinstance(node, ast.Call) and (name := _call_name(node.func)) is not None
    }
    assert call_names == expected_call_names


def test_iter_own_scope_handles_deep_if_nesting_without_recursion_error() -> None:
    depth = 90
    lines = ["def get_data():"]
    lines.extend("    " * (level + 1) + "if True:" for level in range(depth))
    lines.append("    " * (depth + 1) + "open('f')")
    lines.append("    return 1")
    source = "\n".join(lines) + "\n"

    func_node = _func(source, "get_data")

    assert list(_iter_own_scope(func_node))


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("Foo()", True),
        ("foo()", False),
        ("mod.Foo()", True),
        ("mod.foo()", False),
        ("self.Session()", True),
        ("factory()()", False),
    ],
    ids=[
        "bare-capitalized-name",
        "bare-lowercase-name",
        "qualified-capitalized-attribute",
        "qualified-lowercase-attribute",
        "self-capitalized-attribute",
        "unresolvable-func-not-name-or-attribute",
    ],
)
def test_is_capitalized_call(expr: str, *, expected: bool) -> None:
    node = ast.parse(expr, mode="eval").body
    assert isinstance(node, ast.Call)
    assert _is_capitalized_call(node.func) is expected


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
        "call-with-unresolvable-func",
        "attribute-form",
        "name-form",
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
        (
            "def test_something():\n    pass\n",
            "test_something",
            "test_something",
            "function looks like a test",
        ),
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
        (
            (
                "def get_lines(filename):\n"
                "    with open(filename) as f:\n"
                "        for line in f:\n"
                "            yield line.strip()\n"
            ),
            "get_lines",
            "iter_lines",
            "generator/iterator",
        ),
        (
            ("from contextlib import contextmanager\n\n@contextmanager\ndef get_transaction():\n    yield object()\n"),
            "get_transaction",
            "get_transaction",
            "context manager name is left to the author",
        ),
        (
            (
                "from contextlib import asynccontextmanager\n"
                "\n"
                "@asynccontextmanager\n"
                "async def get_tempfile_session():\n"
                "    yield object()\n"
            ),
            "get_tempfile_session",
            "get_tempfile_session",
            "context manager name is left to the author",
        ),
        (
            ("import contextlib\n\n@contextlib.contextmanager\ndef get_connection():\n    yield object()\n"),
            "get_connection",
            "get_connection",
            "context manager name is left to the author",
        ),
        (
            ("import contextlib\n\n@contextlib.asynccontextmanager\nasync def get_lock():\n    yield object()\n"),
            "get_lock",
            "get_lock",
            "context manager name is left to the author",
        ),
    ],
    ids=[
        "test-prefixed-untouched",
        "decorated-untouched",
        "parses-and-collects-prefers-parse",
        "outputs-only-suggests-print",
        "validates-only-suggests-validate",
        "transforms-only-suggests-transform",
        "generator-outranks-disk-read",
        "context-manager-name-is-accepted",
        "async-context-manager-name-is-accepted",
        "qualified-context-manager-name-is-accepted",
        "qualified-async-context-manager-name-is-accepted",
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
