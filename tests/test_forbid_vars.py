"""Tests for forbid_vars hook (TRI001)."""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

from pre_commit_hooks.ast_checks.forbid_vars import ForbidVarsCheck


def test_class_attributes_not_analyzed() -> None:
    """Class attributes, NamedTuple fields, and dataclass fields are excluded
    from analysis because the class name already provides context.
    """
    source = """
from typing import NamedTuple

class ChosenEdge(NamedTuple):
    from_idx: int
    to_idx: int
    data: str  # Should NOT be flagged - class provides context
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0, (
        "Class attributes should not be analyzed - class name provides context"
    )


def test_dataclass_fields_not_analyzed() -> None:
    source = """
from dataclasses import dataclass

@dataclass
class UserData:
    name: str
    data: dict  # Should NOT be flagged
    result: str  # Should NOT be flagged
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0, "Dataclass fields should not be analyzed"


def test_regular_class_attributes_not_analyzed() -> None:
    source = """
class Config:
    data = {}  # Class attribute - should NOT be flagged
    result = None  # Class attribute - should NOT be flagged
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0, "Regular class attributes should not be analyzed"


def test_pydantic_model_validator_data_param_not_flagged() -> None:
    """Pydantic's @model_validator(mode="before") requires the parameter to
    be named 'data'; flagging it would be a false positive.
    """
    source = """
from pydantic import BaseModel, model_validator
from typing import Any

class Email(BaseModel):

    @model_validator(mode="before")
    @classmethod
    def content_is_provided(cls, data: Any) -> Any:
        return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0


def test_pydantic_model_validator_bare_not_flagged() -> None:
    source = """
from pydantic import BaseModel, model_validator
from typing import Any

class MyModel(BaseModel):

    @model_validator
    @classmethod
    def validate_all(cls, data: Any) -> Any:
        return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0


def test_pydantic_model_validator_body_still_checked() -> None:
    source = """
from pydantic import BaseModel, model_validator
from typing import Any

class MyModel(BaseModel):

    @model_validator(mode="before")
    @classmethod
    def validate_all(cls, data: Any) -> Any:
        result = do_something(data)  # 'result =' in body should still be flagged
        return result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert "result" in violations[0].message


def test_non_validator_method_data_param_still_flagged() -> None:
    source = """
from pydantic import BaseModel

class MyModel(BaseModel):

    def process(self, data: dict) -> dict:
        return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert "data" in violations[0].message


def test_function_variables_are_analyzed() -> None:
    source = """
def process():
    data = {}  # Should be flagged
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Function-level variables should be analyzed"
    assert violations[0].line == 3
    assert "data" in violations[0].message


def test_function_parameters_are_analyzed() -> None:
    source = """
def process(data):  # Should be flagged
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Function parameters should be analyzed"
    assert violations[0].line == 2
    assert "data" in violations[0].message


def test_module_level_variables_are_analyzed() -> None:
    source = """
data = {}  # Should be flagged
result = None  # Should be flagged
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2, "Module-level variables should be analyzed"


def test_nested_class_in_function_not_analyzed() -> None:
    source = """
def create_model():
    class Model:
        data: str  # Should NOT be flagged
    return Model
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0, (
        "Class attributes inside functions should not be analyzed"
    )


def test_inline_ignore_comment() -> None:
    source = """
def process():
    data = {}  # pytriage: ignore=TRI001
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0, "Inline ignore comments should suppress violations"


def test_autofix_suggestion() -> None:
    source = """
def fetch_users():
    data = response.get()  # Should suggest 'response' as name
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert violations[0].fixable, "Violation should be fixable"
    assert "response" in violations[0].message


def test_multiple_forbidden_names() -> None:
    source = """
def process():
    data = {}
    result = None
    return data, result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2, "Both 'data' and 'result' should be flagged"
    names = {v.message.split("'")[1] for v in violations}
    assert names == {"data", "result"}


def test_async_function_parameters() -> None:
    source = """
async def fetch(data):  # Should be flagged
    return await data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Async function parameters should be analyzed"
    assert violations[0].line == 2
    assert "data" in violations[0].message


def test_async_function_variables() -> None:
    source = """
async def fetch():
    result = await some_call()  # Should be flagged
    return result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Async function variables should be analyzed"
    assert "result" in violations[0].message


def test_vararg_parameter() -> None:
    source = """
def process(*data):  # Should be flagged
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "*args parameters should be analyzed"
    assert "data" in violations[0].message


def test_kwarg_parameter() -> None:
    source = """
def process(**data):  # Should be flagged
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "**kwargs parameters should be analyzed"
    assert "data" in violations[0].message


def test_annotated_assignment_without_value() -> None:
    source = """
def process():
    data: dict  # Should be flagged even without value
    return None
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, (
        "Annotated assignments without value should be analyzed"
    )
    assert "data" in violations[0].message


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
        assert success, "Fix should be applied successfully"

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
        assert not success, "Fix should return False for non-fixable violations"


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
    """A suggested name must not collide with a `:=` target bound inside a
    comprehension in the same scope (PEP 572: the walrus target belongs to
    the enclosing scope, not the comprehension's own scope), even though
    the comprehension's own loop variable is correctly invisible to it.
    """
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


def test_multiple_violations_same_scope() -> None:
    source = """def process():
    data = response.get()
    result = data.json()
    return result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2
    names = {v.fix_data["name"] for v in violations if v.fix_data}
    assert names == {"data", "result"}


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


def test_no_violations_when_all_suppressed() -> None:
    source = """def process():
    data = {}  # pytriage: ignore=TRI001
    result = None  # pytriage: ignore=TRI001
    return data, result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0, "All violations should be suppressed"


def test_prefilter_pattern() -> None:
    check = ForbidVarsCheck()
    patterns = check.get_prefilter_pattern()

    # Returns ALL forbidden names so files with only 'result =' aren't
    # silently skipped during pre-filtering.
    assert patterns is not None
    assert "data" in patterns
    assert "result" in patterns


def test_result_variable_detected() -> None:
    """Regression test: files containing only 'result =' must be detected.

    Previously, get_prefilter_pattern() returned only 'data' (the first
    sorted forbidden name), so files with only 'result =' were silently
    skipped by the prefilter and never reached the AST check.
    """
    source = """
def compute():
    result = get_value()
    return result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert "result" in violations[0].message


def test_result_variable_in_class_method_detected() -> None:
    """Regression test: 'result =' inside a class method must be detected.

    Previously, visit_ClassDef() did 'pass', skipping the entire class body
    including all method definitions. Variables like 'result =' inside test
    class methods were silently missed.
    """
    source = """
class TestSomething:
    def test_query(self, conn):
        result = conn.execute("SELECT COUNT(*) FROM t").fetchone()
        assert result is not None
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert "result" in violations[0].message


def test_custom_forbidden_names() -> None:
    check = ForbidVarsCheck(forbidden_names={"foo", "bar"})

    source = """def process():
    foo = 1
    bar = 2
    data = 3  # Should NOT be flagged with custom config
    return foo, bar, data
"""

    tree = ast.parse(source)
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2
    names = {v.fix_data["name"] for v in violations if v.fix_data}
    assert names == {"foo", "bar"}


def test_positional_only_parameters() -> None:
    source = """def process(data, /, other):  # 'data' is positional-only
    return data, other
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Positional-only parameters should be analyzed"
    assert "data" in violations[0].message


def test_multiple_assignment_targets_ignored() -> None:
    source = """def process():
    data, result = get_values()  # Multiple targets - not supported
    return data, result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag the assignment (multiple targets not supported), but
    # may flag in get_values if it exists.
    assert all(v.line != 3 for v in violations if v.line == 3)


def test_nested_function_scope() -> None:
    source = """def outer():
    data = 1  # Should be flagged

    def inner():
        data = 2  # Should be flagged (separate scope)
        return data

    return data + inner()
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2


def test_tokenize_error_handling() -> None:
    # Deliberately malformed so tokenizing may raise partway through.
    source = "def func():\n    data = 1  # missing closing quote"

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) >= 1


def test_check_ids() -> None:
    check = ForbidVarsCheck()

    assert check.check_id == "forbid-vars"
    assert check.error_code == "TRI001"


def test_different_forbidden_names() -> None:
    check = ForbidVarsCheck(forbidden_names={"temp", "tmp"})

    source = """def process():
    data = 1  # Should NOT be flagged
    temp = 2  # Should be flagged
    return data, temp
"""

    tree = ast.parse(source)
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Should only flag the configured names"
    assert "temp" in violations[0].message

    patterns = check.get_prefilter_pattern()
    assert patterns is not None
    assert set(patterns) == {"temp", "tmp"}


def test_keyword_only_parameters() -> None:
    source = """def process(*, data, other):  # 'data' is keyword-only
    return data, other
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Keyword-only parameters should be analyzed"
    assert "data" in violations[0].message


def test_all_violations_suppressed_returns_empty() -> None:
    source = """def process():
    data = 1  # pytriage: ignore=TRI001
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 0, "Suppressed violations should be filtered out"


def test_module_level_annotated_assignment_with_value() -> None:
    source = """data: dict = {}  # Should be flagged
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert "data" in violations[0].message


def test_function_annotated_assignment_with_value() -> None:
    source = """def process():
    data: dict = {}  # Should be flagged with suggestion
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert "data" in violations[0].message


def test_load_autofix_config_without_pyproject() -> None:
    import os

    from pre_commit_hooks.ast_checks.forbid_vars import load_autofix_config

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            config = load_autofix_config()

            assert "patterns" in config
            assert "enabled" in config
            assert config["enabled"] == ["http"]
    finally:
        os.chdir(original_dir)


def test_autofix_with_custom_patterns() -> None:
    import os

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            pyproject = Path("pyproject.toml")
            pyproject.write_text(r"""
[tool.forbid-vars.autofix]
enabled = ["custom", "http"]

[[tool.forbid-vars.autofix.patterns]]
category = "custom"
regex = "\\.fetch\\(.*\\)"
name = "fetched_data"
""")

            from pre_commit_hooks.ast_checks.forbid_vars import load_autofix_config

            config = load_autofix_config()

            assert "custom" in config["patterns"]
            assert "http" in config["enabled"]
            assert "custom" in config["enabled"]
    finally:
        os.chdir(original_dir)


def test_autofix_custom_pattern_without_category_is_skipped() -> None:
    import os

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            pyproject = Path("pyproject.toml")
            pyproject.write_text(r"""
[[tool.forbid-vars.autofix.patterns]]
regex = "\\.fetch\\(.*\\)"
name = "fetched_data"
""")

            from pre_commit_hooks.ast_checks.forbid_vars import load_autofix_config

            config = load_autofix_config()

            assert not any(
                p["name"] == "fetched_data"
                for patterns in config["patterns"].values()
                for p in patterns
            )
    finally:
        os.chdir(original_dir)


def test_autofix_custom_pattern_merges_into_existing_category() -> None:
    import os

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            pyproject = Path("pyproject.toml")
            pyproject.write_text(r"""
[[tool.forbid-vars.autofix.patterns]]
category = "http"
regex = "\\.patch\\(.*\\)"
name = "patched_data"
""")

            from pre_commit_hooks.ast_checks.forbid_vars import load_autofix_config

            config = load_autofix_config()

            assert any(p["name"] == "patched_data" for p in config["patterns"]["http"])
            # Default "http" patterns are still present alongside the custom one
            assert any(p["name"] == "response" for p in config["patterns"]["http"])
    finally:
        os.chdir(original_dir)


def test_suggestion_fallback_when_in_forbidden_names() -> None:
    check = ForbidVarsCheck(forbidden_names={"data", "response"})

    source = """def fetch():
    data = response.get()
    return data
"""

    tree = ast.parse(source)
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert violations[0].fixable
    assert violations[0].fix_data is not None
    assert violations[0].fix_data["suggestion"] == "var"


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

        tree = ast.parse(source)
        check = ForbidVarsCheck()
        violations = check.check(filepath, tree, source)

        assert len(violations) == 1
        assert violations[0].fix_data is not None
        assert violations[0].fix_data["suggestion"] == "response_3"


def test_semantic_naming_with_regex_groups() -> None:
    import os

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            pyproject = Path("pyproject.toml")
            pyproject.write_text("""
[tool.forbid-vars.autofix]
enabled = ["semantic"]
""")

            from pre_commit_hooks.ast_checks.forbid_vars import ForbidVarsCheck

            check = ForbidVarsCheck()

            source = """def process():
    data = get_user()
    return data
"""

            tree = ast.parse(source)
            violations = check.check(Path("test.py"), tree, source)

            assert len(violations) == 1
            assert violations[0].fixable
            assert violations[0].fix_data is not None
            assert violations[0].fix_data["suggestion"] == "user"
    finally:
        os.chdir(original_dir)


def test_cached_scope_names_reuse() -> None:
    source = """def process():
    data = response.get()
    result = data.json()
    return result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2
    names = {v.fix_data["name"] for v in violations if v.fix_data}
    assert names == {"data", "result"}


def test_generate_unique_name_cache_hit_for_repeated_reassignment() -> None:
    """Branch coverage: the second reassignment of the same forbidden name in
    the same scope returns the cached suggestion instead of recomputing it.
    """
    source = """def process():
    data = requests.get()
    print(data)
    data = requests.get()
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2
    suggestions = {v.fix_data["suggestion"] for v in violations if v.fix_data}
    assert suggestions == {"response"}


def test_find_best_match_prefers_higher_specificity() -> None:
    """Branch coverage: when an RHS matches multiple patterns in a category,
    the longer (more specific) regex wins even when checked after a shorter
    one that also matches.
    """
    source = """def process():
    data = requests.get(url).json()
    return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert violations[0].fix_data is not None
    assert violations[0].fix_data["suggestion"] == "response"


def test_semantic_naming_false_when_match_not_on_target_line() -> None:
    """Branch coverage: the regex-group name substitution looks up the match
    on the *target's own* source line. When the matched call lives on a
    different line than the target (e.g. a parenthesized multi-line RHS),
    that lookup finds nothing and the raw group-reference name is kept as-is.
    """
    import os

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            pyproject = Path("pyproject.toml")
            pyproject.write_text("""
[tool.forbid-vars.autofix]
enabled = ["semantic"]
""")

            from pre_commit_hooks.ast_checks.forbid_vars import ForbidVarsCheck

            check = ForbidVarsCheck()

            source = """def process():
    data = (
        get_user()
    )
    return data
"""

            tree = ast.parse(source)
            violations = check.check(Path("test.py"), tree, source)

            assert len(violations) == 1
            assert violations[0].fix_data is not None
            # The regex-group reference couldn't be resolved against the
            # target's own line, so the raw pattern name is used unexpanded.
            assert violations[0].fix_data["suggestion"] == r"\1"
    finally:
        os.chdir(original_dir)


def test_annotated_attribute_assignment_is_not_checked() -> None:
    """Branch coverage: an annotated assignment whose target is an attribute
    (e.g. ``self.data: int = 5``), not a plain name, is skipped entirely —
    only simple-name annotated assignments are analyzed.
    """
    source = """class Foo:
    def __init__(self):
        self.data: int = 5
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert violations == []


def test_model_validator_decorator_skips_arg_check() -> None:
    """A function decorated with an irrelevant decorator is still checked for
    forbidden arg names, while one decorated with ``@model_validator`` (in
    either bare or called form) is exempt.
    """
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

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    flagged_functions = {v.fix_data["name"] for v in violations if v.fix_data}
    assert flagged_functions == {"data"}
    assert len(violations) == 1


def test_async_model_validator_decorator_skips_arg_check() -> None:
    """Same exemption as ``test_model_validator_decorator_skips_arg_check``,
    but for an async function definition.
    """
    source = """class Model:
    @model_validator
    async def bare(data):
        return data
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert violations == []


def test_apply_fixes_second_violation_same_name_reuses_replacement() -> None:
    """Branch coverage: when two violations in the same scope share the same
    forbidden name, only the first one's suggestion is kept as the
    replacement for that name.
    """
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
    """Regression: ast.col_offset is a UTF-8 byte offset, not a character
    offset. Non-ASCII text earlier on the same line as the forbidden name
    must not throw off the position used to locate and replace it.
    """
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


def test_fix_write_failure_returns_false(tmp_path: Path) -> None:
    source = """def process():
    data = requests.get(url)
    return data
"""
    # Point at a path inside a directory that doesn't exist so write_text()
    # raises OSError.
    filepath = tmp_path / "missing_dir" / "test.py"

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(filepath, tree, source)

    assert check.fix(filepath, violations, source, tree) is False
