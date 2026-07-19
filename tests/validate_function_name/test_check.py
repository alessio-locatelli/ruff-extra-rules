from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest

import pre_commit_hooks.ast_checks.validate_function_name as module
from pre_commit_hooks.ast_checks._base import FixValidationError, is_fix_errored, is_fix_rejected
from pre_commit_hooks.ast_checks.validate_function_name import ValidateFunctionNameCheck
from tests.factories import ViolationFactory

if TYPE_CHECKING:
    from pathlib import Path

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import Suggestion


def test_check_uses_given_tree_and_source_not_disk(tmp_path: Path) -> None:
    # check() must derive violations from the tree/source CheckOrchestrator
    # hands it, not by independently re-reading the file from disk. The
    # file on disk has no get_ functions at all; the tree/source passed to
    # check() does. If check() ever regresses to re-reading the file
    # itself (as it used to, via analysis.process_file), this would find
    # zero violations instead of one.
    filepath = tmp_path / "mod.py"
    filepath.write_text("x = 1\n")

    source = "def get_data() -> bool:\n    return True\n"

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, ast.parse(source), source)

    assert len(violations) == 1
    assert "get_data" in violations[0].message
    assert "is_data" in violations[0].message


def test_get_prefilter_pattern() -> None:
    assert ValidateFunctionNameCheck().get_prefilter_pattern() == ["def get_"]


def test_fix_with_no_violations_returns_false(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    filepath.write_text("x = 1\n")

    check = ValidateFunctionNameCheck()
    assert check.fix(filepath, [], "x = 1\n", ast.parse("x = 1\n")) is False


@pytest.mark.parametrize(
    "fix_data",
    [None, {"other_key": 1}],
    ids=["no-fix-data", "fix-data-without-suggestion-key"],
)
def test_fix_skips_violation_missing_suggestion(fix_data: dict[str, int] | None, tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    filepath.write_text("def get_data() -> bool:\n    return True\n")

    violation = ViolationFactory.build(check_id="validate-function-name", error_code="TRI004", fix_data=fix_data)

    check = ValidateFunctionNameCheck()
    assert check.fix(filepath, [violation], "x = 1\n", ast.parse("x = 1\n")) is False


def test_fix_applies_safe_suggestion(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    # Marking a violation "fixed" is the orchestrator's responsibility
    # (CheckOrchestrator._apply_fixes), not this check's own fix() — calling
    # fix() directly, as this test does, never sets it.
    assert check.fix(filepath, violations, source, tree) is True
    assert violations[0].fix_data is not None
    assert "def is_data() -> bool:" in filepath.read_text()


def test_fix_skips_unsafe_suggestion(tmp_path: Path) -> None:
    # A suggestion should_autofix rejects (e.g. a method) isn't applied.
    filepath = tmp_path / "mod.py"
    source = 'class Reader:\n    def get_data(self):\n        f = open("f.txt")\n        return f.read()\n'
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    assert check.fix(filepath, violations, source, tree) is False
    assert filepath.read_text() == source


def test_fix_returns_false_when_apply_fix_fails_without_raising(
    tmp_path: Path,
) -> None:
    # apply_fix() can fail internally (e.g. a write error) and simply
    # return False rather than raising; that must not be reported as
    # fixed. The write goes through atomic_write_text's
    # temp-file-then-rename, which only needs the parent directory to be
    # writable (not the target file itself, since rename() doesn't check
    # the destination's permission bits) — so the directory, not the file,
    # has to be read-only to force a write failure here.
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    tmp_path.chmod(0o555)
    try:
        assert check.fix(filepath, violations, source, tree) is False
    finally:
        tmp_path.chmod(0o755)

    assert not (violations[0].fix_data and violations[0].fix_data.get("fixed"))


def test_fix_marks_violation_errored_and_continues_when_apply_fix_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rename that raises something other than FixValidationError is a bug
    # in apply_fix() itself. Must be marked distinctly (mark_fix_errored)
    # so main() reports [FIX ERRORED] with a "this is a bug" hint rather
    # than the ordinary [FIXABLE]/"Run with --fix" — CheckOrchestrator's own
    # equivalent handling in _apply_fixes never even sees this, since it
    # never escapes this method's own try/except.
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    def boom(*_args: object, **_kws: object) -> bool:
        raise RuntimeError("simulated apply_fix failure")

    monkeypatch.setattr(module, "apply_fix", boom)

    assert check.fix(filepath, violations, source, tree) is False
    assert is_fix_errored(violations[0])
    assert not is_fix_rejected(violations[0])
    assert filepath.read_text() == source


def test_fix_marks_violation_rejected_when_apply_fix_raises_fix_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rename that would produce invalid syntax must be reported distinctly
    # from an ordinary internal error, so main() can point the user at
    # filing a bug instead of suggesting --fix will help.
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    def raise_fix_validation_error(*_args: object, **_kws: object) -> bool:
        raise FixValidationError(filepath, SyntaxError("simulated"))

    monkeypatch.setattr(module, "apply_fix", raise_fix_validation_error)

    assert check.fix(filepath, violations, source, tree) is False
    assert is_fix_rejected(violations[0])
    assert filepath.read_text() == source


def test_fix_rejects_only_the_violation_whose_write_produces_invalid_syntax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fix() loops over violations one at a time, re-reading the file
    # between each rename. If a later rename's write is rejected, an
    # earlier one that already committed in this same call must stay —
    # nothing rolls it back.
    filepath = tmp_path / "mod.py"
    source = (
        "def get_config():\n"
        '    with open("config.json") as f:\n'
        "        return f.read()\n"
        "\n\n"
        "def get_active(user: dict) -> bool:\n"
        '    return user.get("status") == "active"\n'
    )
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 2

    original_apply_fix = module.apply_fix

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> bool:
        if suggestion.func_name == "get_active":
            raise FixValidationError(fp, SyntaxError("simulated"))
        return original_apply_fix(fp, suggestion)

    monkeypatch.setattr(module, "apply_fix", flaky_apply_fix)

    assert check.fix(filepath, violations, source, tree) is True

    by_func_name = {v.fix_data["suggestion"].func_name: v for v in violations if v.fix_data}
    fixed_content = filepath.read_text()

    assert "def get_config" not in fixed_content
    assert 'def get_active(user: dict) -> bool:\n    return user.get("status") == "active"\n' in fixed_content
    assert not is_fix_rejected(by_func_name["get_config"])
    assert is_fix_rejected(by_func_name["get_active"])
