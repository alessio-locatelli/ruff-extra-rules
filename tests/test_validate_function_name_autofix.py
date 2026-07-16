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


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "validate_function_name" / "autofix"


@pytest.mark.parametrize(
    "func_name", ["get_config", "get_active", "get_items"], ids=lambda n: n
)
def test_safe_small_fixtures_are_autofixable(func_name: str) -> None:
    filepath = FIXTURES_DIR / "safe_small.py"
    suggestion = _suggestion_for(filepath, func_name)
    assert should_autofix(filepath, suggestion) is True


@pytest.mark.parametrize(
    ("func_name", "lineno"), [("get_value", 4), ("get_result", 14)], ids=lambda v: v
)
def test_unsafe_complex_fixtures_are_not_autofixable(
    func_name: str, lineno: int
) -> None:
    """Neither function matches a naming heuristic strongly enough for
    process_file to suggest a rename, so should_autofix is exercised
    directly with a hand-built Suggestion instead.
    """
    filepath = FIXTURES_DIR / "unsafe_complex.py"
    suggestion = Suggestion(
        path=filepath,
        func_name=func_name,
        lineno=lineno,
        suggested_name=f"renamed_{func_name}",
        reason="test",
    )
    assert should_autofix(filepath, suggestion) is False


def test_unsafe_large_fixture_is_not_autofixable() -> None:
    filepath = FIXTURES_DIR / "unsafe_large.py"
    suggestion = _suggestion_for(filepath, "get_user_data")
    assert should_autofix(filepath, suggestion) is False


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


def test_apply_fix_renames_call_site_on_line_with_non_ascii_text(
    tmp_path: Path,
) -> None:
    """Regression: ast.col_offset is a UTF-8 byte offset, not a character
    offset. Non-ASCII text earlier on a call site's line must not throw off
    the position used to rename it.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n"
        "\n"
        "def caller():\n"
        '    label = "café"; return get_data()\n'
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"def {new_name}():" in result
    assert f"{new_name}()" in result
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


def test_apply_fix_does_not_rename_lambda_parameter_shadowed_reference(
    tmp_path: Path,
) -> None:
    """A lambda parameter with the same name shadows the outer function for
    the lambda's own body, same as a nested function's parameter would.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n\n"
        "def process(items):\n"
        "    return sorted(items, key=lambda get_data: get_data.value)\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"def {new_name}():" in result
    assert "lambda get_data: get_data.value" in result


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


def test_apply_fix_renames_reference_inside_non_shadowing_lambda(
    tmp_path: Path,
) -> None:
    """A lambda whose parameters don't shadow old_name is descended into,
    so a real reference inside its body is still renamed.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n\n"
        "def process(items):\n"
        "    return sorted(items, key=lambda x: get_data().value)\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"def {new_name}():" in result
    assert f"lambda x: {new_name}().value" in result


def test_should_autofix_rejects_low_confidence_suggestion(tmp_path: Path) -> None:
    test_file = tmp_path / "module.py"
    test_file.write_text("def get_data():\n    return 1\n")

    suggestion = Suggestion(
        path=test_file,
        func_name="get_data",
        lineno=1,
        suggested_name="get_data",
        reason="no confident suggestion",
    )
    assert should_autofix(test_file, suggestion) is False


def test_should_autofix_returns_false_on_read_error(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.py"
    suggestion = Suggestion(
        path=missing,
        func_name="get_data",
        lineno=1,
        suggested_name="load_data",
        reason="reads data from disk",
    )
    assert should_autofix(missing, suggestion) is False


def test_should_autofix_returns_false_when_function_not_found(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "module.py"
    test_file.write_text("def get_data():\n    return 1\n")

    suggestion = Suggestion(
        path=test_file,
        func_name="get_missing",
        lineno=1,
        suggested_name="load_missing",
        reason="reads data from disk",
    )
    assert should_autofix(test_file, suggestion) is False


def test_should_autofix_rejects_large_function(tmp_path: Path) -> None:
    """A function with 20+ lines of code (excluding docstring) is too large
    to safely auto-fix.
    """
    test_file = tmp_path / "module.py"
    body_lines = "\n".join(f"    x{i} = {i}" for i in range(20))
    test_file.write_text(f"def get_data():\n{body_lines}\n    return x0\n")

    suggestion = Suggestion(
        path=test_file,
        func_name="get_data",
        lineno=1,
        suggested_name="fetch_data",
        reason="test",
    )
    assert should_autofix(test_file, suggestion) is False


def test_should_autofix_rejects_deeply_nested_function(tmp_path: Path) -> None:
    """A function with control-flow nesting depth > 1 is too complex to
    safely auto-fix.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data(flag, items):\n"
        "    if flag:\n"
        "        for item in items:\n"
        "            if item:\n"
        "                return item\n"
        "    return None\n"
    )

    suggestion = Suggestion(
        path=test_file,
        func_name="get_data",
        lineno=1,
        suggested_name="find_data",
        reason="test",
    )
    assert should_autofix(test_file, suggestion) is False


def test_should_autofix_accepts_small_function_with_docstring(
    tmp_path: Path,
) -> None:
    """A docstring's own lines don't count against the size limit."""
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    """A short docstring\n'
        "    spanning two lines.\n"
        '    """\n'
        '    f = open("f.txt")\n'
        "    return f.read()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert should_autofix(test_file, suggestion) is True


def test_apply_fix_does_not_rename_call_shadowed_by_nested_class(
    tmp_path: Path,
) -> None:
    """A nested class definition with the same name as the function being
    renamed shadows it for the rest of that scope, same as a nested def.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n\n"
        "def caller():\n"
        "    class get_data:\n"
        "        pass\n"
        "    return get_data()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    assert "    class get_data:\n        pass\n    return get_data()\n" in result


def test_apply_fix_does_not_rename_call_shadowed_by_local_import(
    tmp_path: Path,
) -> None:
    """A local import binding the same name shadows the function for the
    rest of that scope, same as an assignment would.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n\n"
        "def caller():\n"
        "    from other_module import get_data\n"
        "    return get_data()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    assert "    from other_module import get_data\n    return get_data()\n" in result


def test_apply_fix_refuses_when_name_rebound_via_import(tmp_path: Path) -> None:
    """An import shadowing the function name anywhere in its own defining
    scope also blocks the rename (not just a plain assignment).
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n\n"
        "from other_module import get_data\n"
        "\n\n"
        "def caller():\n"
        "    return get_data()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion) is False


def test_apply_fix_does_not_rename_call_shadowed_by_nested_async_function(
    tmp_path: Path,
) -> None:
    """A nested async function with the same name shadows the outer
    function for the rest of that scope, same as a sync nested def.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n\n"
        "async def caller():\n"
        "    async def get_data():\n"
        "        return 2\n"
        "    return await get_data()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    assert (
        "    async def get_data():\n        return 2\n    return await get_data()\n"
        in result
    )


def test_apply_fix_returns_false_on_read_error(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.py"
    suggestion = Suggestion(
        path=missing,
        func_name="get_data",
        lineno=1,
        suggested_name="load_data",
        reason="reads data from disk",
    )
    assert apply_fix(missing, suggestion) is False


def test_apply_fix_returns_false_on_syntax_error(tmp_path: Path) -> None:
    test_file = tmp_path / "module.py"
    test_file.write_text("def get_data(:\n")

    suggestion = Suggestion(
        path=test_file,
        func_name="get_data",
        lineno=1,
        suggested_name="load_data",
        reason="reads data from disk",
    )
    assert apply_fix(test_file, suggestion) is False


def test_apply_fix_does_not_rename_call_shadowed_by_nested_assignment(
    tmp_path: Path,
) -> None:
    """A plain local assignment inside a nested function shadows the outer
    function's name for that nested function's own body, same as a
    parameter or nested def/class would.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n\n"
        "def outer():\n"
        "    def inner():\n"
        "        get_data = compute()\n"
        "        return get_data\n"
        "\n"
        "    return inner()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    assert "        get_data = compute()\n        return get_data\n" in result


def test_apply_fix_renames_reference_inside_non_shadowing_async_function(
    tmp_path: Path,
) -> None:
    """A nested async function whose own body doesn't shadow old_name is
    descended into, so a real reference inside it is still renamed.
    """
    test_file = tmp_path / "module.py"
    test_file.write_text(
        "def get_data():\n"
        '    f = open("f.txt")\n'
        "    return f.read()\n"
        "\n\n"
        "async def caller():\n"
        "    async def helper():\n"
        "        return get_data()\n"
        "    return await helper()\n"
    )

    suggestion = _suggestion_for(test_file, "get_data")
    assert apply_fix(test_file, suggestion)

    result = test_file.read_text()
    new_name = suggestion.suggested_name
    assert f"        return {new_name}()\n" in result
