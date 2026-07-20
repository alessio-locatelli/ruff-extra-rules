from __future__ import annotations

import ast
import tempfile
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
