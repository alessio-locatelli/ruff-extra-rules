from __future__ import annotations

import ast
import tempfile
import typing
from pathlib import Path
from typing import Any

import pytest

from pre_commit_hooks.ast_checks._base import is_fix_failed
from pre_commit_hooks.ast_checks.forbid_vars import ForbidVarsCheck


@pytest.mark.parametrize(
    "source",
    [
        # Class attributes, NamedTuple fields, and dataclass fields are
        # excluded from analysis because the class name already provides
        # context.
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
        # Pydantic's @model_validator(mode="before") requires the
        # parameter to be named 'data'; flagging it would be a false
        # positive.
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
    data = {}  # pytriage: ignore=TRI001
    return data
""",
        """
def process():
    data = {}  # pytriage: ignore=TRI001
    result = None  # pytriage: ignore=TRI001
    return data, result
""",
        """def process():
    data = 1  # pytriage: ignore=TRI001
""",
        # An annotated assignment whose target is an attribute (e.g.
        # ``self.data: int = 5``), not a plain name, is skipped entirely —
        # only simple-name annotated assignments are analyzed.
        """class Foo:
    def __init__(self):
        self.data: int = 5
""",
        # Same exemption as the sync @model_validator case, but for an
        # async function definition.
        """class Model:
    @model_validator
    async def bare(data):
        return data
""",
        # Multiple assignment targets aren't supported, so the assignment
        # itself isn't flagged (though get_values() itself might be, if
        # it existed as a forbidden-named call).
        """def process():
    data, result = get_values()  # Multiple targets - not supported
    return data, result
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
        "annotated-attribute-assignment",
        "async-model-validator-decorator",
        "multiple-assignment-targets",
    ],
)
def test_check_reports_no_violations(source: str) -> None:
    assert ForbidVarsCheck().check(Path("test.py"), ast.parse(source), source) == []


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
    data = response.get()  # Should suggest 'response' as name
    return data
""",
            {"message_contains": "response", "fixable": True},
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
            # The semantic category's get_<x> -> <x> substitution can
            # itself produce a forbidden name; the fix then falls back to
            # a generic 'var' name instead of introducing a new violation.
            """def fetch():
    result = get_result()
    return result
""",
            {"fixable": True, "suggestion": "var"},
        ),
        (
            """def process():
    data = get_user()
    return data
""",
            {"fixable": True, "suggestion": "user"},
        ),
        (
            """def process():
    data = requests.get(url).json()
    return data
""",
            {"suggestion": "response"},
        ),
        (
            # The regex-group name substitution looks up the match on the
            # *target's own* source line. When the matched call lives on a
            # different line than the target (e.g. a parenthesized
            # multi-line RHS), that lookup finds nothing and the raw
            # group-reference name is kept as-is.
            """def process():
    data = (
        get_user()
    )
    return data
""",
            {"suggestion": r"\1"},
        ),
    ],
    ids=[
        "pydantic-validator-body-still-checked",
        "non-validator-method-data-param",
        "function-variable",
        "function-parameter",
        "async-function-parameter",
        "autofix-suggestion",
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
        "suggestion-fallback-when-in-forbidden-names",
        "semantic-naming-with-regex-groups",
        "find-best-match-prefers-higher-specificity",
        "semantic-naming-false-when-match-not-on-target-line",
    ],
)
def test_check_reports_single_violation(source: str, expected: dict[str, Any]) -> None:
    violations = ForbidVarsCheck().check(Path("test.py"), ast.parse(source), source)

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
    violations = ForbidVarsCheck().check(Path("test.py"), ast.parse(source), source)
    assert len(violations) == count


def test_multiple_forbidden_names() -> None:
    source = """
def process():
    data = {}
    result = None
    return data, result
"""

    violations = ForbidVarsCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 2
    names = {v.message.split("'")[1] for v in violations}
    assert names == {"data", "result"}


def test_multiple_violations_same_scope() -> None:
    source = """def process():
    data = response.get()
    result = data.json()
    return result
"""

    violations = ForbidVarsCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 2
    names = {v.fix_data["name"] for v in violations if v.fix_data}
    assert names == {"data", "result"}


def test_generate_unique_name_cache_hit_for_repeated_reassignment() -> None:
    # Branch coverage: the second reassignment of the same forbidden name
    # in the same scope returns the cached suggestion instead of
    # recomputing it.
    source = """def process():
    data = requests.get()
    print(data)
    data = requests.get()
    return data
"""

    violations = ForbidVarsCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 2
    suggestions = {v.fix_data["suggestion"] for v in violations if v.fix_data}
    assert suggestions == {"response"}


def test_model_validator_decorator_skips_arg_check() -> None:
    # A function decorated with an irrelevant decorator is still checked
    # for forbidden arg names, while one decorated with
    # ``@model_validator`` (in either bare or called form) is exempt.
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

    violations = ForbidVarsCheck().check(Path("test.py"), ast.parse(source), source)

    flagged_functions = {v.fix_data["name"] for v in violations if v.fix_data}
    assert flagged_functions == {"data"}
    assert len(violations) == 1


def test_name_conflict_counter_increment() -> None:
    source = """def process():
    response = 1
    response_2 = 2
    data = response.get()  # Should suggest response_3
    return data
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        violations = ForbidVarsCheck().check(filepath, ast.parse(source), source)

        assert len(violations) == 1
        assert violations[0].fix_data is not None
        assert violations[0].fix_data["suggestion"] == "response_3"


def test_tokenize_error_handling() -> None:
    # Deliberately malformed so tokenizing may raise partway through.
    source = "def func():\n    data = 1  # missing closing quote"

    violations = ForbidVarsCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) >= 1


def test_check_ids() -> None:
    check = ForbidVarsCheck()

    assert check.check_id == "forbid-vars"
    assert check.error_code == "TRI001"


def test_prefilter_pattern() -> None:
    patterns = ForbidVarsCheck().get_prefilter_pattern()

    # Returns ALL forbidden names so files with only 'result =' aren't
    # silently skipped during pre-filtering.
    assert patterns is not None
    assert "data" in patterns
    assert "result" in patterns


def test_autofix_applies_suggestions() -> None:
    source = """def fetch_users():
    data = response.get()
    return data
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)

        assert len(violations) == 1
        assert violations[0].fixable

        success = check.fix(filepath, violations, source, tree)
        assert success

        fixed_content = filepath.read_text()

        # May use response_2 instead of response to avoid a naming conflict.
        assert "data" not in fixed_content or "# pytriage" in fixed_content
        has_response = "return response" in fixed_content
        has_response_2 = "return response_2" in fixed_content
        assert has_response or has_response_2


def test_autofix_no_fixable_violations() -> None:
    source = """def process():
    data = {}  # No autofix suggestion available
    return data
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        non_fixable = [v for v in violations if not v.fixable]

        success = check.fix(filepath, non_fixable, source, tree)
        assert not success


def test_autofix_follows_closure_reference_into_nested_function() -> None:
    # Regression: renaming only the assignment while leaving a nested
    # function's free-variable reference untouched used to leave the
    # closure reading a name that no longer exists in its enclosing scope
    # (NameError at call time) — ch. 2: "MUST NOT perform an auto-fix that
    # can change runtime behavior"; "MUST ensure that a fix does not change
    # name binding or scope unintentionally".
    source = """def outer(response):
    data = response.json()

    def inner():
        return data

    return inner()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    module_namespace: dict[str, Any] = {}
    # "<forbid_vars_fixture>", not a real path: a filename resolving to a
    # path on disk (e.g. "test.py") makes coverage.py try to trace it as a
    # source file and fail the run.
    exec(compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> dict[str, str]:
            return {"k": "v"}

    assert module_namespace["outer"](FakeResponse()) == {"k": "v"}


def test_autofix_follows_closure_reference_into_lambda() -> None:
    source = """def outer(response):
    data = response.json()
    return lambda: data
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "return lambda: payload" in fixed_content


def test_autofix_follows_closure_reference_into_comprehension() -> None:
    source = """def outer(response, items):
    data = response.json()
    return [str(data) for _ in items]
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    ast.parse(fixed_content)  # Must still be valid Python.


def test_autofix_renames_walrus_target_that_escapes_comprehension_to_outer_scope() -> None:
    # Regression: PEP 572 binds a `:=` target inside a comprehension to the
    # nearest *enclosing* non-comprehension scope, not the comprehension
    # itself — so a walrus target sharing the outer variable's name is the
    # *same* binding, not a shadow of it, and must be renamed along with
    # every other reference. Renaming only the later reference (mistaking
    # the walrus for a comprehension-local shadow) would leave the walrus
    # writing to a stale, now-unrelated "data" while the renamed reference
    # kept the pre-walrus value — a silent change to what's actually
    # returned.
    source = """def outer(response, items):
    data = response.json()
    return [(data := item) for item in items], data
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert fixed_content.count("payload") == 3  # assignment, walrus target, trailing reference


@pytest.mark.parametrize(
    ("source", "expected_snippet"),
    [
        (
            # Regression: a comprehension's *first* `for` clause's iterable
            # is evaluated in the enclosing scope, before the
            # comprehension's own for-target ("data" here) starts shadowing
            # anything — must still be renamed even though the
            # comprehension's own body (also "data") is correctly left
            # alone.
            """def outer(response):
    data = response.json()
    return [data for data in data]
""",
            "return [data for data in payload]",
        ),
        (
            # Branch coverage: a *later* generator's iterable (unlike the
            # first) runs inside the comprehension's own scope — but it's
            # still not shadowed here (the for-targets are x/z, not
            # "data"), so it's an ordinary closure reference.
            """def outer(response, xs):
    data = response.json()
    return [x for x in xs for z in data]
""",
            "for z in payload",
        ),
        (
            # Branch coverage: dict comprehensions have their own
            # key/value/generators shape, distinct from list/set/generator
            # comprehensions.
            """def outer(response, xs):
    data = response.json()
    return {x: data for x in xs}
""",
            "{x: payload for x in xs}",
        ),
        (
            # Regression: a parameter default and a parameter/return
            # annotation are both evaluated at def-time in the enclosing
            # scope (not the function's own body scope) — must be renamed
            # even though the parameter itself ("data") also shadows the
            # name within the function's own body.
            """def outer(response):
    data = response.json()

    def inner(x: data = data) -> data:
        return x

    return inner(), data
""",
            "def inner(x: payload = payload) -> payload:",
        ),
        (
            # Branch coverage: *args/**kwargs annotations go through the
            # same vararg/kwarg-inclusive path as regular parameters.
            """def outer(response):
    data = response.json()

    def inner(*args: data, **kwargs: data):
        return args, kwargs

    return inner(), data
""",
            "def inner(*args: payload, **kwargs: payload):",
        ),
        (
            # Branch coverage: when a function has PEP 695 type parameters
            # but the renamed name isn't one of them, its annotations still
            # move into the type parameters' own implicit scope (unlike a
            # plain function, where they're evaluated in the enclosing
            # scope directly) — but aren't shadowed there either, so they
            # must still be renamed.
            """def outer(response):
    data = response.json()

    def inner[T](x: data) -> T:
        return x

    return inner(1), data
""",
            "def inner[T](x: payload) -> T:",
        ),
        (
            # Branch coverage: a lambda's own default value, distinct code
            # path from a def's.
            """def outer(response):
    data = response.json()
    return lambda x=data: x
""",
            "return lambda x=payload: x",
        ),
        (
            # Branch coverage: a function with type parameters, a
            # parameter annotation, but no *return* annotation at all —
            # distinct from the previous case, which has both.
            """def outer(response):
    data = response.json()

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
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert expected_snippet in fixed_content
    ast.parse(fixed_content)  # Must still be valid Python.


@pytest.mark.parametrize(
    ("source", "shadowed_snippet"),
    [
        (
            # A nested function's own same-named parameter is a distinct
            # binding, not a reference to the outer variable.
            """def outer(response):
    data = response.json()

    def inner(data):
        return data

    return inner("unrelated"), data
""",
            "def inner(data):\n        return data",
        ),
        (
            # A nested function's own local reassignment (not a parameter)
            # is likewise a distinct, shadowing binding.
            """def outer(response):
    data = response.json()

    def inner():
        data = "local"
        return data

    return inner(), data
""",
            'data = "local"\n        return data',
        ),
        (
            # A doubly-nested def/class sharing the outer variable's name
            # shadows it for the rest of the enclosing nested scope too.
            """def outer(response):
    data = response.json()

    def inner():
        def data():
            return 1

        return data()

    return inner(), data
""",
            "def data():\n            return 1",
        ),
        (
            # An import binding the same name inside a nested scope shadows
            # the outer variable the same way an assignment would.
            """def outer(response):
    data = response.json()

    def inner():
        import data

        return data

    return inner(), data
""",
            "import data\n\n        return data",
        ),
        (
            # A comprehension's own `for` target shadows the outer variable
            # for reads inside that comprehension.
            """def outer(response):
    data = response.json()
    return [data for data in range(3)], data
""",
            "[data for data in range(3)]",
        ),
        (
            # Regression: `except E as data:` binds `data` as a plain string
            # (ast.ExceptHandler.name), not an ast.Name node, so it was
            # invisible to the shadow check — `return data` inside the
            # handler was wrongly renamed to `return payload`, silently
            # returning the outer JSON payload instead of the caught
            # exception (syntactically valid, so atomic_write_text()'s
            # compile() check couldn't catch it).
            """def outer(response):
    data = response.json()

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
            # Regression: a match `case data:` capture binds via
            # ast.MatchAs.name, also a plain string, not an ast.Name.
            """def outer(response):
    data = response.json()

    def inner(command):
        match command:
            case data:
                return data

    return inner("x"), data
""",
            "case data:\n                return data",
        ),
        (
            # Regression: a match `case {**rest}:` mapping-rest capture
            # binds via ast.MatchMapping.rest, also a plain string.
            """def outer(response):
    data = response.json()

    def inner(command):
        match command:
            case {**data}:
                return data

    return inner({}), data
""",
            "case {**data}:\n                return data",
        ),
        (
            # Regression: a PEP 695 type parameter (`def f[data]():`) binds
            # via ast.TypeVar.name, also a plain string — and, unlike a
            # regular parameter, type params are accessible at runtime
            # inside the function body too.
            """def outer(response):
    data = response.json()

    def inner[data]() -> data:
        return data

    return inner(), data
""",
            "def inner[data]() -> data:\n        return data",
        ),
        (
            # Regression: `del data` makes `data` local to the *whole*
            # enclosing function (Python's rule for any binding operation,
            # not just assignment) — `del`'s target has ctx=ast.Del, not
            # ast.Store, so the original Store-only check missed it and
            # renamed both the del and the function's own later local
            # reassignment into the outer variable's new name.
            """def outer(response):
    data = response.json()

    def inner():
        del data
        data = "local value"
        return data

    return inner(), data
""",
            'del data\n        data = "local value"\n        return data',
        ),
        (
            # Regression: a dotted `import data.models` (no `as`) binds
            # only the first component, "data", in the local namespace —
            # ast.alias.name is the full dotted path "data.models", which
            # never equals a bare "data", so the original check missed the
            # shadow and renamed `data.models` (a valid attribute access on
            # the imported module) into `payload.models` (nonsensical).
            """def outer(response):
    data = response.json()

    def inner():
        import data.models

        return data.models

    return inner(), data
""",
            "import data.models\n\n        return data.models",
        ),
        (
            # Branch coverage: a non-dotted `from x import data` also
            # shadows via the ast.ImportFrom branch, distinct from the
            # dotted ast.Import case above.
            """def outer(response):
    data = response.json()

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
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert shadowed_snippet in fixed_content
    # The outer occurrence (the trailing `, data` in every case above) must
    # still have been renamed to the .json()-derived suggestion — only the
    # shadowed nested reference is left as "data".
    assert "payload" in fixed_content


def test_autofix_never_offered_for_name_referenced_via_nonlocal() -> None:
    # A nested function's `nonlocal data` declaration means its own `data =
    # "mutated"` Store isn't a shadowing local binding — it mutates the
    # *outer* variable directly. Renaming the outer variable but leaving
    # `nonlocal data` untouched (its name is a plain string, not a
    # rewritable ast.Name) would produce `SyntaxError: no binding for
    # nonlocal 'data' found`. Rather than relying on atomic_write_text()'s
    # compile() check to reject that after the fact, check() itself refuses
    # to suggest a fix at all when it detects `nonlocal`/`global` mentions
    # of the name anywhere in scope (see
    # ForbiddenNameVisitor._referenced_via_global_or_nonlocal) — so the
    # violation is honestly reported as unfixable, not offered and then
    # rejected.
    source = """def outer(response):
    data = response.json()

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
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert violations
        assert all(not v.fixable for v in violations)

        assert check.fix(filepath, violations, source, tree) is False

        fixed_content = filepath.read_text()

    assert fixed_content == source
    ast.parse(fixed_content)  # Left untouched, so it's still valid Python.


def test_autofix_never_offered_for_module_global_read_in_function() -> None:
    # Regression: a module-level `data` read via `global data` inside a
    # function isn't a *new* binding at all — it's the same variable being
    # renamed. `_binds_name_in_nested_scope` used to treat `global data` as
    # shadowing (a blanket, conservative rule that was right for the
    # nonlocal-mutation case but wrong here), so the function's own body
    # was skipped entirely and `return data` was left referencing a name
    # that no longer existed after the module-level rename — a NameError
    # the moment the function is called. Rather than trying to safely
    # follow the reference (impossible: `global data`'s own "data" is a
    # plain string, not a rewritable ast.Name), check() now simply never
    # offers a fix for a name mentioned in any `global`/`nonlocal`
    # anywhere in scope (ch. 2: "MUST NOT perform an auto-fix that can
    # change runtime behavior").
    source = """data = None


def loader(response):
    global data
    data = response.json()


def reader():
    global data
    return data
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert violations
        assert all(not v.fixable for v in violations)

        check.fix(filepath, violations, source, tree)
        fixed_content = filepath.read_text()

    assert fixed_content == source


def test_autofix_avoids_cross_scope_suggestion_collision() -> None:
    # Regression: two *independent* violations in different (but nested,
    # non-shadowing) scopes that happen to generate the same suggested
    # name used to both become that name, colliding once the outer one's
    # rename is followed into the inner scope via closure-following. Here
    # both `data` and the inner `result` match the same `.json()` autofix
    # pattern ("payload") — `return data, result` must not become `return
    # payload, payload`, silently making both returned values identical.
    source = """def outer(response):
    data = response.json()

    def inner(response2):
        result = response2.json()
        return data, result

    return inner(response)
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "result" not in fixed_content
    return_line = next(line for line in fixed_content.splitlines() if line.strip().startswith("return "))
    returned_names = [name.strip() for name in return_line.strip().removeprefix("return ").split(",")]
    assert len(returned_names) == len(set(returned_names)), fixed_content
    ast.parse(fixed_content)  # Must still be valid Python.


def test_autofix_avoids_suggestion_colliding_with_existing_nested_name() -> None:
    # Branch coverage / regression: _get_scope_names() now walks the
    # *entire* subtree (not just the immediate scope) so a suggestion also
    # avoids an already-existing identifier that lives in a nested scope,
    # not just a colliding future suggestion.
    source = """def outer(response):
    data = response.json()

    def inner():
        payload = 5
        return payload, data

    return inner()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "payload = 5" in fixed_content  # inner's own existing name, untouched
    ast.parse(fixed_content)


def test_autofix_avoids_suggestion_colliding_with_nested_parameter_name() -> None:
    # Regression: _get_scope_names() must also see *parameter* names (never
    # `ast.Name` nodes), not just already-bound locals, or a suggestion can
    # collide with a nested function's own parameter and silently rebind a
    # closure read to that parameter instead of the renamed outer variable.
    source = """def outer(response):
    data = response.json()

    def inner(payload):
        return data

    return inner(5)
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "def inner(payload):" in fixed_content  # inner's own parameter, untouched
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "value"

    assert module_namespace["outer"](FakeResponse()) == "value"


def test_autofix_avoids_suggestion_colliding_with_nested_global_declaration() -> None:
    # Regression: a name declared `global`/`nonlocal` in a nested scope is
    # stored as a plain string (`ast.Global.names`), never an `ast.Name`
    # node, so `_get_scope_names()` didn't see it as reserved. A suggestion
    # equal to such a name turned what used to be a closure read into a
    # lookup of the unrelated global/nonlocal binding instead, once the
    # closure-following rename reached that nested scope.
    source = """payload = "module-level unrelated value"

def outer(response):
    data = response.json()

    def inner():
        global payload
        return data

    return inner()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "global payload" in fixed_content  # unrelated global declaration, untouched
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "closure value"

    assert module_namespace["outer"](FakeResponse()) == "closure value"


def test_autofix_renames_walrus_target_inside_default_evaluated_in_enclosing_scope() -> None:
    # Regression: `_binds_name_in_nested_scope()` must scan only the nested
    # function's *own* scope, not its `_outer_scope_children()` (decorators,
    # defaults, annotations without type params) — those run in the
    # *enclosing* scope. A walrus target inside a default value used to be
    # wrongly treated as a body-level shadow, so the default got renamed
    # while the body's closure read was left stale, splitting one variable
    # into two and breaking the fixed code at runtime.
    source = """def outer(response):
    data = response.json()

    def inner(x=(data := response.json())):
        return data, x

    return inner()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "value"

    assert module_namespace["outer"](FakeResponse()) == ("value", "value")


def test_autofix_follows_closure_through_scope_that_itself_contains_a_shadowing_nested_scope() -> None:
    # Regression: `_binds_name_in_nested_scope()` must not descend into a
    # *further*-nested function/lambda/comprehension when checking whether
    # the scope it was actually asked about binds the name. `middle` itself
    # doesn't shadow `data`, but `middle`'s own body contains `deeper`,
    # which does — `_iter_own_scope_descendants()` used to walk straight
    # into `deeper`'s body too, wrongly concluding `middle` itself shadows
    # `data`, and skipping `middle`'s own legitimate closure reference.
    source = """def outer(response):
    data = response.json()

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
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "def deeper():\n            data = " in fixed_content  # deeper's own local, untouched
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "closure value"

    assert module_namespace["outer"](FakeResponse()) == ("closure value", "unrelated local")


def test_autofix_avoids_suggestion_collision_when_nested_closure_precedes_captured_assignment() -> None:
    # Regression: suggestions used to be assigned in AST visit (textual)
    # order, so a nested closure defined *before* the outer variable it
    # will eventually capture (valid Python — closures resolve names at
    # call time) got its own, unrelated violation's suggestion chosen
    # first, unaware of what the outer scope would later pick for the same
    # RHS pattern. assign_suggestions() now processes violations in
    # ascending scope-depth order instead, so the outer scope's own
    # violation is always assigned first regardless of source order.
    source = """def outer(response, response2):
    def inner():
        result = response2.json()
        return data, result

    data = response.json()
    return inner()
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "result" not in fixed_content
    return_line = next(line for line in fixed_content.splitlines() if "return" in line and "," in line)
    returned_names = [name.strip() for name in return_line.split("return", 1)[1].split(",")]
    assert len(returned_names) == len(set(returned_names))  # must stay two distinct names
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def __init__(self, value: str) -> None:
            self._value = value

        def json(self) -> str:
            return self._value

    assert module_namespace["outer"](FakeResponse("outer value"), FakeResponse("inner value")) == (
        "outer value",
        "inner value",
    )


def test_autofix_does_not_rename_annotation_under_deferred_annotations() -> None:
    # Regression: with `from __future__ import annotations` (PEP 563)
    # active, every annotation is stored as a string and resolved later
    # against the function's *module* globals, never the enclosing
    # function's locals — unlike a default value, it is never a true
    # closure reference. Renaming an annotation that happens to share a
    # name with an outer local used to follow it anyway, pointing the
    # (module-global-resolved) annotation at a name that only exists as a
    # local, breaking `typing.get_type_hints()` at runtime.
    source = """from __future__ import annotations

data = int

def outer(response):
    data = response.json()

    def inner(x: data):
        return x

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "def inner(x: data):" in fixed_content  # annotation untouched
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    hints = typing.get_type_hints(inner)
    assert hints == {"x": int}


def test_autofix_still_follows_annotation_closure_without_deferred_annotations() -> None:
    # Without `from __future__ import annotations`, a parameter annotation
    # *is* evaluated eagerly in the enclosing scope (like a default value),
    # so it must still be renamed to follow the closure it actually reads —
    # this is the pre-existing, still-correct behavior the deferred-
    # annotations exclusion above must not disturb.
    source = """def outer(response):
    data = response.json()

    def inner(x: data):
        return x

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "def inner(x: payload):" in fixed_content
    module_namespace: dict[str, Any] = {}
    # dont_inherit=True: this test file's own `from __future__ import
    # annotations` (line 1) would otherwise leak into the compiled fixture
    # regardless of what fixed_content itself contains (compile() inherits
    # __future__ flags from the calling frame by default) — defeating the
    # point of this specific test, which is to exercise the *eager*
    # (non-deferred) annotation path the deferred-annotations exclusion
    # above must leave alone.
    exec(  # noqa: S102
        compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec", dont_inherit=True), module_namespace
    )

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    assert inner.__annotations__["x"] == "runtime value"


def test_autofix_never_offered_for_module_scope_name_referenced_in_annotation() -> None:
    # Regression: under PEP 563, every annotation resolves only against the
    # annotated function's own *module* globals, ignoring any local
    # shadowing along the way — so unlike a nested-local rename (previous
    # two tests), a module-scope rename genuinely *should* propagate into
    # every annotation referencing it, at any nesting depth. Correctly doing
    # that would require an annotation-specific traversal that ignores
    # shadowing entirely (unlike ordinary closure-following), which this
    # codebase doesn't build for such a narrow case — so the violation must
    # not be offered as fixable at all, rather than leaving a stale
    # annotation behind once the module-level binding is renamed out from
    # under it. Two violations (a parameter- and a return-annotation
    # reference) also exercise `_annotation_referenced_names()`'s cache-hit
    # branch and the return-annotation branch of its own walk.
    source = """from __future__ import annotations

class Response:
    def json(self):
        return int

response = Response()
data = response.json()
result = response.json()

def f(x: data) -> result:
    return x
"""
    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2
    assert all(
        violation.message.endswith("Use a more descriptive name. Or add '# pytriage: ignore=TRI001' to suppress.")
        for violation in violations
    )
    assert all(violation.fixable is False for violation in violations)


def test_autofix_follows_closure_into_type_parameter_bound_and_default() -> None:
    # Regression: a PEP 695 type parameter's own `bound`/`default_value`
    # expression is evaluated lazily, but through a real closure over the
    # scope enclosing the `def` — confirmed against CPython to respect
    # ordinary shadowing rules, unlike a deferred annotation (previous
    # test). None of these were visited by the rename traversal at all, so
    # a nested type parameter bound/default referencing an outer local was
    # silently left stale, raising `NameError` the moment it was accessed
    # (e.g. via `__bound__`/`__default__`). Covers all three type parameter
    # kinds (`TypeVar`'s own `bound` and `default_value`, `TypeVarTuple`'s
    # and `ParamSpec`'s own `default_value`) in one fixture; `**Q` (no
    # default at all — a non-default type parameter must precede every
    # defaulted one) is branch coverage for a `ParamSpec`/`TypeVarTuple`
    # with nothing to yield.
    source = """def outer(response):
    data = response.json()

    def inner[**Q, T: data = data, *Ts = data, **P = data]():
        return T, Ts, P, Q

    return inner
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "def inner[**Q, T: payload = payload, *Ts = payload, **P = payload]():" in fixed_content
    module_namespace: dict[str, Any] = {}
    exec(compile(ast.parse(fixed_content), "<forbid_vars_fixture>", "exec"), module_namespace)  # noqa: S102

    class FakeResponse:
        def json(self) -> str:
            return "runtime value"

    inner = module_namespace["outer"](FakeResponse())
    _, type_var, type_var_tuple, param_spec = inner.__type_params__
    assert type_var.__bound__ == "runtime value"
    assert type_var.__default__ == "runtime value"
    assert type_var_tuple.__default__ == "runtime value"
    assert param_spec.__default__ == "runtime value"


def test_scope_names_ignore_unnamed_except_and_match_captures() -> None:
    # Branch coverage: a bare `except:` or wildcard `case _:` produces an
    # ExceptHandler/MatchAs node with `name=None` — `_get_scope_names()`
    # must not treat that as introducing a bound name.
    source = """def outer(response):
    data = response.json()
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
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    ast.parse(fixed_content)


def test_autofix_replaces_all_uses_in_scope() -> None:
    source = """def fetch_users():
    data = response.get()
    print(data)
    result = data.json()
    return result
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        check.fix(filepath, violations, source, tree)

        fixed_content = filepath.read_text()

        assert "data" not in fixed_content or "# pytriage" in fixed_content
        assert "result" not in fixed_content or "# pytriage" in fixed_content
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
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        check.fix(filepath, violations, source, tree)

        fixed_content = filepath.read_text()

    assert "response = requests.get(url)" not in fixed_content
    assert "response := check(x)" in fixed_content


def test_scope_isolation() -> None:
    source = """def func1():
    data = response.get()
    return data

def func2():
    data = file.read()
    return data
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert len(violations) == 2

        check.fix(filepath, violations, source, tree)
        fixed_content = filepath.read_text()

        has_response = "response = response.get()" in fixed_content
        has_response_2 = "response_2 = response.get()" in fixed_content
        assert has_response or has_response_2
        assert "def func1():" in fixed_content
        assert "def func2():" in fixed_content


def test_apply_fixes_second_violation_same_name_reuses_replacement() -> None:
    # Branch coverage: when two violations in the same scope share the
    # same forbidden name, only the first one's suggestion is kept as the
    # replacement for that name.
    source = """def process():
    data = response.get()
    print(data)
    data = other.get()
    print(data)
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert len(violations) == 2

        check.fix(filepath, violations, source, tree)

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content


def test_autofix_replaces_name_on_line_with_non_ascii_text() -> None:
    # Regression: ast.col_offset is a UTF-8 byte offset, not a character
    # offset. Non-ASCII text earlier on the same line as the forbidden
    # name must not throw off the position used to locate and replace it.
    source = """def process():
    label = "café"; data = requests.get(url)
    return data
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "test.py"
        filepath.write_text(source)

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)
        assert len(violations) == 1

        assert check.fix(filepath, violations, source, tree) is True

        fixed_content = filepath.read_text()

    assert "data" not in fixed_content
    assert "response = requests.get(url)" in fixed_content
    assert "return response" in fixed_content


def test_check_reports_character_offset_not_byte_offset_before_multibyte_text() -> None:
    # Regression: ast.col_offset is a UTF-8 *byte* offset, not a character
    # offset -- storing it on Violation.col directly reports a column too
    # far right on any line with non-ASCII text before the violation
    # (ch. 7: "MUST report ... column information accurately"; ch. 20:
    # "MUST handle multibyte Unicode characters correctly"). "café; " is 6
    # characters but 7 UTF-8 bytes ('é' is 2 bytes), so a byte-offset
    # column would over-count "data"'s own position by one.
    source = "café; data = requests.get(url)\n"
    violations = ForbidVarsCheck().check(Path("module.py"), ast.parse(source), source)

    assert len(violations) == 1
    assert violations[0].col == 6


def test_fix_write_failure_returns_false(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = """def process():
    data = requests.get(url)
    return data


def other():
    result = 42
    return result
"""
    # Point at a path inside a directory that doesn't exist so write_text()
    # raises OSError.
    filepath = tmp_path / "missing_dir" / "test.py"

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(filepath, tree, source)
    # "result = 42" has no autofix pattern match, so it's non-fixable —
    # included specifically so the marking loop below has both a fixable
    # and a non-fixable violation to distinguish between.
    assert {v.fixable for v in violations} == {True, False}

    with caplog.at_level("DEBUG"):
        assert check.fix(filepath, violations, source, tree) is False
    # Regression: the write failure must be attributed to the violations it
    # actually affected, not left indistinguishable from "never attempted"
    # — the orchestrator's own report otherwise misleadingly suggests
    # re-running --fix, which would just fail identically again. A
    # non-fixable violation was never part of this attempt at all, so it
    # must be left alone rather than also marked failed.
    for v in violations:
        assert is_fix_failed(v) is v.fixable
    # mark_fix_failed() above already reports this cleanly; a raw traceback
    # on stderr by default would just be redundant noise (ch. 7: "MUST NOT
    # emit uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)
