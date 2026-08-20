from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._orchestrator import CheckOrchestrator
from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import AggressivenessLevel
from tests.redundant_assignment._helpers import _check


def test_check_id_and_error_code() -> None:
    check = RedundantAssignmentCheck()
    assert check.check_id == "redundant-assignment"
    assert check.error_code == "TR5"


def test_prefilter_pattern() -> None:
    assert RedundantAssignmentCheck().get_prefilter_pattern() == [" = "]


def test_check_reports_character_offset_not_byte_offset_before_multibyte_text() -> None:
    source = 'def f():\n    café; x = "foo"\n    func(x=x)\n'
    violations = _check(source)

    assert len(violations) == 1
    assert violations[0].col == 10


@pytest.mark.parametrize(
    "source",
    [
        """
def is_directory_change(command):
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    try:
        target = tokens[1]
    except IndexError:
        return False

    return target.startswith("/")
""",
        """
def start_daemon():
    try:
        ty_version = _ty_version()
    except OSError as error:
        print(f"FAILED: {error}", flush=True)
        return

        return ty_version
""",
        """
def load_config():
    try:
        config: dict[str, str] = read_config()
    except OSError:
        return None

    try:
        return config["name"]
    except KeyError:
        return None
""",
    ],
)
def test_check_does_not_report_assignments_protected_by_try_handlers(source: str) -> None:
    assert _check(source, level=AggressivenessLevel.PERMISSIVE) == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        ('def example():\n    x = "foo"\n    func(x=x)  # pytriage: TR5\n', 3),
        ('def example():\n    x = "foo"  # pytriage: TR5\n    func(x=x)\n', 2),
    ],
    ids=["use", "assignment"],
)
def test_check_records_a_pytriage_usage(source: str, expected_line: int) -> None:
    check_result = RedundantAssignmentCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [(usage.check_id, usage.error_code, usage.line) for usage in check_result.suppression_usages] == [
        ("redundant-assignment", "TR5", expected_line)
    ]


def test_check_records_each_suppressed_pytriage_usage() -> None:
    source = (
        'def example():\n    x = "foo"\n    func(x=x)  # pytriage: TR5\n    y = "bar"\n    func(y=y)  # pytriage: TR5\n'
    )

    check_result = RedundantAssignmentCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [usage.line for usage in check_result.suppression_usages] == [3, 5]


def test_check_ignores_a_format_suppressed_candidate_when_tracking_usage() -> None:
    source = (
        'def example():\n    x = "foo"\n    func(x=x)  # pytriage: TR5\n    y = "bar"\n    func(y=y)  # fmt: skip\n'
    )

    check_result = RedundantAssignmentCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [usage.line for usage in check_result.suppression_usages] == [3]


@pytest.mark.parametrize(
    "source",
    [
        """
value = calc()
print(value)
log(value)
return value
""",
        """
def example():
    formatted_timestamp = format_iso8601(raw_ts)
    return formatted_timestamp
""",
        """
x = "foo"  # pytriage: TR5
func(x=x)
""",
        """
x = "foo"  # PYTRIAGE: TR5
func(x=x)
""",
        """
# fmt: off
x = "foo"
# fmt: on
func(x=x)
""",
        """
x = "foo"
# fmt: off
func(x=x)
# fmt: on
""",
        """
x = "foo"
func(x=x)  # pytriage: TR5
""",
        """
def func():
    global state
    state = "active"
    return state
""",
        """
class MyClass:
    x = "foo"

    def method(self):
        self.x = "bar"
""",
        """
x, y = get_coords()
print(x)
""",
        """
def example():
    x = "foo"
    y = "bar"
""",
        """
def example():
    calculated_value = expensive_operation()
    return calculated_value
""",
        """def fetch_data():
    error = None
    try:
        return get_data()
    except ValueError as value_error:
        error = value_error
    except TypeError as type_error:
        error = type_error
    except KeyError as key_error:
        error = key_error
    raise error
""",
        """def configure(service_name=None):
    if not service_name:
        service_name = get_caller_module_name()
    return configure_service(service_name)
""",
        """
def func(value):
    old_value = value
    value = compute_new()
    use(old_value)
""",
        """
def func(obj):
    old_attr = obj.attr
    obj.attr = compute_new()
    use(old_attr)
""",
        """
parent_url = "https://example.com"
print(parent_url)
""",
        """
# Configuration URL
_url = "https://example.com"
print(_url)
""",
        """
async def test_json(client):
    response = await get_test_response(client, '/null_content')
    assert await response.json() is None
""",
        """
async def test_func():
    x = await get_value()
    process(x)
""",
        """
import sys

DEFAULT_URL = "https://default.example.com"
parent_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
print(parent_url)
""",
        """
def func(condition):
    value = "yes" if condition else "no"
    return value
""",
        """
def func():
    variable = compute_something_with_very_long_function_name()
    assert variable.attribute_name
""",
        """
def auto_clear_fixture():
    # Exclude cache.
    # The prefixes are hard-coded in external library
    cache_prefixes = ("responses", "redirects")
    process(cache_prefixes)
""",
        # RHS is 26 chars: len('("responses", "redirects")') = 26 >= 25.
        """
def func():
    prefixes = ("responses", "redirects")
    process(prefixes)
""",
        """
def func():
    # First comment line
    # Second comment line
    # Third comment line with URL: https://example.com/path
    variable = calculate_value()
    return variable
""",
        """
def func():
    len_prefix = len(x) + 1
    return arr[len_prefix:]
""",
        """
async def test_func(faker):
    return_value = faker.pystr()

    @decorator
    async def inner_func():
        return return_value

    await inner_func()
""",
        """
async def test_func():
    from unittest.mock import Mock
    mock = Mock()

    async def inner_func():
        mock()
        return "result"

    await inner_func()
    assert mock.call_count == 1
""",
        """
def outer():
    value = calculate()

    def inner():
        return value

    return inner
""",
        """
def level1():
    x = 1

    def level2():
        y = x + 1

        def level3():
            return x + y

        return level3()

    return level2()
""",
        """
async def test_rate_limited_decorator_exceeds_limit(
    backend, faker, rate_limit_params
):
    mock = Mock()
    limit, period = rate_limit_params
    return_value = faker.pystr()

    @rate_limited(backend=backend, limit=limit, period=period, ttl=period)
    async def func():
        mock()
        return return_value

    for _ in range(limit):
        assert await func() == return_value
    assert mock.call_count == limit
""",
        """
global_obj = None

def modify_global():
    global global_obj
    global_obj.attr = "value"
""",
        """
def func():
    obj.x += 1
""",
        """
for i in range(10):
    x = i * 2
    print(x)
""",
        """
def func():
    if v:
        msg = "foo"
    else:
        msg = "bar"

    msg += "spameggs"

    print(msg)
""",
        """
def func():
    global x
    x += 1
""",
        """
def outer():
    x = 1
    def inner():
        nonlocal x
        x += 1
""",
        """
def outer():
    x: str = "outer"
    def inner():
        nonlocal x
        x: str = "modified"
""",
        """
very_long_descriptive_name = 42
use(very_long_descriptive_name)
""",
        """
def find_place_document(place_id):
    collection_places = singleton_factory(mongo_client)[DATABASE_NAME]["places"]
    return collection_places.find_one({"_id": place_id})
""",
        """
def func(depot_data, depots):
    depot_iso_country = depot_data.iso_country  # pytriage: TR5
    return [x for x in depots if x.country == depot_iso_country]
""",
    ],
    ids=[
        "multiple-uses",
        "semantic-value",
        "inline-suppression",
        "inline-suppression-case-insensitive",
        "fmt-off-wrapping-assignment-line",
        "fmt-off-wrapping-use-line-only",
        "inline-suppression-on-use-line-only",
        "global-variable",
        "class-attributes",
        "tuple-unpacking",
        "no-uses",
        "non-fixable-semantic-value",
        "multiple-exception-assignments",
        "conditional-assignment-logic-change",
        "snapshot-before-name-reassignment",
        "snapshot-before-attribute-reassignment",
        "global-scope-without-underscore",
        "global-scope-with-comment-above",
        "await-on-assignment-and-usage",
        "await-on-assignment-only",
        "ternary-operator",
        "ternary-in-function",
        "long-rhs-over-79-chars",
        "comment-above-in-function-scope",
        "rhs-at-25-char-threshold",
        "comment-above-multiline",
        "would-require-parentheses",
        "closure-variable",
        "closure-with-mock",
        "closure-single-use-nested-function",
        "closure-multiple-nested-levels",
        "closure-with-decorator",
        "global-attribute-assignment",
        "augmented-assignment-with-attribute",
        "loop-reassignment",
        "conditional-assignment-with-augmented-use",
        "augmented-assignment-with-global-variable",
        "augmented-assignment-with-nonlocal-variable",
        "annotated-assignment-with-nonlocal",
        "long-variable-name",
        "long-chained-expression",
        "comprehension-false-positive-with-ignore-comment",
    ],
)
def test_check_reports_no_violations(source: str) -> None:
    assert _check(source) == []


@pytest.mark.parametrize(
    ("source", "path", "excluded"),
    [
        (
            """
def f(command):
    value = make()
    match command:
        case "go":
            sink(value)
""",
            "test.py",
            "'value'",
        ),
        (
            """
def outer():
    x = "outer"
    def inner():
        nonlocal x
        x = "modified"
        return x
    return inner()
""",
            "test.py",
            "modified",
        ),
        (
            """def find_route():
    latest_datetime = initial_datetime
    for edge in edges:
        destination_datetime_utc = edge.destination_datetime_utc
        if destination_datetime_utc > latest_datetime:
            latest_datetime = destination_datetime_utc
            break
""",
            "test.py",
            "latest_datetime",
        ),
        (
            """def check_cycle(subgraph, depot_idx):
    out_edge_count = len(subgraph.out_edges(depot_idx))
    in_edge_count = len(subgraph.in_edges(depot_idx))
    has_cycle = bool(find_cycle(subgraph, depot_idx))
    if not all((out_edge_count, in_edge_count, has_cycle)):
        raise ValueError("Invalid graph")
""",
            "test.py",
            "has_cycle",
        ),
        (
            """
async def request_json(
    self,
    url: str,
    *,
    method: str = "GET",
    response_content_type: str = "application/json",
    **kwargs,
) -> dict:
    raw_headers = kwargs.get("headers")
    headers = CIMultiDict(raw_headers or {})
""",
            "test.py",
            "raw_headers",
        ),
        (
            """
def load_translations(language, template_name):
    path = TRANSLATIONS_DIR / f"{language}.json"
    file_path = (
        TRANSLATIONS_DIR / "eng.json"
        if not path.exists() or language is None
        else path
    )
    with open(file_path) as f:
        translations = orjson.loads(f.read())
        return {
            k: v
            for k, v in translations.items()
            if k in {TRANSLATIONS_GENERAL, TEMPLATES_TO_TRANSLATIONS[template_name]}
        }
""",
            "test.py",
            "translations",
        ),
        (
            """
def get_firestore():
    firestore_client = db.client()
    return firestore_client
""",
            "test.py",
            "firestore_client",
        ),
        (
            """
def process_user(data):
    user_email = data["email"]
    send_notification(user_email)
""",
            "test.py",
            "user_email",
        ),
        (
            """
def process_input(data):
    raw_data = fetch_from_api()
    return raw_data
""",
            "test.py",
            "raw_data",
        ),
        (
            """
def find_project_root():
    max_search_depth = 10
    current_dir = Path.cwd()
    for _ in range(max_search_depth):
        if (current_dir / "pyproject.toml").is_file():
            return current_dir
        current_dir = current_dir.parent
""",
            "test.py",
            "max_search_depth",
        ),
        (
            """
def calculate_spacing():
    line_spacing = 1.2
    coords = (x, y + height * line_spacing)
    return coords
""",
            "test.py",
            "line_spacing",
        ),
        (
            """
async def find_nicosia(database):
    nicosia_in_cyprus_id = 101749141
    place = await database.find_one({"_id": nicosia_in_cyprus_id})
    return place
""",
            "test.py",
            "nicosia_in_cyprus_id",
        ),
        (
            """
def test_rate_limit():
    sample_class = SampleClass()
    with pytest.raises(RateLimitError):
        sample_class.sample_method()
""",
            "test.py",
            "sample_class",
        ),
        (
            """
def test_retry():
    decorated_mock_func = retry_service(mock_func)

    with pytest.raises(ValueError, match=error_msg):
        decorated_mock_func()
""",
            "test.py",
            "decorated_mock_func",
        ),
        (
            """
def f():
    x = 5
    x += 1
""",
            "test.py",
            "'x'",
        ),
        (
            """
def get_cache_file(cache):
    redirects_file = cache.redirects.filename  # type: ignore[attr-defined]

    assert redirects_file.startswith(cache_dir)
    return redirects_file
""",
            "test.py",
            "redirects_file",
        ),
        (
            """
async def test_websocket():
    cancelled = False
    ping_started = loop.create_future()

    async def delayed_send_frame():
        nonlocal cancelled
        ping_started.set_result(None)
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise

    await resp.close()
    assert cancelled is True
""",
            "test.py",
            "cancelled",
        ),
        (
            """
def process():
    x = 0
    while x < 10:
        x = x + 1
    return x
""",
            "test.py",
            "'x'",
        ),
        (
            """
async def process(items):
    result = []
    async for item in items:
        result = result + [item]
    return result
""",
            "test.py",
            "'result'",
        ),
        (
            """
import time

def measure():
    start = time.time()
    do_work()
    return start
""",
            "test.py",
            "start",
        ),
        (
            """
def func(condition):
    result = "yes" if condition else "no"
    return result
""",
            "test.py",
            "result",
        ),
        (
            """
def test_flow_control_binary(protocol, out_low_limit, parser_low_limit):
    large_payload = b"b" * (1 + 16 * 2)
    large_payload_size = len(large_payload)
    parser_low_limit._handle_frame(True, WSMsgType.BINARY, large_payload, 0)
    res = out_low_limit._buffer[0]
    assert res == WSMessageBinary(data=large_payload, size=large_payload_size, extra="")
""",
            "test.py",
            "large_payload_size",
        ),
        (
            """
def process(data):
    buffer_length = len(data)
    return process_with_length(data, buffer_length)
""",
            "test.py",
            "buffer_length",
        ),
        (
            """
def get_user(data):
    user_id = data.get("id")
    return fetch_user(user_id)
""",
            "test.py",
            "user_id",
        ),
        (
            """
def test_prepare_photo():
    mock_image = MagicMock()
    mock_vision.Image.return_value = mock_image
    result = gcp_vision._prepare_photo(file_obj)
    assert result == mock_image
""",
            "tests/test_vision.py",
            "mock_image",
        ),
        (
            """
def load_config():
    with open("config.toml", "rb") as file:
        config = tomllib.load(file)
    # Use config outside to reduce nesting
    value = config.get("key", {})
    return value
""",
            "test.py",
            "config",
        ),
        (
            """
def load_config():
    with open("config.toml", "rb") as file:
        config = tomllib.load(file)
    if config:
        do_something()
""",
            "test.py",
            "config",
        ),
        (
            """
def load_config():
    with open("config.toml", "rb") as file:
        config = tomllib.load(file)
    match config:
        case "a":
            do_something()
""",
            "test.py",
            "config",
        ),
        (
            """
def load_paths_to_ignore(project_root, src_dir):
    pyproject_path = project_root / "pyproject.toml"
    with pyproject_path.open("rb") as file:
        config = tomllib.load(file)

    paths_to_ignore = set()
    expressions = (
        config.get("tool", {})
        .get("test_linter", {})
        .get("ignore_path_by_expression", [])
    )
    for pattern in expressions:
        paths_to_ignore |= set(src_dir.glob(pattern))
    return paths_to_ignore
""",
            "test.py",
            "config",
        ),
        (
            """
def fetch_user(user_id):
    with get_db_connection() as conn:
        result = conn.execute("SELECT * FROM users WHERE id = ?", user_id)
        user_data = result.fetchone()
    # Process user_data outside connection to avoid holding it open
    return process_user(user_data)
""",
            "test.py",
            "user_data",
        ),
        (
            """
def process():
    if condition:
        data = load_expensive_data()
    # Use data outside if block
    result = transform(data)
    return result
""",
            "test.py",
            "data",
        ),
        (
            """
def load_with_fallback():
    try:
        data = load_from_api()
    except Exception:
        data = load_from_cache()
    # Use data outside try block
    return process(data)
""",
            "test.py",
            "data",
        ),
        (
            """
def find_routes(depot_data, depots):
    depot_iso_country = depot_data.iso_country
    return [x for x in depots if x.country == depot_iso_country]
""",
            "test.py",
            "depot_iso_country",
        ),
        (
            """
def transform(multiplier, items):
    factor = multiplier.value
    return [x * factor for x in items]
""",
            "test.py",
            "factor",
        ),
        (
            """
def build_map(source_obj, keys):
    prefix = source_obj.namespace
    return {k: f"{prefix}_{k}" for k in keys}
""",
            "test.py",
            "prefix",
        ),
        (
            """
def unique_suffixes(config, items):
    suffix = config.default_suffix
    return {item + suffix for item in items}
""",
            "test.py",
            "suffix",
        ),
        (
            """
def total_score(config, players):
    bonus = config.bonus_points
    return sum(p.score + bonus for p in players)
""",
            "test.py",
            "bonus",
        ),
        (
            """
def example(obj, items):
    val = obj.attr
    result = [x for x in items if x == val]
    return val
""",
            "test.py",
            "val",
        ),
        (
            """
def _make_app():
    app = FastAPI()

    @app.get("/guarded")
    async def guarded(uid):
        return {"uid": uid}

    return app
""",
            "test.py",
            "'app'",
        ),
        (
            """
def func():
    x = "foo"  # some comment
    return x
""",
            "test.py",
            "'x'",
        ),
        (
            """
def func(c):
    x = 1 if c else 0
    return x
""",
            "test.py",
            "'x'",
        ),
    ],
    ids=[
        "match-statement-use",
        "nonlocal-variable",
        "problem-1-loop-reassignment",
        "problem-2-boolean-descriptive-names",
        "verbose-kwargs-get",
        "verbose-parsed-data",
        "firestore-client",
        "user-email-dict-access",
        "descriptive-prefix",
        "magic-number-int",
        "magic-number-float",
        "magic-number-id",
        "pytest-raises-pattern",
        "with-block-pattern",
        "augmented-assignment-use",
        "inline-comment",
        "nonlocal-in-nested-function",
        "while-loop-assignment",
        "async-for-loop-assignment",
        "nondeterministic-call",
        "ternary-operator-ifexp",
        "descriptive-suffix-size",
        "descriptive-suffix-length",
        "descriptive-suffix-id",
        "non-generic-call-result-name",
        "context-manager-assignment-inside-usage-outside",
        "with-block-if-condition-pattern",
        "with-block-match-subject-pattern",
        "context-manager-with-block-pattern",
        "database-connection-pattern",
        "if-block-assignment-inside-usage-outside",
        "try-block-assignment-inside-usage-outside",
        "comprehension-condition",
        "comprehension-element-only",
        "dict-comprehension-only",
        "set-comprehension-only",
        "generator-expression-only",
        "inside-and-outside-comprehension",
        "function-decorator-use",
        "inline-comment-single-use",
        "short-ifexp-single-use",
    ],
)
def test_check_never_flags_variable(source: str, path: str, excluded: str) -> None:
    assert all(excluded not in v.message for v in _check(source, path))


@pytest.mark.parametrize(
    ("source", "excluded"),
    [
        (
            """
RED = "red"
GREEN = "green"
BLUE = "blue"
COLORS = [RED, GREEN, BLUE]
""",
            "'RED'",
        ),
        (
            """
def configure(me):
    state = me.state(State)
    state.value = 5
""",
            "'state'",
        ),
        (
            """
def format_headers(headers):
    ci_headers = CIMultiDict(headers)
    return ", ".join(ci_headers.getall("Cookie"))
""",
            "'ci_headers'",
        ),
        (
            """
def check_warning(conn):
    warning = conn.recv()
    assert warning.category == DeprecationWarning
""",
            "'warning'",
        ),
        (
            """
_GREY = "rgb(201, 203, 207)"
config = {"colors": [_GREY]}
""",
            "'_GREY'",
        ),
    ],
    ids=[
        "color-constants-list-at-module-scope",
        "single-purpose-accessor-mutated",
        "locally-renamed-constructor-call",
        "descriptive-name-for-non-obvious-return-value",
        "screaming-snake-case-string-constant-at-module-scope",
    ],
)
def test_conservative_level_calibration_cases_not_flagged(source: str, excluded: str) -> None:
    assert all(excluded not in v.message for v in _check(source))


def test_screaming_snake_case_string_constant_still_flagged_at_permissive_level() -> None:
    source = """
_GREY = "rgb(201, 203, 207)"
config = {"colors": [_GREY]}
"""
    violations = _check(source, level=AggressivenessLevel.PERMISSIVE)
    assert any("'_GREY'" in v.message for v in violations)


@pytest.mark.parametrize("level", [AggressivenessLevel.CONSERVATIVE, AggressivenessLevel.PERMISSIVE])
def test_dunder_assignment_never_flagged(level: AggressivenessLevel) -> None:
    source = """
__author__ = "Hynek Schlawack"
__copyright__ = "Copyright (c) 2013 " + __author__
"""
    violations = _check(source, level=level)
    assert all("'__author__'" not in v.message for v in violations)


def test_multiple_assignment_targets_not_tracked() -> None:
    source = """
def func():
    a = b = c = some_value()
    return a + b + c
"""
    violations = _check(source)
    assert all("'a'" not in v.message for v in violations)
    assert all("'b'" not in v.message for v in violations)
    assert all("'c'" not in v.message for v in violations)


@pytest.mark.parametrize(
    ("source", "excluded_names"),
    [
        (
            """
def parse(host_part):
    user = None

    if "@" in host_part:
        userinfo, _, hosts = host_part.rpartition("@")
        user, passwd = parse_userinfo(userinfo)
    else:
        hosts = host_part

    return {"username": user}
""",
            ("user",),
        ),
        (
            """
def func(cond):
    a = None
    if cond:
        a = b = compute()
    return a
""",
            ("a",),
        ),
        (
            """
def func(items):
    x = None
    for x in items:
        pass
    return x
""",
            ("x",),
        ),
        (
            """
def func(pairs):
    y = None
    z = None
    for y, z in pairs:
        pass
    return {"y": y, "z": z}
""",
            ("y", "z"),
        ),
        (
            """
def func(ctx_factory):
    x = None
    with ctx_factory() as x:
        pass
    return x
""",
            ("x",),
        ),
        (
            """
def func(ctx_factory):
    a = None
    b = None
    with ctx_factory() as (a, b):
        pass
    return {"a": a, "b": b}
""",
            ("a", "b"),
        ),
    ],
    ids=[
        "tuple-unpacking-target",
        "chained-assignment-target",
        "simple-for-loop-target",
        "tuple-unpacked-for-loop-target",
        "simple-with-as-target",
        "tuple-unpacked-with-as-target",
    ],
)
def test_untracked_rebinding_not_flagged_as_redundant(source: str, excluded_names: tuple[str, ...]) -> None:
    violations = _check(source, level=AggressivenessLevel.PERMISSIVE)
    assert all(f"'{name}'" not in v.message for v in violations for name in excluded_names)


def test_ignore_marker_inside_string_literal_does_not_suppress_violation() -> None:
    source = """
def call_it():
    x = "foo"; note = "# pytriage: TR5"
    func(x=x)
"""
    violations = _check(source)
    assert any(v.line == 3 and "'x'" in v.message for v in violations)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_processor.py",
        "spec/processor_spec.py",
        "__tests__/processor.py",
    ],
)
@pytest.mark.parametrize("level", [AggressivenessLevel.CONSERVATIVE, AggressivenessLevel.PERMISSIVE])
def test_reporting_does_not_depend_on_the_file_path(path: str, level: AggressivenessLevel) -> None:
    source = """
def test_landmark_equal_to_none():
    landmark = Landmark(name="Tower", long_lat=(2.0, 48.0), score=0.9)
    result = landmark.__eq__(None)
    assert result is NotImplemented
"""
    violations = _check(source, path, level=level)
    assert [v.message for v in violations] == [v.message for v in _check(source, "src/processor.py", level=level)]
    assert any("'result'" in v.message for v in violations)


@pytest.mark.parametrize(
    ("source", "substring"),
    [
        (
            """
def example():
    result = get_value()
    return result
""",
            "result",
        ),
        (
            """
def func_scope():
    foo = "foo"
    process(foo)
""",
            "foo",
        ),
        (
            """
def func_scope():
    SOME_VALUE = "somevalue"
    process(SOME_VALUE)
""",
            None,
        ),
        (
            """
_temp = "foo"
print(_temp)
""",
            "_temp",
        ),
        (
            """
def func():
    x = "foo"
    print(x)
""",
            "x",
        ),
        (
            """
async def test_func():
    x = get_value()
    result = await x.fetch()
""",
            "x",
        ),
        (
            """
def test_func():
    x = "foo"
    return x
""",
            "x",
        ),
        (
            """
def example():
    x: str  # Type hint only, no assignment
    x = "value"
    return x
""",
            "'x'",
        ),
    ],
    ids=[
        "single-use-return",
        "literal-identity",
        "literal-identity-with-underscores",
        "global-scope-with-underscore",
        "function-scope-single-use",
        "await-only-on-usage",
        "non-closure-detected",
        "annotated-assignment-without-value",
    ],
)
def test_check_reports_flagged_violation(source: str, substring: str | None) -> None:
    violations = _check(source)
    assert len(violations) >= 1
    if substring is not None:
        assert any(substring in v.message for v in violations)


def test_immediate_single_use_detected() -> None:
    source = """
def func_scope():
    x = "foo"
    func(x=x)
"""
    violations = _check(source)

    assert len(violations) >= 1
    violation = violations[0]
    assert violation.error_code == "TR5"
    assert "x" in violation.message


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        (
            """
def compare_results():
    a = func()
    b = func()
    c = func()
    assert a == b == c
""",
            [],
        ),
        (
            """
def outer():
    a = func()
    use(a)

    def inner():
        b = func()
        use(b)
""",
            [3, 7],
        ),
    ],
    ids=["sibling-assignments", "different-scopes"],
)
def test_check_handles_identical_rhs_in_each_scope(source: str, expected_lines: list[int]) -> None:
    assert [violation.line for violation in _check(source)] == expected_lines


def test_check_does_not_report_lambda_capture() -> None:
    source = """
def f():
    a = func()
    return lambda: a
"""

    assert _check(source) == []


@pytest.mark.parametrize(
    ("source", "var_name", "reported"),
    [
        (
            """
def func(days_with_routes_in_a_row: int) -> int:
    return days_with_routes_in_a_row


def caller() -> int:
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row=days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            True,
        ),
        (
            """
def func(days_with_routes_in_a_row: str) -> str:
    return days_with_routes_in_a_row


def caller() -> str:
    days_with_routes_in_a_row = "foo"
    return func(days_with_routes_in_a_row=days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            True,
        ),
        (
            """
def caller():
    has_permission = check_something()
    return func(has_permission=has_permission)
""",
            "has_permission",
            True,
        ),
        (
            """
def caller() -> int:
    days_with_routes_in_a_row = 42
    return func(total_days=days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
        (
            """
def caller():
    has_permission = check_something()
    return func(has_permission)
""",
            "has_permission",
            False,
        ),
        (
            """
def func(days_with_routes_in_a_row: int) -> int:
    return days_with_routes_in_a_row


def caller() -> int:
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row",
            False,
        ),
    ],
    ids=[
        "numeric-literal-echo",
        "string-literal-echo",
        "descriptive-prefix-call-rhs-echo",
        "different-keyword-not-echo",
        "call-rhs-positional-not-echo",
        "positional-argument-not-echo",
    ],
)
def test_keyword_argument_echo_reporting(source: str, var_name: str, *, reported: bool) -> None:
    violations = _check(source)
    if reported:
        assert any(var_name in v.message for v in violations)
    else:
        assert all(var_name not in v.message for v in violations)


def test_annotated_assignment_tracked() -> None:
    source = """
def example():
    x: str = "foo"
    func(x)
"""
    assert _check(source) == []
    assert len(_check(source, level=AggressivenessLevel.PERMISSIVE)) >= 1


def test_fixable_marked_correctly() -> None:
    source = """
def func_scope():
    x = "foo"
    func(x=x)
"""
    violations = _check(source)
    assert any(v.fixable for v in violations)


def test_fixable_violation_message_has_no_embedded_tags() -> None:
    source = """
def func():
    x = "foo"
    return x
"""
    fixable_violations = [v for v in _check(source) if v.fixable]
    assert fixable_violations

    for v in fixable_violations:
        assert "[FIXABLE]" not in v.message
        assert "Run with --fix" not in v.message


@pytest.mark.parametrize(
    ("source", "message_filter"),
    [
        (
            """
def func():
    value = foo(
        1
    )
    return value
""",
            "value",
        ),
        (
            """
def func():
    value = some_func(a, b, c)
    return value
""",
            "value",
        ),
        (
            """
def f():
    source = "..."
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    # comment
    violations = check.check(Path("tests/test_long_name.py"), tree, source)
""",
            "'tree'",
        ),
    ],
    ids=["multiline-rhs", "complex-call-args", "long-use-line"],
)
def test_check_does_not_mark_unfixable_violation_fixable(source: str, message_filter: str) -> None:
    matching = [v for v in _check(source) if message_filter in v.message]
    assert matching
    assert all(not v.fixable for v in matching)


def test_autofix_not_in_control_flow() -> None:
    source = """
def example():
    if condition:
        x = "value"
        process(x)
"""
    violations = _check(source)
    assert violations
    for v in violations:
        assert not v.fixable


def test_autofix_only_simple_rhs() -> None:
    source = """
def example():
    result = func(arg1, arg2, arg3)
    return result
"""
    violations = _check(source)
    assert violations
    for v in violations:
        assert not v.fixable


def test_async_with_body_assignment_flagged_but_not_fixable() -> None:
    source = """
async def process():
    async with context() as ctx:
        x = ctx.value
        return x
"""
    violations = _check(source)
    assert len(violations) == 1
    assert violations[0].fixable is False


def test_orchestrator_skips_file_with_invalid_syntax(tmp_path: Path) -> None:
    filepath = tmp_path / "broken.py"
    filepath.write_text("x = (((")

    orchestrator = CheckOrchestrator(checks=[RedundantAssignmentCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations.get(str(filepath), []) == []  # pytriage: TR6
