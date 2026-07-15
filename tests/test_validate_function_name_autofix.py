"""Tests for validate_function_name autofix (TRI004 --fix).

Regression coverage for the AST-scoped rename: autofix must never touch text
inside string/byte literals or comments, and must never rename identically
named methods on unrelated classes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
    Suggestion,
    process_file,
)
from pre_commit_hooks.ast_checks.validate_function_name.autofix import (
    apply_fix,
    should_autofix,
)


def _suggestion_for(filepath: Path, func_name: str) -> Suggestion:
    suggestions = process_file(filepath)
    matches = [s for s in suggestions if s.func_name == func_name]
    assert matches, f"No suggestion produced for {func_name!r}"
    return matches[0]


def test_apply_fix_does_not_corrupt_unrelated_string_literal(tmp_path: Path) -> None:
    """Renaming a method must not touch an identically-spelled dict key.

    Calls apply_fix directly: should_autofix never routes methods to it (see
    test_should_autofix_rejects_methods), but apply_fix's own scoping must
    still be safe if invoked directly.
    """
    test_file = tmp_path / "reader.py"
    test_file.write_text(
        "class Reader:\n"
        "    def get_data(self):\n"
        '        f = open("f.txt")\n'
        "        return f.read()\n"
        "\n"
        'ROUTES = {"get_data": "/api/legacy-endpoint"}\n'
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    assert f"def {suggestion.suggested_name}(self):" in result
    assert 'ROUTES = {"get_data": "/api/legacy-endpoint"}' in result


def test_apply_fix_does_not_rename_unrelated_class_method(tmp_path: Path) -> None:
    """Renaming Reader.get_data must not touch OtherReader.get_data."""
    test_file = tmp_path / "readers.py"
    test_file.write_text(
        "class Reader:\n"
        "    def get_data(self):\n"
        '        f = open("f.txt")\n'
        "        return f.read()\n"
        "\n"
        "    def use_it(self):\n"
        "        return self.get_data()\n"
        "\n"
        "\n"
        "class OtherReader:\n"
        "    def get_data(self):\n"
        '        return "unrelated"\n'
        "\n"
        "    def use_it(self):\n"
        "        return self.get_data()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"class Reader:\n    def {new_name}(self):" in result
    assert f"return self.{new_name}()" in result
    assert "class OtherReader:\n    def get_data(self):" in result
    assert 'return "unrelated"' in result


def test_should_autofix_rejects_methods(tmp_path: Path) -> None:
    """Methods are never auto-fixed.

    apply_fix can only find self.x/cls.x call sites within the same class
    body, not external calls through a differently-named receiver (e.g.
    reader.get_report() in a free function elsewhere in the file).
    Auto-fixing the definition without being able to find every such call
    site would break real, unrenamed callers, so should_autofix must refuse
    methods outright rather than risk it.
    """
    test_file = tmp_path / "reader.py"
    test_file.write_text(
        "class Reader:\n"
        "    def get_data(self):\n"
        '        f = open("f.txt")\n'
        "        return f.read()\n"
        "\n"
        "\n"
        "def helper(reader):\n"
        "    return reader.get_data()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert should_autofix(test_file, suggestion) is False


def test_apply_fix_renames_recursive_call(tmp_path: Path) -> None:
    """Regression: a recursive call inside the renamed function must not be
    mistaken for a nested definition that shadows the outer function.

    Builds the Suggestion directly: this recursive counter doesn't match any
    of process_file's behavioral heuristics, but apply_fix's own AST-scoping
    logic is what's under test here, independent of suggestion detection.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data(n):\n"
        "    if n <= 0:\n"
        "        return 0\n"
        "    return get_data(n - 1) + 1\n"
    )

    suggestion = Suggestion(
        path=test_file,
        func_name="get_data",
        lineno=1,
        suggested_name="fetch_data",
        reason="test",
    )
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"def {new_name}(n):" in result
    assert f"return {new_name}(n - 1) + 1" in result
    assert "get_data" not in result


def test_apply_fix_updates_call_site_of_nested_target_function(tmp_path: Path) -> None:
    """Regression: renaming a *nested* function must update its own call
    site within the enclosing function.

    The enclosing scope legitimately contains a def matching the target's
    own name (it IS the target), which must not be mistaken for an unrelated
    shadowing definition that would cause the whole scope to be skipped.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def outer():\n"
        "    x = 1\n"
        "\n"
        "    def get_data():\n"
        '        f = open("f.txt")\n'
        "        return f.read()\n"
        "\n"
        "    return get_data() and x\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"    def {new_name}():\n" in result
    assert f"    return {new_name}() and x\n" in result
    assert "get_data" not in result


def test_apply_fix_refuses_when_name_is_rebound(tmp_path: Path) -> None:
    """A reassignment of the function's own name anywhere in scope makes it
    unsafe to trust any Load reference, so apply_fix must refuse entirely.

    `get_data = fake` permanently rebinds the module-level name for the rest
    of the module's runtime lifetime (Python has no block scoping), so a
    later `get_data()` may no longer refer to the function being renamed.
    """
    test_file = tmp_path / "module.py"
    original = (
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n"
        "\n"
        "get_data = None\n"
        "get_data()\n"
    )
    test_file.write_text(original)

    suggestion = Suggestion(
        path=test_file,
        func_name="get_data",
        lineno=1,
        suggested_name="load_data",
        reason="reads data from disk",
    )
    assert apply_fix(test_file, suggestion) is False
    assert test_file.read_text() == original


def test_apply_fix_updates_free_function_call_sites(tmp_path: Path) -> None:
    """Renaming a module-level function updates every call site in the file."""
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return get_data() + get_data()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"def {new_name}():" in result
    assert result.count(f"{new_name}()") == 3  # def + two call sites
    assert "get_data" not in result


def test_apply_fix_leaves_subclass_super_call_untouched(tmp_path: Path) -> None:
    """A subclass's super().get_data() is intentionally left unrenamed.

    Rewriting it would be unsafe: if the subclass overrides get_data,
    self.get_data() elsewhere resolves dynamically to that override, not to
    the base class method being renamed here.
    """
    test_file = tmp_path / "inherit.py"
    test_file.write_text(
        "class Base:\n"
        "    def get_data(self):\n"
        '        f = open("f.txt")\n'
        "        return f.read()\n"
        "\n"
        "\n"
        "class Child(Base):\n"
        "    def get_data(self):\n"
        "        return super().get_data()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"class Base:\n    def {new_name}(self):" in result
    assert "class Child(Base):\n    def get_data(self):" in result
    assert "return super().get_data()" in result


def test_apply_fix_does_not_rename_nested_shadowing_function(tmp_path: Path) -> None:
    """A nested function that redefines the same name shadows the outer one.

    Renaming the outer get_data must not touch the nested get_data's own
    call site inside outer_caller: that call resolves to the nested
    definition, not the module-level function being renamed here.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n"
        "\n"
        "def outer_caller():\n"
        "    def get_data():\n"
        "        return 2\n"
        "\n"
        "    return get_data()\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return get_data() + 1\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert suggestion.lineno == 1
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"def {new_name}():" in result
    assert "    def get_data():\n        return 2\n" in result
    assert "    return get_data()\n" in result
    assert f"    return {new_name}() + 1\n" in result


def test_apply_fix_does_not_rename_parameter_shadowed_call(tmp_path: Path) -> None:
    """A parameter with the same name shadows the outer function for its scope."""
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n"
        "\n"
        "def wrapper(get_data):\n"
        "    return get_data()\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return get_data() + 1\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"def {new_name}():" in result
    assert "def wrapper(get_data):\n    return get_data()\n" in result
    assert f"    return {new_name}() + 1\n" in result


@pytest.mark.parametrize(
    ("suggestion_lineno", "suggestion_func_name"),
    [(1, "get_missing"), (999, "get_data")],
    ids=["unknown-name", "stale-lineno"],
)
def test_apply_fix_returns_false_when_function_not_found(
    tmp_path: Path, suggestion_lineno: int, suggestion_func_name: str
) -> None:
    """A suggestion that no longer matches the source is a safe no-op."""
    test_file = tmp_path / "module.py"
    test_file.write_text("def get_data():\n    return 1\n")

    suggestion = Suggestion(
        path=test_file,
        func_name=suggestion_func_name,
        lineno=suggestion_lineno,
        suggested_name="fetch_data",
        reason="network I/O",
    )
    assert apply_fix(test_file, suggestion) is False
    assert test_file.read_text() == "def get_data():\n    return 1\n"
