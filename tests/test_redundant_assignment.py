"""Tests for TRI005 redundant assignment check."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks import CheckOrchestrator
from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from pre_commit_hooks.ast_checks.redundant_assignment.analysis import (
    AssignmentInfo,
    PatternType,
    UsageInfo,
    VariableLifecycle,
    VariableTracker,
    _evaluation_order_children,
    _has_comment_above,
    _has_inline_comment,
    detect_redundancy,
    is_preceded_by_call,
)
from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
    _can_safely_inline,
    _cleanup_blank_lines_around_removals,
    apply_fixes,
)
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import (
    _adds_verbosity_or_context,
    _contains_nondeterministic_call,
    _is_named_constant_pattern,
    _is_test_file,
    _would_require_parentheses,
    calculate_semantic_value,
    should_autofix,
)
from tests.factories import ViolationFactory

if TYPE_CHECKING:
    from pre_commit_hooks.ast_checks._base import Violation

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _check(source: str, path: str = "test.py") -> list[Violation]:
    return RedundantAssignmentCheck().check(Path(path), ast.parse(source), source)


def _lifecycle_for(source: str, var_name: str) -> VariableLifecycle:
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    return next(lc for lc in tracker.build_lifecycles() if lc.assignment.var_name == var_name)


def _lifecycle_count(source: str, var_name: str) -> int:
    tracker = VariableTracker(source)
    tracker.visit(ast.parse(source))
    return len([lc for lc in tracker.build_lifecycles() if lc.assignment.var_name == var_name])


def _make_single_use_lifecycle(
    rhs_source: str,
    rhs_node: ast.expr,
    var_name: str = "x",
    *,
    in_loop: bool = False,
    in_control_flow: bool = False,
    preceded_by_call: bool = False,
) -> VariableLifecycle:
    """Build a minimal VariableLifecycle for direct should_autofix tests.

    The use's node/enclosing_stmt are built from a real parsed statement (not
    hand-faked AST nodes) so analysis.is_preceded_by_call sees a genuinely
    consistent tree: `{var_name}.method()` when preceded_by_call is False
    (var_name is the first thing evaluated), or
    `sink(side_effect(), {var_name})` when True (a sibling call precedes it).
    """
    assignment = AssignmentInfo(
        var_name=var_name,
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=rhs_source,
        scope_id=1,
        has_type_annotation=False,
        in_loop=in_loop,
        in_control_flow=in_control_flow,
        in_global_scope=False,
    )
    use_stmt_source = f"sink(side_effect(), {var_name})" if preceded_by_call else f"{var_name}.method()"
    use_stmt = ast.parse(use_stmt_source).body[0]
    use_node = next(
        n for n in ast.walk(use_stmt) if isinstance(n, ast.Name) and n.id == var_name and isinstance(n.ctx, ast.Load)
    )
    use = UsageInfo(
        var_name=var_name,
        line=2,
        col=0,
        stmt_index=1,
        context="return",
        scope_id=1,
        node=use_node,
        enclosing_stmt=use_stmt,
    )
    return VariableLifecycle(assignment=assignment, uses=[use])


def _lifecycle_no_node(rhs_source: str, var_name: str = "x") -> VariableLifecycle:
    """A lifecycle whose use has no real AST node attached (unknown context)."""
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assignment = AssignmentInfo(
        var_name=var_name,
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=rhs_source,
        scope_id=0,
        has_type_annotation=False,
    )
    return VariableLifecycle(
        assignment=assignment,
        uses=[UsageInfo(var_name=var_name, line=2, col=0, stmt_index=1, context="unknown", scope_id=0)],
    )


def _lifecycle_with_use_node(
    rhs_source: str, var_name: str = "x", use_stmt_source: str = "x.method()"
) -> VariableLifecycle:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assignment = AssignmentInfo(
        var_name=var_name,
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source=rhs_source,
        scope_id=0,
        has_type_annotation=False,
    )
    use_stmt = ast.parse(use_stmt_source).body[0]
    use_node = next(
        n for n in ast.walk(use_stmt) if isinstance(n, ast.Name) and n.id == var_name and isinstance(n.ctx, ast.Load)
    )
    return VariableLifecycle(
        assignment=assignment,
        uses=[
            UsageInfo(
                var_name=var_name,
                line=5,
                col=0,
                stmt_index=4,
                context="unknown",
                scope_id=0,
                node=use_node,
                enclosing_stmt=use_stmt,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Check metadata
# ---------------------------------------------------------------------------


def test_check_id_and_error_code() -> None:
    check = RedundantAssignmentCheck()
    assert check.check_id == "redundant-assignment"
    assert check.error_code == "TRI005"


def test_prefilter_pattern() -> None:
    assert RedundantAssignmentCheck().get_prefilter_pattern() == [" = "]


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
# fix() / apply_fixes(): file-mutation regression tests
# ---------------------------------------------------------------------------


def test_fix_method_with_fixable_violations(tmp_path: Path) -> None:
    source = """def func_scope():
    x = "foo"
    func(x=x)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert len(violations) >= 1
    assert any(v.fixable for v in violations)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()

    # The assignment should be removed and the usage inlined.
    assert "x = " not in fixed_content
    assert 'func(x="foo")' in fixed_content


def test_fix_two_assignments_used_on_the_same_line(tmp_path: Path) -> None:
    # Regression: two independently-fixable assignments whose single uses
    # land on the same line must both be inlined, even when the
    # replacement text is a different length than the variable it
    # replaces (which shifts the column of whichever use is processed
    # second).
    source = """def f():
    x = 1
    y = 22
    return y + x
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "x = " not in fixed_content
    assert "y = " not in fixed_content
    assert "return 22 + 1" in fixed_content


def test_fix_chained_assignment_where_use_line_is_another_assign_line(
    tmp_path: Path,
) -> None:
    # Regression: x's only use is on the same line as y's assignment (`y
    # = x`). Applying y's fix first blanks that whole line, so x's own fix
    # must skip cleanly instead of crashing when its use is gone.
    source = """def f():
    x = 1
    y = x
    return y
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "y = " not in fixed_content
    assert "return x" in fixed_content


def test_autofix_skips_violation_without_fix_data(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violation = ViolationFactory.build(
        check_id="redundant-assignment", error_code="TRI005", fixable=True, fix_data=None
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_skips_violation_with_invalid_fix_data(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    # fix_data is missing 'use_line'.
    violation = ViolationFactory.build(
        check_id="redundant-assignment", error_code="TRI005", fixable=True, fix_data={"other_key": "value"}
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_skips_multiline_rhs() -> None:
    # RHS with newline should not be inlined.
    source_lines = ["result = func(x)\n"]
    assert _can_safely_inline("result", "func(\n    arg\n)", 0, source_lines) is False


def test_autofix_skips_line_length_violation() -> None:
    # Current line is 80 chars, adding 20 more would exceed 88.
    source_lines = ["x = " + "a" * 80 + "\n"]
    assert _can_safely_inline("x", "a" * 20, 0, source_lines) is False


def test_autofix_skips_invalid_line_indices() -> None:
    source_lines = ["line1\n", "line2\n"]
    assert _can_safely_inline("x", "value", -1, source_lines) is False  # negative index
    assert _can_safely_inline("x", "value", 10, source_lines) is False  # out of bounds


def test_autofix_with_invalid_assignment_line(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violation = ViolationFactory.build(
        check_id="redundant-assignment",
        error_code="TRI005",
        fixable=True,
        fix_data={
            "pattern": "IMMEDIATE_SINGLE_USE",
            "assign_line": 100,  # Invalid line number
            "var_name": "x",
            "rhs_source": "1",
            "use_line": 2,
            "use_col": 6,
        },
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_with_invalid_usage_line(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violation = ViolationFactory.build(
        check_id="redundant-assignment",
        error_code="TRI005",
        fixable=True,
        fix_data={
            "pattern": "IMMEDIATE_SINGLE_USE",
            "assign_line": 1,
            "var_name": "x",
            "rhs_source": "1",
            "use_line": 100,  # Invalid line number
            "use_col": 6,
        },
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_with_multiple_uses(tmp_path: Path) -> None:
    source = "x = 1\nprint(x)\nprint(x)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    # RedundantAssignmentCheck.check() leaves use_line/use_col unset
    # whenever a lifecycle doesn't have exactly one use.
    violation = ViolationFactory.build(
        check_id="redundant-assignment",
        error_code="TRI005",
        fixable=True,
        fix_data={
            "pattern": "SINGLE_USE",
            "assign_line": 1,
            "var_name": "x",
            "rhs_source": "1",
            "use_line": None,
            "use_col": None,
        },
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_autofix_with_unsafe_inlining(tmp_path: Path) -> None:
    # Line is already 60 chars; adding a 40-char value would exceed 88.
    source = "x = " + "a" * 40 + "\nresult = some_long_function_name(x, param1, param2)\n"
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violation = ViolationFactory.build(
        check_id="redundant-assignment",
        error_code="TRI005",
        fixable=True,
        fix_data={
            "pattern": "IMMEDIATE_SINGLE_USE",
            "assign_line": 1,
            "var_name": "x",
            "rhs_source": "a" * 40,
            "use_line": 2,
            "use_col": 41,  # Position of 'x' in the usage line
        },
    )

    check = RedundantAssignmentCheck()
    assert check.fix(filepath, [violation], source, ast.parse(source)) is False


def test_fix_method_with_no_fixable_violations() -> None:
    source = """
x = "foo"
func(x=x)
"""
    violation = ViolationFactory.build(
        check_id="redundant-assignment", error_code="TRI005", fixable=False, fix_data=None
    )
    assert apply_fixes(Path("test.py"), [violation], source) is False


def test_autofix_simple_constant(tmp_path: Path) -> None:
    source = """def f():
    y = 42
    result = y + 10
    return result
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations
    assert check.fix(filepath, fixable_violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "y = 42" not in fixed_content
    assert "result = 42 + 10" in fixed_content


def test_autofix_simple_attribute(tmp_path: Path) -> None:
    source = """def f():
    v = obj.attr
    use(v)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations
    assert check.fix(filepath, fixable_violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "v = obj.attr" not in fixed_content
    assert "use(obj.attr)" in fixed_content


def test_autofix_word_boundaries(tmp_path: Path) -> None:
    source = """def f():
    x = 5
    result = max(x, 10)
    return result
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    fixable_violations = [v for v in violations if v.fixable]
    assert fixable_violations
    assert check.fix(filepath, fixable_violations, source, tree) is True

    fixed_content = filepath.read_text()
    # Should replace 'x' but not affect 'max'.
    assert "result = max(5, 10)" in fixed_content
    assert "max" in fixed_content


def test_autofix_handles_word_boundaries(tmp_path: Path) -> None:
    source = """
def func(index):
    x = 5
    return max(x, index)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)

    # Should only replace the standalone 'x', not 'max' or 'index'.
    assert "max(5, index)" in filepath.read_text()


def test_autofix_respects_line_length(tmp_path: Path) -> None:
    source = """
def func():
    some_result = "data"
    return some_result
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violations = RedundantAssignmentCheck().check(filepath, ast.parse(source), source)

    # Long variable names (>10 chars) are excluded from autofix as a
    # conservative proxy for lines that would grow too long when inlined.
    assert violations
    assert all(not v.fixable for v in violations)


def test_zero_arg_call_immediate_single_use_is_fixable(tmp_path: Path) -> None:
    # Regression test (issue #22): IMMEDIATE_SINGLE_USE never allowed a
    # Call RHS, even trivial zero-arg ones, so idiomatic test code like
    # `check = ForbidVarsCheck(); check.check(...)` was never auto-fixed.
    # A zero-arg call has no operands whose evaluation order inlining
    # could disturb, so it's safe to allow as a narrow carve-out.
    source = """def test_something():
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    check_violations = [v for v in violations if "'check'" in v.message]
    assert check_violations
    assert all(v.fixable for v in check_violations)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "check = " not in fixed_content
    assert "ForbidVarsCheck().check(" in fixed_content


def test_augmented_assignment_use_not_flagged_for_zero_arg_call(
    tmp_path: Path,
) -> None:
    # Regression test: the issue #22 zero-arg-call carve-out for
    # IMMEDIATE_SINGLE_USE must not make `x = Box(); x += 1` fixable —
    # inlining would produce invalid syntax (`Box() += 1`).
    source = """def f():
    x = Box()
    x += 1
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert all("'x'" not in v.message for v in violations)

    # Even if something slipped through and marked it fixable, fix() must
    # never corrupt the file.
    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


@pytest.mark.parametrize(
    ("source", "message_filter"),
    [
        (
            # A use inside a loop body isn't "the same execution point" as
            # the assignment — `value = make(); for _ in r: sink(value)`
            # runs make() once, but inlining would make it run once per
            # iteration.
            """def f(r):
    value = make()
    for _ in r:
        sink(value)
""",
            "'value'",
        ),
        (
            # A use inside a lambda body executes later (whenever the
            # lambda is called, if ever) — not once at the assignment
            # point. `x = make(); return lambda: x` must not become
            # `return lambda: make()`, which defers (and can repeat) the
            # call.
            """def f():
    x = make()
    return lambda: x
""",
            "'x'",
        ),
        (
            # An `await` is a suspension point where other code can run
            # and change state, so a use after one within the same
            # statement must be treated like a preceding call. `x =
            # make(); return sink(await future, x)` must not become
            # `sink(await future, make())`, which runs make() after the
            # await instead of before it.
            """async def f(future):
    x = make()
    return sink(await future, x)
""",
            "'x'",
        ),
        (
            # The pre-existing SINGLE_USE call allowance (args<=2) has the
            # exact same loop-repetition risk as the zero-arg carve-out.
            # `value = make(); other(); for _ in r: sink(value)` must not
            # become `for _ in r: sink(make())`, which runs make() N times
            # instead of once.
            """def f(r):
    value = make()
    other()
    for _ in r:
        sink(value)
""",
            "'value'",
        ),
        (
            # A dict literal's own AST field order (all keys, then all
            # values) doesn't match Python's real per-pair evaluation
            # order, so a naive evaluation-order walk would wrongly call
            # `x` in `{"a": side_effect(), x: 1}` safe — it isn't, since
            # "a": side_effect() runs as a pair before x is reached.
            """def f():
    x = make()
    d = {"a": side_effect(), x: 1}
""",
            "'x'",
        ),
        (
            # Binary/boolean/unary/compare operators can invoke arbitrary
            # user code via dunder overloads (__add__, __eq__, __bool__,
            # ...), so a sibling operator expression must count as a
            # preceding effect too. `x = make(); sink(a + b, x)` must not
            # become `sink(a + b, make())`.
            """def f():
    x = make()
    sink(a + b, x)
""",
            "'x'",
        ),
        (
            # A ternary's body/orelse are each conditional — never both
            # run, never unconditionally. `x = make(); sink(x if flag else
            # 0)` must not become `sink(make() if flag else 0)`.
            """def f():
    x = make()
    sink(x if flag else 0)
""",
            "'x'",
        ),
        (
            # `and`/`or` short-circuit, so a non-first operand may never
            # evaluate. `x = make(); sink(flag and x)` must not become
            # `sink(flag and make())`.
            """def f():
    x = make()
    sink(flag and x)
""",
            "'x'",
        ),
        (
            # Python evaluates an assignment's RHS *before* its target,
            # the opposite of ast.Assign's own field order. `x = make();
            # x.attr = side_effect()` must not become `make().attr =
            # side_effect()`.
            """def f():
    x = make()
    x.attr = side_effect()
""",
            "'x'",
        ),
    ],
    ids=[
        "loop-body",
        "lambda-body",
        "after-await",
        "single-use-call-in-loop-body",
        "dict-value-after-earlier-pair",
        "after-operator-sibling",
        "ternary-branch",
        "short-circuited-boolop",
        "assign-target-base",
    ],
)
def test_zero_arg_call_use_not_fixable(tmp_path: Path, source: str, message_filter: str) -> None:
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    matching = [v for v in violations if message_filter in v.message]
    assert matching
    assert all(not v.fixable for v in matching)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_single_use_call_in_loop_body_not_fixable(tmp_path: Path) -> None:
    source = """def f(r):
    value = make()
    other()
    for _ in r:
        sink(value)
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    value_violations = [v for v in violations if "'value'" in v.message]
    assert value_violations
    assert all(not v.fixable for v in value_violations)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_autofix_preserves_blank_lines_across_file(tmp_path: Path) -> None:
    # Regression: autofix used to delete blank lines across the entire
    # file, not just around the removed assignment.
    source = """class FirstClass:
    def method_one(self):
        pass


class SecondClass:
    def method_two(self):
        pass


def function_with_redundant_var():
    x = 42
    return x


def another_function():
    pass


class ThirdClass:
    def method_three(self):
        pass
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violations = check.check(filepath, tree, source)

    # This source always yields a fixable violation for `x`.
    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)
    fixed_content = filepath.read_text()

    assert "class FirstClass:\n    def method_one(self):\n        pass\n\n\nclass SecondClass:" in fixed_content
    assert (
        "class SecondClass:\n    def method_two(self):\n        pass\n\n\ndef function_with_redundant_var():"
        in fixed_content
    )
    assert "def another_function():\n    pass\n\n\nclass ThirdClass:" in fixed_content

    # Verify the fixed code is still valid Python; raises on failure.
    ast.parse(fixed_content)


def test_autofix_cleans_up_excessive_blank_lines(tmp_path: Path) -> None:
    source = """def function_with_redundant():


    x = 42


    return x
"""
    tree = ast.parse(source)
    check = RedundantAssignmentCheck()

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    violations = check.check(filepath, tree, source)

    # This source always yields a fixable violation for `x`.
    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)
    fixed_content = filepath.read_text()

    lines = fixed_content.split("\n")
    def_index = next(i for i, line in enumerate(lines) if "def function_with_redundant" in line)
    return_index = next(i for i in range(def_index, len(lines)) if "return" in lines[i])
    blanks_before_return = 0
    j = return_index - 1
    while j >= 0 and lines[j].strip() == "":
        blanks_before_return += 1
        j -= 1

    assert blanks_before_return <= 2

    # Verify the fixed code is still valid Python; raises on failure.
    ast.parse(fixed_content)


def test_cleanup_blank_lines_only_excess_below() -> None:
    # Branch coverage: blank_above <= 1 but blank_below > 1 (total >= 3).
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {0})
    assert lines[2] == ""
    assert lines[3] == "code\n"


def test_cleanup_blank_lines_only_excess_above() -> None:
    # Branch coverage: blank_above > 1 but blank_below <= 1 (total >= 3).
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {2})
    assert lines[0] == ""
    assert lines[3] == "code\n"


def test_fix_inlines_use_on_line_with_non_ascii_text(tmp_path: Path) -> None:
    # Regression: ast.col_offset is a UTF-8 byte offset, not a character
    # offset. A non-ASCII character earlier on the use's line must not
    # throw off the position used to locate the variable for inlining.
    source = """def process():
    data = calc()
    x = "café"; return data
"""
    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = RedundantAssignmentCheck()
    violations = check.check(filepath, tree, source)

    assert any(v.fixable for v in violations)
    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "data" not in fixed_content
    assert "return calc()" in fixed_content


# ---------------------------------------------------------------------------
# VariableTracker / lifecycle building
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "var_name", "count"),
    [
        (
            """
def outer():
    x = "outer"
    def inner():
        x = "inner"
        return x
    return x
""",
            "x",
            2,
        ),
        (
            """
def example():
    x = "first"
    print(x)
    x = "second"
    print(x)
""",
            "x",
            2,
        ),
        (
            """
def example():
    x: str = "first"
    print(x)
    x: str = "second"
    print(x)
""",
            "x",
            2,
        ),
    ],
    ids=["scope-isolation", "multiple-plain-assignments", "multiple-annotated-assignments"],
)
def test_lifecycle_count_for_variable(source: str, var_name: str, count: int) -> None:
    assert _lifecycle_count(source, var_name) == count


def test_self_referential_assignment_correctly_tracked() -> None:
    source = """
def example():
    x = 1
    x = x + 1
    print(x)
    return x
"""
    # Second assignment (x = x + 1) has two uses (print and return). First
    # assignment (x = 1) has one use (x + 1 RHS). Neither should be
    # flagged as redundant because both have multiple uses.
    assert _check(source) == []


def test_augmented_assignment_tracks_usage() -> None:
    source = """
def example():
    x = 1
    x += 2
    print(x)
"""
    lifecycle = _lifecycle_for(source, "x")
    # Two uses: the read in `x += 2` (augmented assignment) and the use
    # in `print(x)`.
    assert len(lifecycle.uses) == 2


def test_repeated_augmented_assignment_reuses_existing_uses_key() -> None:
    # Branch coverage: a second augmented assignment to the same variable
    # in the same scope appends to the existing self.uses[key] list
    # rather than recreating it.
    source = """
def example():
    x = 0
    x += 1
    x += 2
"""
    lifecycle = _lifecycle_for(source, "x")
    # Each `x += n` counts as one use (the implicit read).
    assert len(lifecycle.uses) == 2


def test_decorator_use_is_tracked_by_variable_tracker() -> None:
    source = """
def outer():
    app = make_app()

    @app.route("/")
    def index():
        pass

    return app
"""
    lifecycle = _lifecycle_for(source, "app")
    # Two uses: @app.route("/") and return app.
    assert len(lifecycle.uses) == 2


def test_class_decorator_use_is_tracked() -> None:
    source = """
def factory():
    validator = build_validator()

    @validator.register
    class Rule:
        pass

    return validator
"""
    lifecycle = _lifecycle_for(source, "validator")
    # Two uses: @validator.register (decorator) and return validator.
    assert len(lifecycle.uses) == 2


def test_track_attribute_assignment_with_non_name_base() -> None:
    # Branch coverage: when the target of an assignment is something like
    # ``func().attr = v`` (a method-call result), unwinding the Attribute
    # chain leads to a Call node, not a Name.
    # _track_attribute_or_subscript_base_usage must skip tracking rather
    # than crashing.
    source = """
def outer():
    get_obj().attr = "value"
    return 42
"""
    # Must not raise; call-result targets are silently skipped.
    VariableTracker(source).visit(ast.parse(source))


def test_track_attribute_assignment_key_already_in_uses() -> None:
    # Branch coverage: when the same variable is the base of two separate
    # attribute assignments (``obj.x = 1`` then ``obj.y = 2``), the second
    # call to _track_attribute_or_subscript_base_usage finds the key
    # already present in self.uses and must append rather than create a
    # new list.
    source = """
def outer():
    obj = make_obj()
    obj.x = 1
    obj.y = 2
    return obj
"""
    lifecycle = _lifecycle_for(source, "obj")
    # obj is used in: obj.x = 1, obj.y = 2, return obj -> 3 uses.
    assert len(lifecycle.uses) == 3


def test_in_comprehension_flag_set_correctly() -> None:
    source = """
def func(obj, items):
    cached = obj.attr
    result = [x for x in items if x == cached]
    return result
"""
    lifecycle = _lifecycle_for(source, "cached")
    assert len(lifecycle.uses) == 1
    assert lifecycle.uses[0].in_comprehension is True


def test_in_comprehension_flag_false_for_normal_usage() -> None:
    source = """
def func():
    x = "foo"
    print(x)
"""
    lifecycle = _lifecycle_for(source, "x")
    assert all(not use.in_comprehension for use in lifecycle.uses)


def test_variable_tracker_scope_isolation() -> None:
    assert (
        _lifecycle_count(
            """
def outer():
    x = "outer"
    def inner():
        x = "inner"
        return x
    return x
""",
            "x",
        )
        == 2
    )


def test_get_source_segment_error_handling() -> None:
    node = ast.Constant(value=1, lineno=-1, col_offset=-1)
    assert VariableTracker("x = 1")._get_source_segment(node) == ""


# ---------------------------------------------------------------------------
# detect_redundancy() / lifecycle properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "var_name", "pattern"),
    [
        (
            """
def func():
    x = "foo"
    print(x)
""",
            "x",
            PatternType.IMMEDIATE_SINGLE_USE,
        ),
        (
            """
def func():
    x = "foo"
    y = "bar"
    z = "baz"
    print(x)
""",
            "x",
            PatternType.SINGLE_USE,
        ),
        (
            # An augmented-assignment target (`x += 1`) can't be inlined —
            # the result (`5 += 1`) is invalid syntax — and isn't the
            # read-then-forward pattern TRI005 targets anyway.
            """
def func():
    x = 5
    x += 1
""",
            "x",
            None,
        ),
    ],
    ids=["immediate-use", "single-use-with-intervening-statements", "augmented-assignment-is-not-redundant"],
)
def test_detect_redundancy(source: str, var_name: str, pattern: PatternType | None) -> None:
    assert detect_redundancy(_lifecycle_for(source, var_name)) == pattern


def test_match_statement_case_body_use_not_immediate() -> None:
    # A use inside a match/case body must be treated as control flow (like
    # an if/elif branch), not as an ordinary use that always runs —
    # otherwise it could be reported/autofixed as if the case always
    # matched.
    source = """
def f(command):
    value = make()
    match command:
        case "go":
            sink(value)
"""
    assert all("'value'" not in v.message for v in _check(source))


def test_lifecycle_no_uses_not_immediate() -> None:
    rhs_node = ast.parse("func()", mode="eval").body
    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=rhs_node,
        rhs_source="func()",
        scope_id=0,
        has_type_annotation=False,
    )
    lifecycle = VariableLifecycle(assignment=assignment, uses=[])

    assert lifecycle.is_immediate_use is False
    assert lifecycle.is_single_use is False


def test_lifecycle_is_immediate_use_with_closure() -> None:
    assignment = AssignmentInfo(
        var_name="x",
        line=1,
        col=0,
        stmt_index=0,
        rhs_node=ast.parse("1", mode="eval").body,
        rhs_source="1",
        scope_id=1,  # Outer scope
        has_type_annotation=False,
    )
    usage = UsageInfo(
        var_name="x",
        line=3,
        col=0,
        stmt_index=1,  # Would normally be considered immediate
        context="unknown",
        scope_id=2,  # Nested scope (closure)
    )
    lifecycle = VariableLifecycle(assignment=assignment, uses=[usage])

    # Even though stmt_index suggests immediate use, it should return
    # False because the use is in a different scope (closure).
    assert lifecycle.is_immediate_use is False
    assert lifecycle.is_single_use is True


def test_evaluation_order_children_assign_yields_value_before_targets() -> None:
    # Branch coverage + contract test: for ast.Assign,
    # _evaluation_order_children must yield the RHS value before the
    # target(s) — the opposite of Assign._fields, which lists targets
    # first — matching Python's real evaluate-RHS-then-target(s) order.
    tree = ast.parse("x.attr = value_expr")
    assign_node = tree.body[0]
    assert isinstance(assign_node, ast.Assign)
    children = list(_evaluation_order_children(assign_node))

    assert children == [(assign_node.value, False), (assign_node.targets[0], False)]


# ---------------------------------------------------------------------------
# should_autofix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rhs_source", "var_name", "pattern", "expected"),
    [
        # "unknown" context, no real use node attached.
        ("get_value()", "x", PatternType.IMMEDIATE_SINGLE_USE, False),
        ("func(1, 2)", "x", PatternType.IMMEDIATE_SINGLE_USE, False),
        ("func()", "x", PatternType.IMMEDIATE_SINGLE_USE, False),
        ("func({k: v for k, v in items})", "x", PatternType.IMMEDIATE_SINGLE_USE, False),
        # "has_" prefix scores +50, well above the semantic-score cutoff.
        ("check()", "has_something", PatternType.SINGLE_USE, False),
    ],
    ids=["simple-call", "call-with-simple-args", "no-args-call", "complex-call-args", "high-semantic-score"],
)
def test_should_autofix_no_node(rhs_source: str, var_name: str, pattern: PatternType, *, expected: bool) -> None:
    lifecycle = _lifecycle_no_node(rhs_source, var_name)
    assert should_autofix(lifecycle, pattern) is expected


@pytest.mark.parametrize(
    ("rhs_source", "expected"),
    [
        ("obj.attr", True),
        ("func(key=value)", True),
        ("func(a, b, c)", False),  # Exceeds the 2-arg limit for SINGLE_USE.
    ],
    ids=["attribute", "keywords", "complex-call-rejected"],
)
def test_should_autofix_single_use_with_real_node(rhs_source: str, *, expected: bool) -> None:
    lifecycle = _lifecycle_with_use_node(rhs_source)
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is expected


def test_should_autofix_with_single_use_pattern() -> None:
    # SINGLE_USE pattern CAN be auto-fixed for simple cases (simple call
    # with no args).
    lifecycle = _lifecycle_with_use_node("get_value()")
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is True


def test_should_autofix_returns_false_for_loop_assignment() -> None:
    rhs_node = ast.parse('"foo"', mode="eval").body
    lifecycle = _make_single_use_lifecycle('"foo"', rhs_node, in_loop=True)
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_should_autofix_returns_false_for_multiline_rhs() -> None:
    rhs_node = ast.parse('"foo"', mode="eval").body
    lifecycle = _make_single_use_lifecycle('"foo"\n"bar"', rhs_node)
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_should_autofix_returns_false_for_long_var_name_immediate() -> None:
    # Uses 'myvariablex' (11 chars, no underscores) with a 10-char Name
    # RHS to keep semantic_score=0 — the name/rhs length ratio stays below
    # 1.1 so no score is added from the ratio check. The code therefore
    # reaches the len(var_name) > 10 guard rather than returning early at
    # the semantic_score > 10 check. This guard applies only to
    # IMMEDIATE_SINGLE_USE / LITERAL_IDENTITY patterns.
    rhs_node = ast.parse("something1", mode="eval").body
    lifecycle = _make_single_use_lifecycle("something1", rhs_node, var_name="myvariablex")
    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is False


def test_should_autofix_returns_true_for_single_use_constant_rhs() -> None:
    rhs_node = ast.parse("42", mode="eval").body
    lifecycle = _make_single_use_lifecycle("42", rhs_node, var_name="x")
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is True


def test_should_autofix_returns_false_for_non_call_non_attr_rhs_single_use() -> None:
    # A list literal falls through all isinstance checks in the
    # SINGLE_USE block and reaches the final ``return False``.
    rhs_node = ast.parse("[1, 2, 3]", mode="eval").body
    lifecycle = _make_single_use_lifecycle("[1, 2, 3]", rhs_node)
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is False


def test_should_autofix_allows_zero_arg_call_for_immediate_single_use() -> None:
    # Issue #22 gap 2: IMMEDIATE_SINGLE_USE previously excluded every Call
    # RHS, even trivial zero-arg ones like `check = ForbidVarsCheck()`. A
    # zero-arg call with nothing else evaluating before its use (within
    # the use's statement) has no sibling operand whose order inlining
    # could disturb, so it gets a narrow carve-out here.
    rhs_node = ast.parse("ForbidVarsCheck()", mode="eval").body
    lifecycle = _make_single_use_lifecycle("ForbidVarsCheck()", rhs_node, var_name="check", preceded_by_call=False)
    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is True


def test_should_autofix_rejects_zero_arg_call_preceded_by_a_call() -> None:
    # Regression test (P1 caught in review of issue #22's fix): a
    # zero-arg call must not be inlined when a sibling expression
    # evaluates before it within the same statement, or inlining reverses
    # the original execution order. Example: `value = next_value();
    # sink(side_effect(), value)` must not become `sink(side_effect(),
    # next_value())` — that runs next_value() after side_effect() instead
    # of before it.
    rhs_node = ast.parse("next_value()", mode="eval").body
    lifecycle = _make_single_use_lifecycle("next_value()", rhs_node, var_name="value", preceded_by_call=True)
    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is False


def test_should_autofix_rejects_call_with_args_for_immediate_single_use() -> None:
    # The zero-arg carve-out must stay narrow: a call with any argument is
    # still rejected for IMMEDIATE_SINGLE_USE/LITERAL_IDENTITY, unlike the
    # more permissive allowance already granted to SINGLE_USE.
    rhs_node = ast.parse("make_check(1)", mode="eval").body
    lifecycle = _make_single_use_lifecycle("make_check(1)", rhs_node, var_name="check")
    assert should_autofix(lifecycle, PatternType.IMMEDIATE_SINGLE_USE) is False


def test_should_autofix_uses_real_use_line_length_when_available() -> None:
    # Issue #22 gap 1: should_autofix's line-length check must reflect the
    # *actual* use line when the caller can supply it, not just the
    # conservative RHS/var-name-based estimate — otherwise a violation can
    # be reported [FIXABLE] and then silently skipped by apply_fixes' own,
    # accurate length check.
    rhs_node = ast.parse("ast.parse(source)", mode="eval").body
    lifecycle = _make_single_use_lifecycle("ast.parse(source)", rhs_node, var_name="tree")

    # Without the real use line, the conservative RHS/var-name estimate
    # says inlining is safe (both are short).
    assert should_autofix(lifecycle, PatternType.SINGLE_USE) is True

    # _make_single_use_lifecycle fixes the use at line 2 (1-indexed).
    long_use_line = '    violations = check.check(Path("tests/test_something_with_a_long_name.py"), tree, source)'
    source_lines = ["def f():", long_use_line]
    assert should_autofix(lifecycle, PatternType.SINGLE_USE, source_lines=source_lines) is False


# ---------------------------------------------------------------------------
# is_preceded_by_call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "var_name", "expected"),
    [
        (
            # Regression (2nd P1 in issue #22's fix): the evaluation-order
            # check must be AST-based, not line/column-text-based — a
            # text heuristic sees an empty same-line prefix for `x` here
            # and wrongly calls it safe, even though side_effect() (on the
            # previous physical line, same statement) already ran first.
            """
def f():
    x = make()
    sink(
        side_effect(),
        x,
    )
""",
            "x",
            True,
        ),
        (
            # Attribute/subscript access (e.g. a @property getter) can
            # run arbitrary code just like a call, so a sibling attribute
            # access must count as "preceding" too.
            """
def f():
    value = make()
    sink(obj.property, value)
""",
            "value",
            True,
        ),
        (
            # ast.Dict's own _fields order is ('keys', 'values') — every
            # key, then every value — which does NOT match Python's real
            # per-pair evaluation order.
            """
def f():
    x = make()
    d = {"a": side_effect(), x: 1}
""",
            "x",
            True,
        ),
        (
            # Branch coverage: a dict literal that doesn't contain the
            # target at all (and has no calls in it) must be walked fully.
            """
def f():
    x = make()
    sink({"a": 1, "b": 2}, x)
""",
            "x",
            False,
        ),
        (
            # x as the very first key (nothing evaluates before it, not
            # even its own paired value) is still safe.
            """
def f():
    x = make()
    d = {x: 1, "b": side_effect()}
""",
            "x",
            False,
        ),
        (
            # Branch coverage: a None key marks **unpacking (evaluates
            # only the paired value) — a value after one must still see it
            # as a preceding effect if that unpacked expression is a call.
            """
def f():
    x = make()
    d = {**other(), "b": x}
""",
            "x",
            True,
        ),
        (
            # Python evaluates `obj.attr = value` by computing `value`
            # *before* `obj` — the opposite of ast.Assign's own _fields
            # order.
            """
def f():
    x = make()
    x.attr = side_effect()
""",
            "x",
            True,
        ),
        (
            # Exactly one of a ternary's body/orelse ever runs — a call
            # used there might not execute at all.
            """
def f():
    x = make()
    sink(x if flag else 0)
""",
            "x",
            True,
        ),
        (
            # `and`/`or` short-circuit, so only the first operand is
            # guaranteed to evaluate.
            """
def f():
    x = make()
    sink(flag and x)
""",
            "x",
            True,
        ),
        (
            # Branch coverage: a ternary that doesn't contain the target
            # at all must still be walked fully — and since IfExp's `test`
            # invokes `__bool__`, it's still a preceding effect.
            """
def f():
    x = make()
    sink(a if flag else b, x)
""",
            "x",
            True,
        ),
        (
            # Branch coverage: a BoolOp that doesn't contain the target at
            # all must still be walked fully — and since BoolOp invokes
            # `__bool__` on its left operand, it's still a preceding
            # effect.
            """
def f():
    x = make()
    sink(flag and other, x)
""",
            "x",
            True,
        ),
        (
            # The BoolOp fix must stay precise: the *first* operand always
            # evaluates unconditionally, so `sink(x and flag)` is safe.
            """
def f():
    x = make()
    sink(x and flag)
""",
            "x",
            False,
        ),
        (
            # The issue's own motivating idiom must remain safe: `check`
            # is the receiver of `check.check(...)`, evaluated before any
            # of that call's own arguments — nothing precedes it.
            """
def f():
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)
""",
            "check",
            False,
        ),
    ],
    ids=[
        "multiline-statement",
        "attribute-sibling",
        "dict-key-after-earlier-pair",
        "dict-sibling-without-calls",
        "dict-first-key",
        "dict-value-after-unpacking",
        "assign-target-base-after-value",
        "ifexp-branch",
        "boolop-non-first-operand",
        "ifexp-sibling-without-target",
        "boolop-sibling-without-target",
        "boolop-first-operand",
        "method-call-receiver",
    ],
)
def test_is_preceded_by_call(source: str, var_name: str, *, expected: bool) -> None:
    lifecycle = _lifecycle_for(source, var_name)
    assert is_preceded_by_call(lifecycle.uses[0]) is expected


def test_is_preceded_by_call_defaults_to_true_for_unknown_container() -> None:
    # When the enclosing statement (or node) can't be determined,
    # is_preceded_by_call must default to the conservative "unsafe" answer
    # rather than guessing.
    use = UsageInfo(var_name="x", line=1, col=0, stmt_index=0, context="unknown", scope_id=1)
    assert is_preceded_by_call(use) is True


# ---------------------------------------------------------------------------
# calculate_semantic_value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "minimum"),
    [
        ("x", "a + b", 15),  # BinOp adds 15.
        ("x", "1 if c else 0", 20),  # IfExp adds 20.
        ("has_permission", "check_something()", 50),  # has_ prefix.
        ("item_count", "len(items)", 40),  # descriptive suffix.
        ("result", "[x for x in items]", 30),  # list comprehension.
        ("result", "-value", 10),  # unary op.
        ("func", "lambda x: x * 2", 25),  # lambda.
        ("x", "a" * 85, 35),  # very long expression (80+ chars).
        ("x", "a" * 65, 25),  # long expression (60+ chars).
        ("x", "some_function_with_exactly_45_characters()", 10),  # medium length (40-60 chars).
        # Multipart name bonus in isolation: identical RHS, only the name
        # differs, and the multipart name scores strictly higher.
        ("user_email_address", "get_email()", 30),
    ],
    ids=[
        "binop",
        "ifexp",
        "descriptive-boolean-prefix",
        "descriptive-suffix",
        "list-comprehension",
        "unary-operation",
        "lambda-expression",
        "very-long-expression",
        "long-expression-60-plus",
        "medium-length-expression",
        "multipart-name",
    ],
)
def test_calculate_semantic_value_at_least(var_name: str, rhs_source: str, minimum: int) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert calculate_semantic_value(var_name, rhs_source, rhs_node, has_type_annotation=False) >= minimum


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "expected"),
    [
        ("result", "obj[x][y]", 20),  # 2 chains (+20).
        ("my_value", "func()[x][y]", 40),  # 3+ chains (+30) + 2-part name (+10).
        # Name moderately longer than the RHS (ratio between 1.1x and
        # 1.3x) scores +5, distinct from the +15 given to a name that's
        # significantly (>1.3x) longer.
        ("another", '"test"', 5),
    ],
    ids=["two-subscript-chains", "three-plus-chains-with-multipart-name", "name-moderately-longer-than-rhs"],
)
def test_calculate_semantic_value_exact(var_name: str, rhs_source: str, expected: int) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert calculate_semantic_value(var_name, rhs_source, rhs_node, has_type_annotation=False) == expected


def test_calculate_semantic_value_chained_attributes() -> None:
    rhs_source = "obj.foo.bar"
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert calculate_semantic_value("result", rhs_source, rhs_node, has_type_annotation=False) >= 20


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "minimum"),
    [
        # Rule 10 intercepts variables used solely inside comprehensions
        # before they reach calculate_semantic_value, so these need a
        # direct test: multi-part name (+30) + "some" in
        # test_semantic_words (+25) + list bonus (+25).
        ("some_european_airports", '["AES", "BYJ", "BTS"]', 25),
        ("my_mapping", '{"key": "value"}', 25),
        # multi-part name (+30) + no test_semantic_words match (+0) +
        # range bonus (+25).
        ("days_with_routes_in_a_row", "range(70)", 25),
        # Covers the False branch of the test_semantic_words check: no
        # semantic test words present.
        ("flight_count", "42", 0),
    ],
    ids=[
        "test-context-list-literal",
        "test-context-dict-literal",
        "test-context-range-call",
        "test-context-no-semantic-word",
    ],
)
def test_calculate_semantic_value_test_context(var_name: str, rhs_source: str, minimum: int) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert (
        calculate_semantic_value(var_name, rhs_source, rhs_node, has_type_annotation=False, is_test_context=True)
        >= minimum
    )


# ---------------------------------------------------------------------------
# _adds_verbosity_or_context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "expected"),
    [
        ("raw_data", "fetch_data()", True),  # Descriptive prefix.
        ("raw_headers", 'kwargs.get("headers")', True),  # Var contains RHS key, more verbose.
        ("user_email", 'data.get("email")', True),  # .get() with more context.
        ("translations", "orjson.loads(data)", True),  # Generic parse func, descriptive name.
        ("user_config", "json.load(f)", True),  # Generic parse func, multi-part name.
        ("data", "json.loads(data)", False),  # Parse func but generic variable name.
        ("configuration", "loads(data)", True),  # Parse function as a bare Name node.
        ("x", "42", False),  # No verbosity added.
        # Branch coverage: Subscript RHS with a variable (non-constant)
        # slice — rhs_key_or_method stays None.
        ("user_obj", "obj[key]", False),
        # Branch coverage: Call RHS where func is a Subscript, not
        # Name/Attribute.
        ("configuration", 'funcs["load"](data)', False),
        # Branch coverage: Pattern 3 (.get() call) where the key is not in
        # the var name.
        ("x", 'data.get("email")', False),
        # Branch coverage: Pattern 4 parse func where func is a Subscript.
        ("parsed_data", 'parsers["json"](data)', True),
        # Branch coverage: Pattern 4 parse func but var name is generic
        # (in generic_names).
        ("result", "json.loads(data)", False),
    ],
    ids=[
        "descriptive-prefix",
        "contains-rhs-key-more-verbose",
        "get-call-with-context",
        "generic-parse-descriptive-name",
        "generic-parse-multipart-name",
        "generic-parse-generic-name",
        "parse-function-as-name",
        "no-verbosity",
        "subscript-with-variable-slice",
        "call-with-subscript-func",
        "get-call-key-not-in-var",
        "parse-func-with-subscript-func",
        "parse-func-with-generic-var-name",
    ],
)
def test_adds_verbosity_or_context(var_name: str, rhs_source: str, *, expected: bool) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert _adds_verbosity_or_context(var_name, rhs_source, rhs_node) is expected


# ---------------------------------------------------------------------------
# _would_require_parentheses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rhs_source", "expected"),
    [
        ("len(x) + 1", True),
        ("a and b", True),
        ("x == y", True),
        ("len(x)", False),
    ],
    ids=["binop", "boolop", "compare", "simple-call"],
)
def test_would_require_parentheses(rhs_source: str, *, expected: bool) -> None:
    rhs_node = ast.parse(rhs_source, mode="eval").body
    assert _would_require_parentheses(rhs_node) is expected


# ---------------------------------------------------------------------------
# _is_named_constant_pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var_name", "rhs_source", "expected"),
    [
        ("max_depth", "10", True),  # Multi-part name and number.
        ("line_spacing", "1.2", True),  # Float.
        ("threshold", "42", True),  # Single-part long name.
        ("value", "10", False),  # Single-part short generic name.
        ("num", "10", False),
        ("msg", '"hello"', False),  # Non-numeric.
    ],
    ids=["multipart-int", "float", "single-part-long-name", "generic-value", "generic-num", "non-numeric"],
)
def test_is_named_constant_pattern(var_name: str, rhs_source: str, *, expected: bool) -> None:
    node = ast.parse(rhs_source, mode="eval").body
    assert _is_named_constant_pattern(var_name, node) is expected


# ---------------------------------------------------------------------------
# _has_inline_comment / _has_comment_above
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("x = value  # this is a comment", True),
        ("x = value", False),
        ('x = "hello # world"', False),  # `#` inside a string, not a comment.
        ('x = "foo"  # comment', True),  # `#` in both string and as a comment.
        ('x = "test#test"  # real comment', True),  # String with `#` followed by a real comment.
        ('x = "test # not a comment"', False),  # Only a string containing `#`.
        ('x = ""  # comment', True),  # Empty string then comment.
        # Regression: a single-quote inside a double-quoted string (e.g.
        # "it's") must not be mistaken for a comment delimiter.
        ('x = "it\'s fine"', False),
        ('x = "it\'s fine"  # comment', True),
    ],
    ids=[
        "with-comment",
        "without-comment",
        "hash-inside-string",
        "hash-in-string-and-comment",
        "hash-in-string-then-real-comment",
        "only-string-with-hash",
        "empty-string-then-comment",
        "mismatched-quote-in-string",
        "mismatched-quote-with-real-comment",
    ],
)
def test_has_inline_comment(line: str, *, expected: bool) -> None:
    assert _has_inline_comment(1, [line]) is expected


def test_has_inline_comment_out_of_bounds() -> None:
    lines = ["x = value"]
    assert _has_inline_comment(0, lines) is False
    assert _has_inline_comment(5, lines) is False


def test_has_comment_above_first_line_returns_false() -> None:
    # Branch coverage: an assignment on line 1 has no line above to check.
    lines = ['x = "foo"', "process(x)"]
    assert _has_comment_above(1, lines) is False


# ---------------------------------------------------------------------------
# _is_test_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (Path("tests/test_something.py"), True),
        (Path("tests/utils/test_helpers.py"), True),
        (Path("test/test_foo.py"), True),
        (Path("test_example.py"), True),
        (Path("src/test_module.py"), True),
        (Path("example_test.py"), True),
        (Path("src/module_test.py"), True),
        (Path("src/module.py"), False),
        (Path("main.py"), False),
        (Path("setup.py"), False),
        (None, False),
    ],
    ids=[
        "tests-directory",
        "nested-tests-directory",
        "singular-test-directory",
        "test-prefix",
        "test-prefix-nested",
        "test-suffix",
        "test-suffix-nested",
        "plain-module",
        "main",
        "setup",
        "none",
    ],
)
def test_is_test_file(path: Path | None, *, expected: bool) -> None:
    assert _is_test_file(path) is expected


# ---------------------------------------------------------------------------
# _contains_nondeterministic_call
# ---------------------------------------------------------------------------


def test_contains_nondeterministic_call_with_subscript_func() -> None:
    # Branch coverage: when the called function is accessed via subscript
    # (e.g. ``funcs[0]()``), ``node.func`` is neither Name nor Attribute.
    # The detector must continue visiting child nodes rather than crashing
    # or silently skipping.
    rhs_node = ast.parse("funcs[0]()", mode="eval").body
    assert _contains_nondeterministic_call(rhs_node) is False


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
