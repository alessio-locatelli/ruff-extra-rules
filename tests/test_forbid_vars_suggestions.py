from __future__ import annotations

import ast
from typing import cast

import pytest

from pre_commit_hooks.ast_checks._forbid_vars_suggestions import (
    Confidence,
    RenameProposal,
    _annotation_constraints,
    _annotation_name,
    _comprehension_name,
    _Index,
    _is_command_shape,
    _is_len_call,
    _is_parser_argument,
    _is_public_attribute,
    _is_subprocess_argument,
    _pluralize,
    _refine_parser_candidates,
    _singularize,
    _to_snake_case,
    plan_suggestions,
)


def _plans(source: str, ignored_lines: set[int] | None = None) -> dict[tuple[int, int], RenameProposal]:
    return plan_suggestions(ast.parse(source), {"data", "result"}, ignored_lines or set())


def _plan_for(source: str, target: str = "data") -> RenameProposal | None:
    tree = ast.parse(source)
    target_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == target
    )
    return _plans(source).get((target_node.lineno, target_node.col_offset))


def _assert_plan_for(
    source: str,
    target: str,
    expected_name: str | None,
    expected_confidence: Confidence | None,
) -> None:
    proposal = _plan_for(source, target)

    assert (proposal is None) is (expected_name is None)
    if proposal is not None:
        assert proposal.name == expected_name
        assert proposal.confidence is expected_confidence


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("def f():\n    data: User = make()\n", "user"),
        ("def f():\n    data: HTTPResponse = make()\n", "http_response"),
        ("def f():\n    data: list[User] = make()\n", "users"),
        ("def f():\n    data: set[Entry] = make()\n", "entries"),
        ("def f():\n    data: frozenset[Match] = make()\n", "matches"),
        ("def f():\n    data: tuple[User, ...] = make()\n", "users"),
    ],
)
def test_explicit_annotations_produce_autofixable_names(source: str, name: str) -> None:
    _assert_plan_for(source, "data", name, Confidence.AUTO_FIX)


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    data: dict[str, User] = make()\n",
        "def f():\n    data: tuple[User, Order] = make()\n",
        "def f():\n    data: list[Person] = make()\n",
        "def f():\n    data: Any = make()\n",
    ],
)
def test_ambiguous_or_irregular_annotations_do_not_produce_names(source: str) -> None:
    _assert_plan_for(source, "data", None, None)


@pytest.mark.parametrize(
    "source",
    [
        "from typing import Dict\ndef f():\n    data: Dict[str, int] = make()\n",
        "from typing import Tuple\ndef f():\n    data: Tuple[str, ...] = make()\n",
        "from typing import List\ndef f():\n    data: List[str] = make()\n",
        "from typing import Set\ndef f():\n    data: Set[str] = make()\n",
        "from typing import Dict\ndef f():\n    data: Dict = make()\n",
        "def f(obj):\n    data = obj.type\n    return data\n",
        "def f():\n    data = get_id()\n    return data\n",
    ],
    ids=[
        "typing-dict-subscripted",
        "typing-tuple-subscripted",
        "typing-list-subscripted",
        "typing-set-subscripted",
        "typing-dict-bare",
        "attribute-shadows-type-builtin",
        "producer-prefix-shadows-id-builtin",
    ],
)
def test_builtin_shadowing_candidates_never_produce_names(source: str) -> None:
    _assert_plan_for(source, "data", None, None)


@pytest.mark.parametrize(
    ("source", "name", "confidence"),
    [
        ("def f():\n    result = get_user()\n", "user", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = fetch_order()\n", "order", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = load_config()\n", "config", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = read_manifest()\n", "manifest", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = parse_document()\n", "document", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = create_session()\n", "session", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = build_index()\n", "index", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = make_client()\n", "client", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = find_match()\n", "match", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = User()\n", "user", Confidence.SUGGESTION_ONLY),
        ("def f():\n    result = record.value\n", "value", Confidence.SUGGESTION_ONLY),
    ],
)
def test_producer_candidates_are_suggestion_only(source: str, name: str, confidence: Confidence) -> None:
    _assert_plan_for(source, "result", name, confidence)


@pytest.mark.parametrize(
    ("source", "target", "name"),
    [
        (
            "import requests\n\ndef f(url):\n    result = requests.get(url)\n    return result.status_code\n",
            "result",
            "response",
        ),
        (
            "from httpx import get\n\ndef f(url):\n    result = get(url)\n    return result.headers\n",
            "result",
            "response",
        ),
        (
            "import subprocess\n\ndef f():\n"
            '    result = subprocess.run(["git", "status"])\n'
            "    return result.returncode\n",
            "result",
            "completed_process",
        ),
        (
            'import re\n\ndef f():\n    result = re.search("x", "x")\n    return result.group()\n',
            "result",
            "match",
        ),
        (
            "from pathlib import Path\n\ndef f(path):\n    result = Path(path).open()\n    return result.read()\n",
            "result",
            "file_handle",
        ),
        (
            "def f(path):\n    result = open(path)\n    return result.read()\n",
            "result",
            "file_handle",
        ),
    ],
)
def test_registry_and_access_evidence_produce_autofixes(source: str, target: str, name: str) -> None:
    _assert_plan_for(source, target, name, Confidence.AUTO_FIX)


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ('import re\n\ndef f():\n    result = re.findall("x", "x")\n', "matches"),
        (
            "from urllib.request import urlopen\n\ndef f(url):\n    result = urlopen(url)\n    return result.read()\n",
            "response",
        ),
        (
            "import urllib.request\n\ndef f(url):\n"
            "    result = urllib.request.urlopen(url)\n"
            "    return result.read()\n",
            "response",
        ),
    ],
)
def test_registry_forms_without_extra_evidence_remain_suggestions(source: str, name: str) -> None:
    _assert_plan_for(source, "result", name, Confidence.SUGGESTION_ONLY)


@pytest.mark.parametrize(
    "source",
    [
        "def f(url):\n    result = requests.get(url)\n",
        'def f():\n    result = re.search("x", "x")\n',
        "def f(path):\n    open = lambda value: value\n    result = open(path)\n",
        "def f():\n    result = unknown.get()\n",
    ],
)
def test_unresolved_or_shadowed_apis_do_not_produce_names(source: str) -> None:
    _assert_plan_for(source, "result", None, None)


@pytest.mark.parametrize(
    ("source", "target", "expected_name", "expected_confidence"),
    [
        (
            "import requests\n\ndef f(url):\n    data = requests.get(url).json()\n",
            "data",
            "payload",
            Confidence.SUGGESTION_ONLY,
        ),
        (
            "import json\n\ndef f(path):\n    data: bytes = read_bytes(path)\n    return json.loads(data)\n",
            "data",
            None,
            None,
        ),
        (
            'def f():\n    data: Payload = get_payload()\n    return locals()["data"]\n',
            "data",
            "payload",
            Confidence.SUGGESTION_ONLY,
        ),
        (
            "def get_user() -> User:\n"
            "    raise NotImplementedError\n\n"
            "def f():\n"
            "    result = get_user()\n"
            "    return result\n",
            "result",
            "user",
            Confidence.AUTO_FIX,
        ),
        (
            "from urllib import request\n\ndef f(url):\n    result = request.urlopen(url)\n    return result.read()\n",
            "result",
            "response",
            Confidence.SUGGESTION_ONLY,
        ),
        (
            "def f(service):\n    data = service.get_user()\n    return len(data)\n",
            "data",
            "user",
            Confidence.SUGGESTION_ONLY,
        ),
        (
            'def f(registry, value):\n    result = registry["factory"](value)\n    result.unknown\n',
            "result",
            None,
            None,
        ),
        (
            "import requests\n\ndef f(url):\n"
            "    consume(result)\n"
            "    print(result.text)\n"
            "    result = requests.get(url)\n"
            "    return result\n",
            "result",
            "response",
            Confidence.SUGGESTION_ONLY,
        ),
        (
            "import tomllib\n\ndef f(path):\n    data: bytes = read_bytes(path)\n    return tomllib.loads(data)\n",
            "data",
            None,
            None,
        ),
        (
            "import urllib\n\ndef f(url):\n    result = urllib.request.urlopen(url)\n    return result.read()\n",
            "result",
            "file_handle",
            Confidence.SUGGESTION_ONLY,
        ),
    ],
    ids=[
        "immediate-http-json",
        "bytes-json",
        "reflection",
        "function-return-annotation",
        "urllib-submodule-import",
        "singular-collection",
        "unrecognised-call",
        "earlier-consumer-use",
        "bytes-toml",
        "bare-urllib-package",
    ],
)
def test_plan_for_inference_signals(
    source: str,
    target: str,
    expected_name: str | None,
    expected_confidence: Confidence | None,
) -> None:
    _assert_plan_for(source, target, expected_name, expected_confidence)


@pytest.mark.parametrize(
    ("source", "target", "name"),
    [
        (
            "import json\n\ndef f(path):\n    data: str = read_text(path)\n    return json.loads(data)\n",
            "data",
            "json_text",
        ),
        (
            "import tomllib\n\ndef f(path):\n    data: str = read_text(path)\n    return tomllib.loads(data)\n",
            "data",
            "toml_text",
        ),
        (
            'import subprocess\n\ndef f():\n    data = ["git", "status"]\n    return subprocess.run(data)\n',
            "data",
            "command",
        ),
    ],
)
def test_consumer_evidence_can_confirm_a_role(source: str, target: str, name: str) -> None:
    _assert_plan_for(source, target, name, Confidence.AUTO_FIX)


@pytest.mark.parametrize(
    ("source", "target", "name"),
    [
        (
            "def f(service):\n    result = service.is_ready()\n    if result:\n        return result\n",
            "result",
            "is_ready",
        ),
        ("def f():\n    data = find_users()\n    for user in data:\n        print(user)\n", "data", "users"),
        ("def f():\n    data = find_users()\n    return len(data)\n", "data", "users"),
        ("def f():\n    data = find_users()\n    return user in data\n", "data", "users"),
        ("def f(users):\n    data = [user.id for user in users]\n    return data\n", "data", "user_ids"),
    ],
)
def test_control_flow_and_comprehensions_confirm_names(source: str, target: str, name: str) -> None:
    _assert_plan_for(source, target, name, Confidence.AUTO_FIX)


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    result = is_ready()\n    return result is None\n",
        "def f():\n    data = [transform(value) for value in values]\n",
        "def f():\n    data = find_users() if enabled else find_orders()\n",
        "def f():\n    data = get_data()\n",
    ],
)
def test_ambiguous_semantics_do_not_produce_names(source: str) -> None:
    _assert_plan_for(source, "data" if "data" in source else "result", None, None)


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    data: Payload = get_payload()\n    data = get_payload()\n",
        "def f():\n    data: Payload = get_payload()\n    del data\n",
        "def f():\n    data: Payload = get_payload()\n    def nested():\n        nonlocal data\n        return data\n",
        "def f():\n    data: Payload = get_payload()\n    class Nested:\n        value = data\n",
        "def f(payload):\n    data: Payload = get_payload()\n    return data\n",
        "data: Payload = get_payload()\n",
    ],
)
def test_unsafe_bindings_collisions_and_module_scope_do_not_produce_names(source: str) -> None:
    _assert_plan_for(source, "data", None, None)


def test_ignored_binding_is_not_planned() -> None:
    source = "def f():\n    data: Payload = get_payload()\n"

    assert _plans(source, {2}) == {}


def test_reachable_scopes_reject_only_equal_names() -> None:
    source = """def outer():
    data: Payload = get_payload()

    def inner():
        result: Payload = get_payload()
        return result

    return data, inner
"""

    assert _plans(source) == {}


def test_sibling_scopes_can_use_distinct_names() -> None:
    source = """def first():
    data: FirstPayload = get_first_payload()
    return data

def second():
    data: SecondPayload = get_second_payload()
    return data
"""

    proposals = _plans(source)

    assert {proposal.name for proposal in proposals.values()} == {"first_payload", "second_payload"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("HTTPResponse", "http_response"),
        ("XMLParser", "xml_parser"),
        ("UserID", "user_id"),
    ],
)
def test_snake_case_conversion(name: str, expected: str) -> None:
    assert _to_snake_case(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("user", "users"), ("entry", "entries"), ("match", "matches"), ("person", None)],
)
def test_regular_pluralization(name: str, expected: str | None) -> None:
    assert _pluralize(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"), [("users", "user"), ("entries", "entry"), ("matches", "match"), ("class", "class")]
)
def test_regular_singularization(name: str, expected: str) -> None:
    assert _singularize(name) == expected


@pytest.mark.parametrize(
    ("annotation", "name", "constraints"),
    [
        ("User", "user", set()),
        ("bool", None, {"bool"}),
        ("str", None, {"text"}),
        ("bytes", None, {"bytes"}),
        ("list[User]", "users", set()),
    ],
)
def test_annotation_helpers(annotation: str, name: str | None, constraints: set[str]) -> None:
    expression = ast.parse(annotation, mode="eval").body

    assert _annotation_name(expression) == name
    assert _annotation_constraints(expression) == constraints


def test_small_ast_helpers() -> None:
    command = ast.parse('["git", "status"]', mode="eval").body
    length_call = ast.parse("len(values)", mode="eval").body
    json_call = ast.parse("json.loads(text)", mode="eval").body
    subprocess_call = ast.parse("subprocess.run(args=command)", mode="eval").body
    comprehension = ast.parse("[user.id for user in users]", mode="eval").body

    assert _is_command_shape(command)
    assert not _is_command_shape(ast.parse("command", mode="eval").body)
    assert isinstance(length_call, ast.Call)
    assert _is_len_call(length_call, 0)
    assert not _is_len_call(length_call, "items")
    assert isinstance(json_call, ast.Call)
    assert _is_parser_argument(("json", "loads"), 0, "json")
    assert not _is_parser_argument(("json", "loads"), 1, "json")
    assert not _is_parser_argument(("tomllib", "loads"), 0, "json")
    assert _is_parser_argument(("tomllib", "loads"), 0, "toml")
    assert isinstance(subprocess_call, ast.Call)
    assert _is_subprocess_argument(("subprocess", "run"), "args")
    assert not _is_subprocess_argument(("subprocess", "run"), 2)
    assert _is_public_attribute("payload")
    assert not _is_public_attribute("_payload")
    assert not _is_public_attribute("PAYLOAD")
    assert isinstance(comprehension, ast.ListComp)
    assert _comprehension_name(comprehension) == "user_ids"


def test_index_handles_rare_scope_constructs() -> None:
    source = """from . import local
from package import *
import package.submodule

class Container:
    async def method(self):
        data = get_payload()
        async for item in data:
            assert not item

    class Nested:
        pass

def function[T](value, /, *args, keyword, **kwargs):
    consume(data)
    data = get_payload()
    while not data:
        break
    match value:
        case [*items]:
            pass
        case {**rest}:
            pass
    return member in values["members"]
"""

    index = _Index(ast.parse(source))

    assert index.root.children


def test_named_expression_bindings_in_every_comprehension_form() -> None:
    source = """def f(values):
    first = [(data := value) for value in values]
    second = {(result := value) for value in values}
    third = {value: (data := value) for value in values}
    fourth = ((result := value) for value in values)
    return first, second, third, fourth
"""

    assert _plans(source) == {}


def test_parser_refinement_removes_conflicting_text_candidates() -> None:
    candidates = {"text": {"producer"}, "json_text": {"parser"}}

    _refine_parser_candidates(candidates, {"bytes"})

    assert candidates == {}


def test_sibling_scopes_can_reuse_the_same_name() -> None:
    source = """def first():
    data: Payload = get_payload()
    return data

def second():
    result: Payload = get_payload()
    return result
"""

    assert {proposal.name for proposal in _plans(source).values()} == {"payload"}


def test_sibling_collision_planning_scales_with_scope_count() -> None:
    source = "\n\n".join(
        f"def function_{index}():\n    data: Payload = get_payload()\n    return data" for index in range(1_000)
    )

    assert len(_plans(source)) == 1_000


def test_annotation_and_name_helper_edge_cases() -> None:
    qualified = ast.parse("types.User", mode="eval").body
    nested = ast.parse("Result[User]", mode="eval").body
    multi_generator = ast.parse("[user.id for user in users for group in groups]", mode="eval").body
    wrong_base = ast.parse("[group.id for user in users]", mode="eval").body
    private_attribute = ast.parse("[user._id for user in users]", mode="eval").body

    assert _annotation_name(qualified) == "user"
    assert _annotation_name(nested) == "result"
    assert isinstance(multi_generator, ast.ListComp)
    assert _comprehension_name(multi_generator) is None
    assert isinstance(wrong_base, ast.ListComp)
    assert _comprehension_name(wrong_base) is None
    assert isinstance(private_attribute, ast.ListComp)
    assert _comprehension_name(private_attribute) is None
    assert _pluralize(None) is None
    assert _pluralize("box") == "boxes"


def test_index_handles_nonbinding_type_params_and_nonname_loops() -> None:
    tree = ast.parse("def f():\n    first = second = value\n    for left, right in values:\n        pass\n")
    function = tree.body[0]

    assert isinstance(function, ast.FunctionDef)
    function.type_params.append(cast("ast.type_param", ast.Name(id="T", ctx=ast.Load())))
    _Index(tree)


def test_index_handles_unbound_match_patterns_and_nonname_keyword_values() -> None:
    source = """def f(value, payload):
    consume(value=make_value(), **kwargs)
    consume(value=payload)
    match value:
        case [*_]:
            pass
        case {"name": name}:
            pass
"""

    _Index(ast.parse(source))


def test_dynamic_command_and_nonmatching_loop_keep_suggestions_ambiguous() -> None:
    source = """import subprocess

def f(service):
    data = service.get_command()
    subprocess.run(data)
    result = find_users()
    for item in result:
        print(item)
"""

    plans = _plans(source)

    by_name = {proposal.name: proposal.confidence for proposal in plans.values()}

    assert by_name == {"command": Confidence.AUTO_FIX, "users": Confidence.SUGGESTION_ONLY}


def test_nonresponse_json_chain_and_unrecognized_registry_call_are_ignored() -> None:
    source = """import re

def f(client):
    data = client.get().json()
    result = re.compile("x")
"""

    assert _plans(source) == {}
