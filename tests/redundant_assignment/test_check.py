from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks._orchestrator import CheckOrchestrator
from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from tests.redundant_assignment._helpers import _check

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Check metadata
# ---------------------------------------------------------------------------


def test_check_id_and_error_code() -> None:
    check = RedundantAssignmentCheck()
    assert check.check_id == "redundant-assignment"
    assert check.error_code == "TRI005"


def test_prefilter_pattern() -> None:
    assert RedundantAssignmentCheck().get_prefilter_pattern() == [" = "]


def test_check_reports_character_offset_not_byte_offset_before_multibyte_text() -> None:
    # Regression: ast.col_offset is a UTF-8 *byte* offset, not a character
    # offset -- storing it on Violation.col directly reports a column too
    # far right on any line with non-ASCII text before the violation
    # (ch. 7: "MUST report ... column information accurately"; ch. 20:
    # "MUST handle multibyte Unicode characters correctly"). "    café; " is
    # 10 characters but 11 UTF-8 bytes ('é' is 2 bytes), so a byte-offset
    # column would over-count "x"'s own position by one.
    source = 'def f():\n    café; x = "foo"\n    func(x=x)\n'
    violations = _check(source)

    assert len(violations) == 1
    assert violations[0].col == 10


# ---------------------------------------------------------------------------
# check(): reports no violations at all
# ---------------------------------------------------------------------------


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
x = "foo"  # pytriage: ignore=TRI005
func(x=x)
""",
        """
x = "foo"  # PYTRIAGE: IGNORE=TRI005
func(x=x)
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
        # `error` is assigned multiple times (once per except branch), so it
        # is skipped entirely rather than risk autofix producing
        # concatenated nonsense like "value_errortype_errorkey_error".
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
        # `service_name` is assigned inside an `if` block but used outside
        # it, so it is skipped entirely rather than risk autofix changing
        # program logic (e.g. turning it into "if not
        # get_caller_module_name():").
        """def configure(service_name=None):
    if not service_name:
        service_name = get_caller_module_name()
    return configure_service(service_name)
""",
        # "Snapshot the old value before reassigning it" (issue #74):
        # inlining `old_value` as `value` after `value` has been rebound
        # would silently read the new value instead of the one captured at
        # assignment time.
        """
def func(value):
    old_value = value
    value = compute_new()
    use(old_value)
""",
        # Same hazard for an Attribute RHS: `obj.attr` is reassigned
        # between the tracked assignment and its use.
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
        # Inlining await expressions often requires parentheses, making
        # code bulky: `json_resp = await resp.json(); return
        # json_resp['key']` would become `return (await
        # resp.json())['key']`.
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
        # The heuristic checks if len(rhs_source) >= 25 or len_diff > 15;
        # len(rhs_source) = 49 >= 25, so this should not be flagged.
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
        # A `nonlocal` binding captures the outer variable, so an
        # augmented assignment through it is a real subsequent use, not a
        # redundant local assignment.
        """
def outer():
    x = 1
    def inner():
        nonlocal x
        x += 1
""",
        # A `nonlocal` binding on an annotated reassignment is likewise a
        # real use of the outer name.
        """
def outer():
    x: str = "outer"
    def inner():
        nonlocal x
        x: str = "modified"
""",
        # Long variable names (>10 chars) are excluded from autofix as a
        # conservative proxy for lines that would grow too long when
        # inlined — the assignment is skipped entirely, not merely marked
        # unfixable.
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
    depot_iso_country = depot_data.iso_country  # pytriage: ignore=TRI005
    return [x for x in depots if x.country == depot_iso_country]
""",
    ],
    ids=[
        "multiple-uses",
        "semantic-value",
        "inline-suppression",
        "inline-suppression-case-insensitive",
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


# ---------------------------------------------------------------------------
# check(): a specific variable is never mentioned (other violations may exist)
# ---------------------------------------------------------------------------


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
            # An augmented-assignment target (`x += 1`) is a mutation, not
            # a read-then-pass-through — and even if it were flagged,
            # inlining it would produce invalid syntax (`x = 5; x += 1` ->
            # `5 += 1`). It must never be reported, let alone marked
            # fixable.
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
            # Regression: the linter used to remove a variable that was
            # modified via nonlocal in a nested function.
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
            # `large_payload_size = len(large_payload)` clarifies what the
            # value represents.
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
def test_camel_to_under():
    camel_case_sample = "RandomClassName"
    assert camel_to_under(camel_case_sample) == "random_class_name"
""",
            "tests/test_utils.py",
            "camel_case_sample",
        ),
        (
            """
def test_translate_templates():
    templates = ["Hello", "Goodbye"]
    translator = MockTranslator(templates)
    assert translator.templates == templates
""",
            "test_translator.py",
            "templates",
        ),
        (
            """
def test_landmark_equal_to_none():
    landmark = Landmark(name="Tower", long_lat=(2.0, 48.0), score=0.9)
    result = landmark.__eq__(None)
    assert result is NotImplemented
""",
            "tests/test_model.py",
            "result",
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
def test_airport_connectivity():
    some_european_airports = ["AES", "BYJ", "BTS"]
    assert all(
        iata in airport_connectivity.airports_by_continent
        for iata in some_european_airports
    )
""",
            "tests/test_kiwi_api.py",
            "some_european_airports",
        ),
        (
            """
def generate_price_data():
    days_with_routes_in_a_row = range(70)
    return [
        faker.pyint(min_value=50, max_value=MAX_PRICE_EUR)
        for _ in days_with_routes_in_a_row
    ]
""",
            "tests/test_flight_prices.py",
            "days_with_routes_in_a_row",
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
            # A statement's own test always evaluates unconditionally when
            # the statement is reached — only its body/branches are
            # conditional. The with-block exception must apply here just
            # like it does for a plain statement use (issue #73).
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
            # This caches an attribute lookup for reuse inside a
            # comprehension filter; inlining would re-evaluate
            # depot_data.iso_country on every iteration.
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
            # A variable used both inside AND outside a comprehension has
            # multiple uses, so detect_redundancy returns None and it is
            # never flagged regardless.
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
            # The decorator expression is evaluated in the outer scope, so
            # any variable referenced there counts as a use. 'app' is used
            # twice: once in @app.get(...) and once in 'return app', so it
            # is not single-use and must not be flagged.
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
            # A ternary expression short enough (<25 chars) to pass the
            # line-length check but still excluded by the IfExp guard.
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
        "test-file-detection-by-path",
        "test-file-detection-by-name",
        "test-result-variable",
        "test-mock-object",
        "semantic-test-data-list",
        "range-with-descriptive-name",
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


def test_multiple_assignment_targets_not_tracked() -> None:
    source = """
def func():
    a = b = c = some_value()
    return a + b + c
"""
    violations = _check(source)
    # Multiple assignment targets are skipped entirely.
    assert all("'a'" not in v.message for v in violations)
    assert all("'b'" not in v.message for v in violations)
    assert all("'c'" not in v.message for v in violations)


def test_ignore_marker_inside_string_literal_does_not_suppress_violation() -> None:
    # A string literal that merely contains the ignore-marker text is not
    # a real suppression comment and must not hide a violation on that
    # line. Regression: line-based ignore detection used a plain text
    # search over raw source lines, so a string literal containing '#
    # pytriage: ignore=...' text was indistinguishable from an actual
    # comment. Ignore detection must be tokenize-based so it only matches
    # genuine COMMENT tokens.
    source = """
def call_it():
    x = "foo"; note = "# pytriage: ignore=TRI005"
    func(x=x)
"""
    violations = _check(source)
    assert any(v.line == 3 and "'x'" in v.message for v in violations)


def test_non_test_file_still_flags_simple_assignments() -> None:
    source = """
def process_data():
    x = "foo"
    return x
"""
    violations = _check(source, "src/processor.py")
    assert len(violations) > 0
    assert any("x" in v.message for v in violations)


# ---------------------------------------------------------------------------
# check(): reports a flagged violation
# ---------------------------------------------------------------------------


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
    assert violation.error_code == "TRI005"
    assert "x" in violation.message


def test_annotated_assignment_tracked() -> None:
    source = """
def example():
    x: str = "foo"
    func(x)
"""
    # Type annotation adds 15 points, but 'x' literal is still low value.
    assert len(_check(source)) >= 1


def test_fixable_marked_correctly() -> None:
    source = """
def func_scope():
    x = "foo"
    func(x=x)
"""
    violations = _check(source)
    # Simple case: constant assignment, immediate use, short name, no
    # control flow.
    assert any(v.fixable for v in violations)


def test_fixable_violation_message_has_no_embedded_tags() -> None:
    # [FIXABLE] and 'Run with --fix' are presentation concerns emitted by
    # the output layer (main()), not part of the machine-readable
    # violation message. Embedding them in the message caused '[FIXED]
    # [FIXABLE] ... Run with --fix...' output when --fix was already used.
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
    # Regression test: violations used to be marked [FIXABLE] even when
    # --fix couldn't actually fix them.
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
    x = func(arg1, arg2)
    return x
"""
    violations = _check(source)
    assert violations
    for v in violations:
        assert not v.fixable


def test_async_with_body_assignment_flagged_but_not_fixable() -> None:
    # An `async with` body, like a plain `with`, always runs exactly once,
    # so the assignment inside it is still redundant — but it's tracked as
    # control flow, so it isn't marked fixable.
    source = """
async def process():
    async with context() as ctx:
        x = ctx.value
        return x
"""
    violations = _check(source)
    assert len(violations) == 1
    assert violations[0].fixable is False


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def test_orchestrator_skips_file_with_invalid_syntax(tmp_path: Path) -> None:
    # Files with invalid syntax must not crash the check pipeline. Syntax
    # errors are caught by CheckOrchestrator._check_file (it parses the
    # AST once for all checks), not by RedundantAssignmentCheck itself.
    filepath = tmp_path / "broken.py"
    filepath.write_text("x = (((")

    orchestrator = CheckOrchestrator(checks=[RedundantAssignmentCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations.get(str(filepath), []) == []
