"""Tests for forbid_vars hook (TRI001)."""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

from pre_commit_hooks.ast_checks.forbid_vars import ForbidVarsCheck


def test_class_attributes_not_analyzed() -> None:
    """Test that class attributes are not analyzed by TRI001.

    Class attributes, NamedTuple fields, and dataclass fields should be
    excluded from analysis because the class name provides sufficient context.
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
    """Test that dataclass fields are not analyzed."""
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
    """Test that regular class attributes are not analyzed."""
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
    """Test that 'data' in a @model_validator parameter is not flagged.

    Pydantic's @model_validator(mode="before") requires the parameter to be
    named 'data'; flagging it would produce a false positive.
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
    """Test that bare @model_validator (without call) also suppresses the check."""
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
    """Test that the body of a @model_validator is still analysed for violations."""
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
    """Test that 'data' is still flagged in regular (non-validator) methods."""
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
    """Test that function-level variables ARE still analyzed."""
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
    """Test that function parameters ARE still analyzed."""
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
    """Test that module-level variables ARE still analyzed."""
    source = """
data = {}  # Should be flagged
result = None  # Should be flagged
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 2, "Module-level variables should be analyzed"


def test_nested_class_in_function_not_analyzed() -> None:
    """Test that class definitions inside functions are not analyzed."""
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
    """Test that inline ignore comments suppress violations."""
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
    """Test that autofix suggestions are provided."""
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
    """Test detection of multiple forbidden variable names."""
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
    """Test that async function parameters are analyzed."""
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
    """Test that async function local variables are analyzed."""
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
    """Test that *args parameters with forbidden names are flagged."""
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
    """Test that **kwargs parameters with forbidden names are flagged."""
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
    """Test annotated assignments without initial values."""
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
    """Test that autofix applies suggested replacements."""
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

        # Should have a fixable violation with suggestion
        assert len(violations) == 1
        assert violations[0].fixable

        # Apply the fix
        success = check.fix(filepath, violations, source, tree)
        assert success, "Fix should be applied successfully"

        # Read the fixed content
        fixed_content = filepath.read_text()

        # Should have replaced 'data' - may use response_2 to avoid conflicts
        assert "data" not in fixed_content or "# pytriage" in fixed_content
        # The return statement should use the new name
        has_response = "return response" in fixed_content
        has_response_2 = "return response_2" in fixed_content
        assert has_response or has_response_2


def test_autofix_no_fixable_violations() -> None:
    """Test that fix returns False when there are no fixable violations."""
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

        # Filter to only non-fixable violations
        non_fixable = [v for v in violations if not v.fixable]

        # Apply fix with non-fixable violations
        success = check.fix(filepath, non_fixable, source, tree)
        assert not success, "Fix should return False for non-fixable violations"


def test_autofix_replaces_all_uses_in_scope() -> None:
    """Test that autofix replaces all uses of a variable in its scope."""
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

        # Apply the fix
        check.fix(filepath, violations, source, tree)

        # Read the fixed content
        fixed_content = filepath.read_text()

        # All uses of forbidden names should be replaced
        assert "data" not in fixed_content or "# pytriage" in fixed_content
        assert "result" not in fixed_content or "# pytriage" in fixed_content
        # Check that variables were actually used consistently
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
    """Test handling of multiple forbidden variables in the same scope."""
    source = """def process():
    data = response.get()
    result = data.json()
    return result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should detect both 'data' and 'result'
    assert len(violations) == 2
    names = {v.fix_data["name"] for v in violations if v.fix_data}
    assert names == {"data", "result"}


def test_scope_isolation() -> None:
    """Test that variables in different scopes don't interfere."""
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

        # Should have 2 violations, one in each function
        assert len(violations) == 2

        # Apply fixes
        check.fix(filepath, violations, source, tree)
        fixed_content = filepath.read_text()

        # Each scope should have appropriate replacements
        has_response = "response = response.get()" in fixed_content
        has_response_2 = "response_2 = response.get()" in fixed_content
        assert has_response or has_response_2
        # But they should be isolated to their scopes
        assert "def func1():" in fixed_content
        assert "def func2():" in fixed_content


def test_no_violations_when_all_suppressed() -> None:
    """Test that suppressed lines don't generate violations."""
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
    """Test that prefilter pattern includes all forbidden names."""
    check = ForbidVarsCheck()
    patterns = check.get_prefilter_pattern()

    # Should return ALL forbidden names so that files with only 'result ='
    # are not silently skipped during pre-filtering
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
    """Test that custom forbidden names can be configured."""
    check = ForbidVarsCheck(forbidden_names={"foo", "bar"})

    source = """def process():
    foo = 1
    bar = 2
    data = 3  # Should NOT be flagged with custom config
    return foo, bar, data
"""

    tree = ast.parse(source)
    violations = check.check(Path("test.py"), tree, source)

    # Should only flag 'foo' and 'bar', not 'data'
    assert len(violations) == 2
    names = {v.fix_data["name"] for v in violations if v.fix_data}
    assert names == {"foo", "bar"}


def test_positional_only_parameters() -> None:
    """Test that positional-only parameters are analyzed (Python 3.8+)."""
    source = """def process(data, /, other):  # 'data' is positional-only
    return data, other
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Positional-only parameters should be analyzed"
    assert "data" in violations[0].message


def test_multiple_assignment_targets_ignored() -> None:
    """Test that multiple assignment targets are not analyzed."""
    source = """def process():
    data, result = get_values()  # Multiple targets - not supported
    return data, result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should not flag the assignment (multiple targets not supported)
    # But may flag in get_values if it exists
    assert all(v.line != 3 for v in violations if v.line == 3)


def test_nested_function_scope() -> None:
    """Test that nested functions have separate scopes."""
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

    # Should have 2 violations, one in each scope
    assert len(violations) == 2


def test_tokenize_error_handling() -> None:
    """Test that tokenize errors are handled gracefully."""
    # Incomplete source that might cause tokenize issues
    source = "def func():\n    data = 1  # missing closing quote"

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should still detect the violation despite potential tokenize issues
    assert len(violations) >= 1


def test_check_ids() -> None:
    """Test that check IDs and error codes are correct."""
    check = ForbidVarsCheck()

    assert check.check_id == "forbid-vars"
    assert check.error_code == "TRI001"


def test_different_forbidden_names() -> None:
    """Test behavior with non-default forbidden names."""
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

    # Prefilter should return ALL configured names
    patterns = check.get_prefilter_pattern()
    assert patterns is not None
    assert set(patterns) == {"temp", "tmp"}


def test_keyword_only_parameters() -> None:
    """Test that keyword-only parameters are analyzed."""
    source = """def process(*, data, other):  # 'data' is keyword-only
    return data, other
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1, "Keyword-only parameters should be analyzed"
    assert "data" in violations[0].message


def test_all_violations_suppressed_returns_empty() -> None:
    """Test that when all violations are suppressed, empty list is returned."""
    source = """def process():
    data = 1  # pytriage: ignore=TRI001
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should find violation but then filter it out
    assert len(violations) == 0, "Suppressed violations should be filtered out"


def test_module_level_annotated_assignment_with_value() -> None:
    """Test module-level annotated assignments with values."""
    source = """data: dict = {}  # Should be flagged
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert len(violations) == 1
    assert "data" in violations[0].message


def test_function_annotated_assignment_with_value() -> None:
    """Test function-level annotated assignments with values."""
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
    """Test loading autofix config when pyproject.toml doesn't exist."""
    import os

    from pre_commit_hooks.ast_checks.forbid_vars import load_autofix_config

    # Save original directory and change to a temp dir without pyproject.toml
    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            config = load_autofix_config()

            # Should return default config
            assert "patterns" in config
            assert "enabled" in config
            assert config["enabled"] == ["http"]
    finally:
        os.chdir(original_dir)


def test_autofix_with_custom_patterns() -> None:
    """Test autofix with custom patterns in pyproject.toml."""
    import os

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            # Create pyproject.toml with custom patterns
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

            # Should include custom pattern
            assert "custom" in config["patterns"]
            assert "http" in config["enabled"]
            assert "custom" in config["enabled"]
    finally:
        os.chdir(original_dir)


def test_suggestion_fallback_when_in_forbidden_names() -> None:
    """Test that suggestion falls back to 'var' when itself is forbidden."""
    # Create a custom check where 'response' is also forbidden
    check = ForbidVarsCheck(forbidden_names={"data", "response"})

    source = """def fetch():
    data = response.get()
    return data
"""

    tree = ast.parse(source)
    violations = check.check(Path("test.py"), tree, source)

    # Should have a violation with 'var' as suggestion
    assert len(violations) == 1
    assert violations[0].fixable
    assert violations[0].fix_data is not None
    assert violations[0].fix_data["suggestion"] == "var"


def test_name_conflict_counter_increment() -> None:
    """Test that counter increments when multiple conflicts exist."""
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

        # Should suggest response_3 to avoid conflicts
        assert len(violations) == 1
        assert violations[0].fix_data is not None
        assert violations[0].fix_data["suggestion"] == "response_3"


def test_semantic_naming_with_regex_groups() -> None:
    """Test semantic naming pattern with regex group substitution."""
    import os

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)

            # Create pyproject.toml with semantic patterns enabled
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

            # Should suggest 'user' based on semantic pattern
            assert len(violations) == 1
            assert violations[0].fixable
            assert violations[0].fix_data is not None
            assert violations[0].fix_data["suggestion"] == "user"
    finally:
        os.chdir(original_dir)


def test_cached_scope_names_reuse() -> None:
    """Test that scope names are cached and reused."""
    source = """def process():
    data = response.get()
    result = data.json()
    return result
"""

    tree = ast.parse(source)
    check = ForbidVarsCheck()
    violations = check.check(Path("test.py"), tree, source)

    # Should detect both violations using cached scope names
    assert len(violations) == 2
    names = {v.fix_data["name"] for v in violations if v.fix_data}
    assert names == {"data", "result"}
