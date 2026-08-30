from __future__ import annotations

import ast
import tempfile
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from pre_commit_hooks.ast_checks._base import FixOutcome, Violation
from pre_commit_hooks.ast_checks.meaningless_vars import (
    MeaninglessVarsCheck,
    MeaninglessVarsLevel,
    _collect_replacements,
    _collect_scope_replacements,
    _function_name_describes_parameter,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(
    "source",
    [
        """
from typing import NamedTuple

class ChosenEdge(NamedTuple):
    from_idx: int
    to_idx: int
    data: str  # Should NOT be flagged - class provides context
""",
        """
from dataclasses import dataclass

@dataclass
class UserData:
    name: str
    data: dict  # Should NOT be flagged
    result: str  # Should NOT be flagged
""",
        """
class Config:
    data = {}  # Class attribute - should NOT be flagged
    result = None  # Class attribute - should NOT be flagged
""",
        """
from pydantic import BaseModel, model_validator
from typing import Any

class Email(BaseModel):

    @model_validator(mode="before")
    @classmethod
    def content_is_provided(cls, data: Any) -> Any:
        return data
""",
        """
from pydantic import BaseModel, model_validator
from typing import Any

class MyModel(BaseModel):

    @model_validator
    @classmethod
    def validate_all(cls, data: Any) -> Any:
        return data
""",
        """
def create_model():
    class Model:
        data: str  # Should NOT be flagged
    return Model
""",
        """
def process():
    data = {}  # pytriage: TR1
    return data
""",
        """
def process():
    data = {}  # pytriage: TR1
    result = None  # pytriage: TR1
    return data, result
""",
        """def process():
    data = 1  # pytriage: TR1
""",
        """
def process():
    data = {}  # pytriage: TR5,TR1
    return data
""",
        """class Foo:
    def __init__(self):
        self.data: int = 5
""",
        """class Model:
    @model_validator
    async def bare(data):
        return data
""",
        """def process():
    data, result = get_values()  # Multiple targets - not supported
    return data, result
""",
        """from typing import Any, List, Optional, Tuple

def feed_data(
    self,
    data: bytes,
    SEP: Optional[str] = None,
    *args: Any,
    **kwargs: Any,
) -> Tuple[List[bytes], bool, bytes]:
    return [], True, data
""",
        """def parse_client_bulk_write_result(result):
    return result
""",
        """def send_data(data: bytes):
    return data
""",
    ],
    ids=[
        "class-attributes",
        "dataclass-fields",
        "regular-class-attributes",
        "pydantic-validator-data-param",
        "pydantic-validator-bare",
        "nested-class-in-function",
        "inline-ignore-comment",
        "all-suppressed",
        "single-suppressed",
        "multi-code-suppression-list",
        "annotated-attribute-assignment",
        "async-model-validator-decorator",
        "multiple-assignment-targets",
        "function-name-describes-data-parameter",
        "function-name-describes-result-parameter",
        "function-name-describes-data-parameter-simple",
    ],
)
def test_check_reports_no_violations(source: str) -> None:
    assert (
        MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(Path("test.py"), ast.parse(source), source)
        == []
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            """
from pydantic import BaseModel, model_validator
from typing import Any

class MyModel(BaseModel):

    @model_validator(mode="before")
    @classmethod
    def validate_all(cls, data: Any) -> Any:
        result = do_something(data)  # 'result =' in body should still be flagged
        return result
""",
            {"message_contains": "result"},
        ),
        (
            """
from pydantic import BaseModel

class MyModel(BaseModel):

    def process(self, data: dict) -> dict:
        return data
""",
            {"message_contains": "data"},
        ),
        (
            """
def process():
    data = {}  # Should be flagged
    return data
""",
            {"message_contains": "data", "line": 3},
        ),
        (
            """
def process(data):  # Should be flagged
    return data
""",
            {"message_contains": "data", "line": 2},
        ),
        (
            """
async def fetch(data):  # Should be flagged
    return await data
""",
            {"message_contains": "data", "line": 2},
        ),
        (
            """
def fetch_users():
    data = response.get()
    return data
""",
            {"message_contains": "data"},
        ),
        (
            """
async def fetch():
    result = await some_call()  # Should be flagged
    return result
""",
            {"message_contains": "result"},
        ),
        (
            """
def process(*data):  # Should be flagged
    return data
""",
            {"message_contains": "data"},
        ),
        (
            """
def process(**data):  # Should be flagged
    return data
""",
            {"message_contains": "data"},
        ),
        (
            """
def process():
    data: dict  # Should be flagged even without value
    return None
""",
            {"message_contains": "data"},
        ),
        (
            """def process(data, /, other):  # 'data' is positional-only
    return data, other
""",
            {"message_contains": "data"},
        ),
        (
            """def process(*, data, other):  # 'data' is keyword-only
    return data, other
""",
            {"message_contains": "data"},
        ),
        (
            """data: dict = {}  # Should be flagged
""",
            {"message_contains": "data"},
        ),
        (
            """def process():
    data: dict = {}  # Should be flagged with suggestion
    return data
""",
            {"message_contains": "data"},
        ),
        (
            """
def compute():
    result = get_value()
    return result
""",
            {"message_contains": "result"},
        ),
        (
            """
class TestSomething:
    def test_query(self, conn):
        result = conn.execute("SELECT COUNT(*) FROM t").fetchone()
        assert result is not None
""",
            {"message_contains": "result"},
        ),
        (
            """def fetch():
    result = get_result()
    return result
""",
            {"message_contains": "result"},
        ),
        (
            """def process():
    data = get_user()
    return data
""",
            {"fixable": False, "suggestion": "user"},
        ),
        (
            """import requests

def process():
    data = requests.get(url).json()
    return data
""",
            {"fixable": False, "suggestion": "payload"},
        ),
        (
            """def process():
    data = (
        get_user()
    )
    return data
""",
            {"fixable": False, "suggestion": "user"},
        ),
        (
            """from typing import Union, Tuple

def handle(self, data: Union[bytes, bytearray, memoryview]) -> Tuple[bool, bytes]:
    return True, data
""",
            {"fixable": False, "suggestion": None},
        ),
        (
            """def handle(self, data: bytes | int):
    return data
""",
            {"fixable": False, "suggestion": None},
        ),
    ],
    ids=[
        "pydantic-validator-body-still-checked",
        "non-validator-method-data-param",
        "function-variable",
        "function-parameter",
        "async-function-parameter",
        "unknown-receiver",
        "async-function-variable",
        "vararg-parameter",
        "kwarg-parameter",
        "annotated-assignment-without-value",
        "positional-only-parameter",
        "keyword-only-parameter",
        "module-level-annotated-assignment-with-value",
        "function-annotated-assignment-with-value",
        "result-variable",
        "result-variable-in-class-method",
        "meaningless-derived-name",
        "producer-suggestion-only",
        "http-json-payload-suggestion-only",
        "multiline-producer-suggestion-only",
        "union-annotated-parameter-never-suggested",
        "pipe-union-annotated-parameter-never-suggested",
    ],
)
def test_check_reports_single_violation(source: str, expected: dict[str, Any]) -> None:
    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )

    assert len(violations) == 1
    violation = violations[0]
    if "message_contains" in expected:
        assert expected["message_contains"] in violation.message
    if "line" in expected:
        assert violation.line == expected["line"]
    if "fixable" in expected:
        assert violation.fixable is expected["fixable"]
    if "suggestion" in expected:
        assert violation.fix_data is not None
        assert violation.fix_data["suggestion"] == expected["suggestion"]


@pytest.mark.parametrize(
    ("source", "count"),
    [
        (
            """
data = {}  # Should be flagged
result = None  # Should be flagged
""",
            2,
        ),
        (
            """def outer():
    data = 1  # Should be flagged

    def inner():
        data = 2  # Should be flagged (separate scope)
        return data

    return data + inner()
""",
            2,
        ),
    ],
    ids=["module-level-variables", "nested-function-scope-flagged-separately"],
)
def test_check_reports_violation_count(source: str, count: int) -> None:
    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )
    assert len(violations) == count


@pytest.mark.parametrize(
    ("second_fragment", "expected_lines"),
    [
        ("    result = None  # pytriage: TR1\n", [3, 4]),
        ("    # fmt: off\n    result = None\n    # fmt: on\n", [3]),
    ],
    ids=["pytriage", "format-suppressed"],
)
def test_check_records_suppression_usage_for_each_reportable_candidate(
    second_fragment: str, expected_lines: list[int]
) -> None:
    source = f"""
def process():
    data = {{}}  # pytriage: TR1
{second_fragment}    return data, result
"""

    check_result = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check_with_suppression_tracking(
        Path("test.py"), ast.parse(source), source
    )

    assert check_result == []
    assert [usage.line for usage in check_result.suppression_usages] == expected_lines


def test_multiple_meaningless_names() -> None:
    source = """
def process():
    data = {}
    result = None
    results = []
    return data, result, results
"""

    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )

    assert len(violations) == 3
    names = {v.message.split("'")[1] for v in violations}
    assert names == {"data", "result", "results"}


def test_multiple_violations_same_scope() -> None:
    source = """def process():
    data = response.get()
    result = data.json()
    return result
"""

    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )

    assert len(violations) == 2
    names = {v.fix_data["name"] for v in violations if v.fix_data}
    assert names == {"data", "result"}


def test_reassignment_suppresses_suggestions() -> None:
    source = """def process():
    data: Response = get_response()
    print(data)
    data = get_response()
    return data
"""

    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )

    assert len(violations) == 2
    assert all(not violation.fixable for violation in violations)
    assert all(violation.fix_data and violation.fix_data["suggestion"] is None for violation in violations)


def test_model_validator_decorator_skips_arg_check() -> None:
    source = """class Model:
    @staticmethod
    def plain(data):
        return data

    @model_validator
    def bare(data):
        return data

    @model_validator(mode="before")
    def called(data):
        return data
"""

    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )

    flagged_functions = {v.fix_data["name"] for v in violations if v.fix_data}
    assert flagged_functions == {"data"}
    assert len(violations) == 1


def test_name_collision_suffixes_suggestion() -> None:
    source = """def process():
    response = 1
    response_2 = 2
    data: Response = get_response()
    return data
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
            filepath, ast.parse(source), source
        )

        assert len(violations) == 1
        assert violations[0].fixable
        assert violations[0].fix_data is not None
        assert violations[0].fix_data["suggestion"] == "response_3"


def test_tokenize_error_handling() -> None:
    source = "def func():\n    data = 1  # missing closing quote"

    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )

    assert len(violations) >= 1


def test_check_ids() -> None:
    check = MeaninglessVarsCheck()

    assert check.check_id == "meaningless-vars"
    assert check.error_code == "TR1"


def test_prefilter_pattern() -> None:
    patterns = MeaninglessVarsCheck().get_prefilter_pattern()

    assert patterns is not None
    assert "data" in patterns
    assert "result" in patterns


@pytest.mark.parametrize(
    ("function_name", "parameter_name", "expected"),
    [
        ("feed_data", "data", True),
        ("parse_client_bulk_write_result", "result", True),
        ("send_data", "data", True),
        ("data", "data", False),
        ("_data", "data", False),
        ("update", "data", False),
        ("dataset", "data", False),
    ],
)
def test_function_name_describes_parameter(function_name: str, parameter_name: str, expected: bool) -> None:
    assert _function_name_describes_parameter(function_name, parameter_name) is expected


def test_autofix_applies_suggestions() -> None:
    source = """import requests

def fetch_users():
    data = requests.get(url)
    return data.status_code
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)

        assert len(violations) == 1
        assert violations[0].fixable

        fix_result = check.fix(filepath, violations, source, tree)
        assert fix_result.outcomes == (FixOutcome.APPLIED,)

        fixed_content = filepath.read_text()

        assert "data" not in fixed_content
        assert "return response.status_code" in fixed_content


def test_autofix_refuses_when_a_reference_line_is_format_suppressed(tmp_path: Path) -> None:
    source = """import requests

def fetch_users():
    data = requests.get(url)
    # fmt: off
    return data.status_code
    # fmt: on
"""

    filepath = tmp_path / "test.py"
    filepath.write_text(source)

    tree = ast.parse(source)
    check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
    violations = check.check(filepath, tree, source)

    assert len(violations) == 1
    assert violations[0].fixable

    fix_result = check.fix(filepath, violations, source, tree)
    assert fix_result.outcomes == (FixOutcome.DECLINED,)
    assert filepath.read_text() == source


def test_autofix_declines_fixable_violation_without_fix_data(tmp_path: Path) -> None:
    source = """import requests

def fetch_users():
    data = requests.get(url)
    return data.status_code
"""
    filepath = tmp_path / "test.py"
    filepath.write_text(source)
    tree = ast.parse(source)
    check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
    (candidate,) = check.check(filepath, tree, source)
    stale_violation = Violation(
        check_id=candidate.check_id,
        error_code=candidate.error_code,
        line=candidate.line,
        col=candidate.col,
        message=candidate.message,
        fixable=True,
    )

    fix_result = check.fix(filepath, [candidate, stale_violation], source, tree)

    assert fix_result.outcomes == (FixOutcome.APPLIED, FixOutcome.DECLINED)


def test_autofix_no_fixable_violations() -> None:
    source = """def process():
    data = {}  # No autofix suggestion available
    return data
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        non_fixable = [v for v in violations if not v.fixable]

        success = check.fix(filepath, non_fixable, source, tree)
        assert success.outcomes == (FixOutcome.DECLINED,) * len(non_fixable)


def test_autofix_follows_closure_reference_into_nested_function() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner():
        return data

    return inner()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> dict[str, str]:
            return {"k": "v"}

    assert module_namespace["outer"](FakeResponse()) == {"k": "v"}


def test_autofix_follows_closure_reference_into_lambda() -> None:
    source = """def outer(response):
    data: Payload = response.json()
    return lambda: data
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "return lambda: payload" in fixed_content


def test_autofix_follows_closure_reference_into_comprehension() -> None:
    source = """def outer(response, items):
    data: Payload = response.json()
    return [str(data) for _ in items]
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    ast.parse(fixed_content)


def test_walrus_rebinding_suppresses_suggestion() -> None:
    source = """def outer(response, items):
    data: Payload = response.json()
    return [(data := item) for item in items], data
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert fixed_content == source


@pytest.mark.parametrize(
    ("source", "expected_snippet"),
    [
        (
            """def outer(response):
    data: Payload = response.json()
    return [data for data in data]
""",
            "return [data for data in payload]",
        ),
        (
            """def outer(response, xs):
    data: Payload = response.json()
    return [x for x in xs for z in data]
""",
            "for z in payload",
        ),
        (
            """def outer(response, xs):
    data: Payload = response.json()
    return {x: data for x in xs}
""",
            "{x: payload for x in xs}",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner(x: data = data) -> data:
        return x

    return inner(), data
""",
            "def inner(x: payload = payload) -> payload:",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner(*args: data, **kwargs: data):
        return args, kwargs

    return inner(), data
""",
            "def inner(*args: payload, **kwargs: payload):",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner[T](x: data) -> T:
        return x

    return inner(1), data
""",
            "def inner[T](x: payload) -> T:",
        ),
        (
            """def outer(response):
    data: Payload = response.json()
    return lambda x=data: x
""",
            "return lambda x=payload: x",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner[T](x: data):
        return x

    return inner(1), data
""",
            "def inner[T](x: payload):",
        ),
    ],
    ids=[
        "comprehension-first-iterable",
        "comprehension-later-iterable",
        "dict-comprehension",
        "function-default-and-annotations",
        "vararg-kwarg-annotations",
        "type-params-annotation-not-shadowed",
        "lambda-default",
        "type-params-no-return-annotation",
    ],
)
def test_autofix_renames_reference_evaluated_in_enclosing_scope(source: str, expected_snippet: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert expected_snippet in fixed_content
    ast.parse(fixed_content)


@pytest.mark.parametrize(
    ("source", "shadowed_snippet"),
    [
        (
            """def outer(response):
    data: Payload = response.json()

    def inner(data):
        return data

    return inner("unrelated"), data
""",
            "def inner(data):\n        return data",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner():
        data = "local"
        return data

    return inner(), data
""",
            'data = "local"\n        return data',
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner():
        def data():
            return 1

        return data()

    return inner(), data
""",
            "def data():\n            return 1",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner():
        import data

        return data

    return inner(), data
""",
            "import data\n\n        return data",
        ),
        (
            """def outer(response):
    data: Payload = response.json()
    return [data for data in range(3)], data
""",
            "[data for data in range(3)]",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner():
        try:
            return risky()
        except RuntimeError as data:
            return data

    return inner(), data
""",
            "except RuntimeError as data:\n            return data",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner(command):
        match command:
            case data:
                return data

    return inner("x"), data
""",
            "case data:\n                return data",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner(command):
        match command:
            case {**data}:
                return data

    return inner({}), data
""",
            "case {**data}:\n                return data",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner[data]() -> data:
        return data

    return inner(), data
""",
            "def inner[data]() -> data:\n        return data",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner():
        del data
        data = "local value"
        return data

    return inner(), data
""",
            'del data\n        data = "local value"\n        return data',
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner():
        import data.models

        return data.models

    return inner(), data
""",
            "import data.models\n\n        return data.models",
        ),
        (
            """def outer(response):
    data: Payload = response.json()

    def inner():
        from collections import data

        return data

    return inner(), data
""",
            "from collections import data\n\n        return data",
        ),
    ],
    ids=[
        "parameter",
        "local-reassignment",
        "nested-def",
        "nested-import",
        "comprehension-for-target",
        "except-handler-name",
        "match-as-capture",
        "match-mapping-rest",
        "type-parameter",
        "nested-del-then-reassignment",
        "dotted-import",
        "from-import",
    ],
)
def test_autofix_does_not_rename_shadowed_reference_in_nested_scope(source: str, shadowed_snippet: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert shadowed_snippet in fixed_content
    assert "payload" in fixed_content


def test_autofix_never_offered_for_name_referenced_via_nonlocal() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner():
        nonlocal data
        data = "mutated"

    inner()
    return data
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert violations
        assert all(not v.fixable for v in violations)

        assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert fixed_content == source
    ast.parse(fixed_content)


@pytest.mark.parametrize(
    "source",
    [
        """def outer(response):
    data: Payload = response.json()

    def reader():
        return data

    def data():
        return "shadowing function"

    return reader, data
""",
        """def outer(response):
    data: Payload = response.json()

    def reader():
        return data

    class data:
        pass

    return reader, data
""",
        """def outer(response):
    data: Payload = response.json()

    def reader():
        return data

    try:
        pass
    except RuntimeError as data:
        pass

    return reader
""",
        """def outer(response, command):
    data: Payload = response.json()

    def reader():
        return data

    match command:
        case data:
            pass

    return reader
""",
        """def outer(response, command):
    data: Payload = response.json()

    def reader():
        return data

    match command:
        case {**data}:
            pass

    return reader
""",
        """def outer(response):
    data: Payload = response.json()

    def reader():
        return data

    import data.models

    return reader
""",
        """def outer(response):
    data: Payload = response.json()

    def reader():
        return data

    from collections import data

    return reader
""",
    ],
    ids=[
        "same-scope-def",
        "same-scope-class",
        "same-scope-except-handler",
        "same-scope-match-as",
        "same-scope-match-mapping-rest",
        "same-scope-dotted-import",
        "same-scope-from-import",
    ],
)
def test_autofix_never_offered_when_same_scope_rebinds_via_non_name_construct(source: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert violations
        assert all(not v.fixable for v in violations)

        assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert fixed_content == source
    ast.parse(fixed_content)


def test_autofix_never_offered_for_module_global_read_in_function() -> None:
    source = """data = None


def loader(response):
    global data
    data: Payload = response.json()


def reader():
    global data
    return data
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert violations
        assert all(not v.fixable for v in violations)

        check.fix(filepath, violations, source, tree)
        fixed_content = filepath.read_text()

    assert fixed_content == source


@pytest.fixture
def autofix_meaningless_vars(tmp_path: Path) -> Callable[[str], str]:
    def apply(source: str) -> str:
        filepath = tmp_path / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        outcomes = check.fix(filepath, violations, source, tree).outcomes

        assert outcomes
        assert all(outcome is FixOutcome.APPLIED for outcome in outcomes)

        return filepath.read_text()

    return apply


def test_autofix_assigns_distinct_names_to_cross_scope_suggestions(
    autofix_meaningless_vars: Callable[[str], str],
) -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner(response2):
        result: Payload = response2.json()
        return data, result

    return inner(response)
"""
    fixed_content = autofix_meaningless_vars(source)

    assert "payload: Payload = response.json()" in fixed_content
    assert "payload_2: Payload = response2.json()" in fixed_content
    assert "return payload, payload_2" in fixed_content


def test_autofix_suffixes_suggestion_colliding_with_existing_nested_name(
    autofix_meaningless_vars: Callable[[str], str],
) -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner():
        payload = 5
        return payload, data

    return inner()
"""
    fixed_content = autofix_meaningless_vars(source)

    assert "payload_2: Payload = response.json()" in fixed_content
    assert "return payload, payload_2" in fixed_content


def test_autofix_suffixes_suggestion_colliding_with_nested_parameter_name(
    autofix_meaningless_vars: Callable[[str], str],
) -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner(payload):
        return data

    return inner(5)
"""
    fixed_content = autofix_meaningless_vars(source)

    assert "payload_2: Payload = response.json()" in fixed_content
    assert "return payload_2" in fixed_content


def test_autofix_suffixes_suggestion_colliding_with_nested_global_declaration(
    autofix_meaningless_vars: Callable[[str], str],
) -> None:
    source = """payload = "module-level unrelated value"

def outer(response):
    data: Payload = response.json()

    def inner():
        global payload
        return data

    return inner()
"""
    fixed_content = autofix_meaningless_vars(source)

    assert "payload_2: Payload = response.json()" in fixed_content
    assert "return payload_2" in fixed_content


def test_autofix_renames_walrus_target_inside_default_evaluated_in_enclosing_scope(
    autofix_meaningless_vars: Callable[[str], str],
) -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner(x=(data := response.json())):
        return data, x

    return inner()
"""
    fixed_content = autofix_meaningless_vars(source)

    assert "data" not in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "value"

    assert module_namespace["outer"](FakeResponse()) == ("value", "value")


def test_autofix_follows_closure_through_scope_that_itself_contains_a_shadowing_nested_scope() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def middle():
        def deeper():
            data = "unrelated local"
            return data

        return data, deeper()

    return middle()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "def deeper():\n            data = " in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "closure value"

    assert module_namespace["outer"](FakeResponse()) == ("closure value", "unrelated local")


def test_autofix_assigns_outer_name_first_when_nested_closure_precedes_assignment(
    autofix_meaningless_vars: Callable[[str], str],
) -> None:
    source = """def outer(response, response2):
    def inner():
        result: Payload = response2.json()
        return data, result

    data: Payload = response.json()
    return inner()
"""
    fixed_content = autofix_meaningless_vars(source)

    assert "payload_2: Payload = response2.json()" in fixed_content
    assert "return payload, payload_2" in fixed_content
    assert "payload: Payload = response.json()" in fixed_content


def test_autofix_does_not_rename_annotation_under_deferred_annotations() -> None:
    source = """from __future__ import annotations

data = int

def outer(response):
    data: Payload = response.json()

    def inner(x: data):
        return x

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "def inner(x: data):" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    hints = typing.get_type_hints(inner)
    assert hints == {"x": int}


def test_autofix_still_follows_annotation_closure_without_deferred_annotations() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner(x: data):
        return x

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "def inner(x: payload):" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(  # noqa: S102
        compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec", dont_inherit=True), module_namespace
    )

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    assert inner.__annotations__["x"] == "runtime value"


def test_autofix_never_offered_for_module_scope_name_referenced_in_annotation() -> None:
    source = """from __future__ import annotations

class Response:
    def json(self):
        return int

response = Response()
data: Payload = response.json()
result = response.json()

def f(x: data) -> result:
    return x
"""
    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )

    assert len(violations) == 2
    assert all(
        violation.message.endswith("Use a more descriptive name. Or add '# pytriage: TR1' to suppress.")
        for violation in violations
    )
    assert all(violation.fixable is False for violation in violations)


def test_autofix_follows_closure_into_type_parameter_bound_and_default() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner[**Q, T: data = data, *Ts = data, **P = data]():
        return T, Ts, P, Q

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "def inner[**Q, T: payload = payload, *Ts = payload, **P = payload]():" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    _, type_var, type_var_tuple, param_spec = inner.__type_params__
    assert type_var.__bound__ == "runtime value"
    assert type_var.__default__ == "runtime value"
    assert type_var_tuple.__default__ == "runtime value"
    assert param_spec.__default__ == "runtime value"


def test_autofix_does_not_rename_type_parameter_bound_referencing_a_peer_type_parameter() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner[data, T: data]():
        return T, data

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "def inner[data, T: data]():" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    peer_type_var, type_var = inner.__type_params__
    assert type_var.__bound__ is peer_type_var


def test_autofix_does_not_reuse_a_nested_functions_own_mapping_for_its_default() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner(x=data):
        data: InnerPayload = response.json()
        return x, data

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "def inner(x=payload):" in fixed_content
    assert "inner_payload: InnerPayload = response.json()" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    assert inner() == ("runtime value", "runtime value")


def test_autofix_does_not_rename_a_nested_functions_own_type_parameter_bound_via_its_own_scope() -> None:
    source = """def outer(response):
    def inner[data, T: data](response2):
        data = response2.json()
        return T, data

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert fixed_content == source


def test_autofix_does_not_rename_type_alias_bound_referencing_a_peer_type_parameter() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    type Alias[data, T: data] = T

    return Alias
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "type Alias[data, T: data] = T" in fixed_content
    assert "payload: Payload = response.json()" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    alias = module_namespace["outer"](FakeResponse())
    peer_data, type_var_t = alias.__type_params__
    assert type_var_t.__bound__ is peer_data


def test_autofix_preserves_type_alias_peer_reference_inside_nested_lambda(
    autofix_meaningless_vars: Callable[[str], str],
) -> None:
    source = """def outer(response):
    data: Payload = response.json()
    result: SecondaryPayload = response.json()

    type Alias[data] = lambda: (data, result)

    return Alias
"""

    fixed_content = autofix_meaningless_vars(source)

    assert "type Alias[data] = lambda: (data, secondary_payload)" in fixed_content


def test_autofix_follows_closure_into_type_alias_value() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    type Alias[T: int] = tuple[T, data]

    return Alias
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "type Alias[T: int] = tuple[T, payload]" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    alias = module_namespace["outer"](FakeResponse())
    type_var_t = alias.__type_params__[0]
    assert type_var_t.__bound__ is int
    assert typing.get_origin(alias.__value__) is tuple
    assert typing.get_args(alias.__value__) == (type_var_t, "runtime value")


def test_autofix_follows_closure_into_generic_functions_own_annotation_despite_body_shadowing() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner[T](value: data):
        data = 1
        return T, value, data

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "def inner[T](value: payload):" in fixed_content
    assert "data = 1" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(  # noqa: S102
        compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec", dont_inherit=True), module_namespace
    )

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    assert inner.__annotations__ == {"value": "runtime value"}


def test_autofix_does_not_rename_generic_functions_own_annotation_referencing_a_peer_type_parameter() -> None:
    source = """def outer(response):
    data: Payload = response.json()

    def inner[data](value: data) -> data:
        return value

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "def inner[data](value: data) -> data:" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(  # noqa: S102
        compile(ast.parse(fixed_content), "<meaningless_vars_fixture>", "exec", dont_inherit=True), module_namespace
    )

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    peer_type_var = inner.__type_params__[0]
    assert inner.__annotations__ == {"value": peer_type_var, "return": peer_type_var}


@pytest.mark.parametrize(
    "source",
    [
        "def inner[data, T: (lambda: (data, result))()]():\n    pass\n",
        "def inner[data, T](value: (lambda: (data, result))()):\n    pass\n",
    ],
    ids=["bound", "annotation"],
)
def test_collect_replacements_preserves_peer_names_inside_nested_generic_scopes(source: str) -> None:
    replacements = _collect_replacements(
        ast.parse(source).body[0],
        {"data": "payload", "result": "secondary_result"},
        has_future_annotations=False,
    )

    assert [(old_name, new_name) for _, _, old_name, new_name in replacements] == [("result", "secondary_result")]


def test_autofix_does_not_follow_generic_functions_own_annotation_under_deferred_annotations() -> None:
    source = """from __future__ import annotations

def outer(response):
    data: Payload = response.json()

    def inner[T](value: data):
        return value

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "def inner[T](value: data):" in fixed_content


def test_scope_names_ignore_unnamed_except_and_match_captures() -> None:
    source = """def outer(response):
    data: Payload = response.json()
    try:
        pass
    except Exception:
        pass
    match data:
        case _:
            pass
    return data
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    ast.parse(fixed_content)


def test_autofix_replaces_all_uses_in_scope() -> None:
    source = """import requests

def fetch_users():
    data = requests.get(url)
    print(data)
    result = data.json()
    return result
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        check.fix(filepath, violations, source, tree)

        fixed_content = filepath.read_text()

        assert "data" not in fixed_content
        assert "result = response.json()" in fixed_content
        assert ".json()" in fixed_content
        assert "print(" in fixed_content


def test_autofix_avoids_walrus_target_collision_in_comprehension() -> None:
    # A suggested name must not collide with a `:=` target bound inside a
    # comprehension in the same scope (PEP 572: the walrus target belongs
    # to the enclosing scope, not the comprehension's own scope), even
    # though the comprehension's own loop variable is correctly invisible
    # to it.
    source = (
        "def foo():\n"
        "    data = requests.get(url)\n"
        "    items = [y for x in xs if (response := check(x)) and response.ok]\n"
        "    return data, items\n"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        check.fix(filepath, violations, source, tree)

        fixed_content = filepath.read_text()

    assert "response = requests.get(url)" not in fixed_content
    assert "response := check(x)" in fixed_content


def test_scope_isolation() -> None:
    source = """def func1():
    data: FirstPayload = get_first_payload()
    return data

def func2():
    data: SecondPayload = get_second_payload()
    return data
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert len(violations) == 2

        check.fix(filepath, violations, source, tree)
        fixed_content = filepath.read_text()

        assert "first_payload: FirstPayload = get_first_payload()" in fixed_content
        assert "second_payload: SecondPayload = get_second_payload()" in fixed_content
        assert "def func1():" in fixed_content
        assert "def func2():" in fixed_content


def test_scope_replacement_helpers_support_class_bodies_and_modules() -> None:
    class_tree = ast.parse("class Container:\n    value = data\n")
    module_tree = ast.parse("data = value\n")

    assert _collect_replacements(class_tree.body[0], {"data": "payload"}, has_future_annotations=False) == [
        (2, 12, "data", "payload")
    ]
    assert _collect_scope_replacements(
        module_tree,
        {"data": "payload"},
        has_future_annotations=False,
    ) == [(1, 0, "data", "payload")]


@pytest.mark.parametrize(
    ("has_future_annotations", "annotation_replacements"),
    [
        (False, [(8, 37, "data", "payload"), (8, 53, "data", "payload")]),
        (True, []),
    ],
    ids=["eager-annotations", "deferred-annotations"],
)
def test_scope_replacement_helpers_support_generic_class_headers_and_methods(
    has_future_annotations: bool,
    annotation_replacements: list[tuple[int, int, str, str]],
) -> None:
    source = """@data
class Container[T: data](data, key=data):
    value = data

    class Nested:
        value = data

    def method[V: data](self, value: data = data) -> data:
        return data
"""

    expected = [
        (1, 1, "data", "payload"),
        (2, 25, "data", "payload"),
        (2, 35, "data", "payload"),
        (2, 19, "data", "payload"),
        (3, 12, "data", "payload"),
        (6, 16, "data", "payload"),
        (8, 44, "data", "payload"),
        (8, 18, "data", "payload"),
        (9, 15, "data", "payload"),
        *annotation_replacements,
    ]

    assert sorted(
        _collect_replacements(
            ast.parse(source).body[0],
            {"data": "payload"},
            has_future_annotations=has_future_annotations,
        )
    ) == sorted(expected)


def test_scope_replacement_helpers_support_generic_methods_without_return_annotations() -> None:
    tree = ast.parse("class Container:\n    def method[T: data](self, value: data):\n        return data\n")

    assert sorted(_collect_replacements(tree.body[0], {"data": "payload"}, has_future_annotations=False)) == sorted(
        [
            (2, 18, "data", "payload"),
            (2, 37, "data", "payload"),
            (3, 15, "data", "payload"),
        ]
    )


def test_scope_replacement_helpers_preserve_generic_class_header_peer_names() -> None:
    tree = ast.parse("class Container[data](lambda: (data, result)):\n    pass\n")

    assert _collect_replacements(
        tree.body[0],
        {"data": "payload", "result": "value"},
        has_future_annotations=False,
    ) == [(1, 37, "result", "value")]


def test_repeated_binding_leaves_the_file_unchanged() -> None:
    source = """def process():
    data: Payload = get_payload()
    print(data)
    data = get_payload()
    print(data)
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert len(violations) == 2

        check.fix(filepath, violations, source, tree)

        fixed_content = filepath.read_text()

    assert fixed_content == source


def test_autofix_replaces_name_on_line_with_non_ascii_text() -> None:
    # ast.col_offset is a UTF-8 byte offset, not a character
    # offset. Non-ASCII text earlier on the same line as the meaningless
    # name must not throw off the position used to locate and replace it.
    source = """import requests

def process():
    label = "café"; data = requests.get(url)
    return data.status_code
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
        violations = check.check(filepath, tree, source)
        assert len(violations) == 1

        assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "response = requests.get(url)" in fixed_content
    assert "return response.status_code" in fixed_content


def test_check_reports_character_offset_not_byte_offset_before_multibyte_text() -> None:
    # ast.col_offset is a UTF-8 *byte* offset, not a character
    # offset -- storing it on Violation.col directly reports a column too
    # far right on any line with non-ASCII text before the violation
    # (ch. 7: "MUST report ... column information accurately"; ch. 20:
    # "MUST handle multibyte Unicode characters correctly"). "café; " is 6
    # characters but 7 UTF-8 bytes ('é' is 2 bytes), so a byte-offset
    # column would over-count "data"'s own position by one.
    source = "café; data = requests.get(url)\n"
    violations = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(
        Path("module.py"), ast.parse(source), source
    )

    assert len(violations) == 1
    assert violations[0].col == 6


def test_fix_write_failure_reports_failed_outcome(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = """import requests

def process():
    data = requests.get(url)
    return data.status_code


def other():
    result = 42
    return result
"""
    # Point at a path inside a directory that doesn't exist so write_text()
    # raises OSError.
    filepath = tmp_path / "missing_dir" / "test.py"

    tree = ast.parse(source)
    check = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
    violations = check.check(filepath, tree, source)
    # "result = 42" has no autofix pattern match, so it's non-fixable —
    # included specifically so the marking loop below has both a fixable
    # and a non-fixable violation to distinguish between.
    assert {v.fixable for v in violations} == {True, False}
    candidate = next(violation for violation in violations if violation.fixable)
    violations.append(
        Violation(
            check_id=candidate.check_id,
            error_code=candidate.error_code,
            line=candidate.line,
            col=candidate.col,
            message=candidate.message,
            fixable=True,
        )
    )

    with caplog.at_level("DEBUG"):
        fix_result = check.fix(filepath, violations, source, tree)
    # the write failure must be attributed to the violations it
    # actually affected, not left indistinguishable from "never attempted"
    # — the orchestrator's own report otherwise misleadingly suggests
    # re-running --fix, which would just fail identically again. A
    # non-fixable violation was never part of this attempt at all, so it
    # must be left alone rather than also marked failed.
    assert fix_result.outcomes == tuple(
        FixOutcome.FAILED if violation is candidate else FixOutcome.DECLINED for violation in violations
    )
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_default_level_is_conservative() -> None:
    source = """def other():
    result = 42
    return result
"""
    tree = ast.parse(source)

    assert MeaninglessVarsCheck().check(Path("test.py"), tree, source) == MeaninglessVarsCheck(
        level=MeaninglessVarsLevel.CONSERVATIVE
    ).check(Path("test.py"), tree, source)


@pytest.mark.parametrize(
    ("source", "conservative_count"),
    [
        (
            """def other():
    result = 42
    return result
""",
            0,
        ),
        (
            """import requests

def fetch_users():
    data = requests.get(url)
    return data.status_code
""",
            1,
        ),
    ],
    ids=["no-suggestion-hidden", "suggestion-still-reported"],
)
def test_conservative_level_gates_on_suggestion_presence(source: str, conservative_count: int) -> None:
    tree = ast.parse(source)

    conservative = MeaninglessVarsCheck().check(Path("test.py"), tree, source)
    permissive = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE).check(Path("test.py"), tree, source)

    assert len(conservative) == conservative_count
    assert len(permissive) == 1
