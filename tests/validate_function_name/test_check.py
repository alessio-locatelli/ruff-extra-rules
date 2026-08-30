from __future__ import annotations

import ast
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

import pre_commit_hooks.ast_checks.validate_function_name as module
from pre_commit_hooks.ast_checks._base import FixOutcome, FixValidationError
from pre_commit_hooks.ast_checks.validate_function_name import ValidateFunctionNameCheck, autofix
from tests._helpers import raises, restricted_permissions
from tests.factories import ViolationFactory

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import Suggestion


def _repository_with_fixable_target(tmp_path: Path) -> tuple[str, Path, str]:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q"], check=True, cwd=tmp_path)

    filepath = tmp_path / "definitions.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    subprocess.run([git, "add", "definitions.py"], check=True, cwd=tmp_path)
    return git, filepath, source


def _add_external_reference(filepath: Path, git: str, *, tracked: bool = True) -> None:
    (filepath.parent / "consumer.py").write_text("from definitions import get_data\n\nvalue = get_data()\n")
    if tracked:
        subprocess.run([git, "add", "consumer.py"], check=True, cwd=filepath.parent)


def _check_fixability(tmp_path: Path) -> bool:
    source = "def get_data() -> bool:\n    return True\n"
    violations = ValidateFunctionNameCheck().check(tmp_path / "definitions.py", ast.parse(source), source)
    assert len(violations) == 1
    return violations[0].fixable


def test_check_uses_given_tree_and_source_not_disk(tmp_path: Path) -> None:
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


def test_fix_with_no_violations_declines(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    filepath.write_text("x = 1\n")

    check = ValidateFunctionNameCheck()
    assert FixOutcome.APPLIED not in check.fix(filepath, [], "x = 1\n", ast.parse("x = 1\n")).outcomes


@pytest.mark.parametrize(
    "fix_data",
    [None, {"other_key": 1}],
    ids=["no-fix-data", "fix-data-without-suggestion-key"],
)
def test_fix_skips_violation_missing_suggestion(fix_data: dict[str, int] | None, tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    filepath.write_text("def get_data() -> bool:\n    return True\n")

    violation = ViolationFactory.build(check_id="validate-function-name", error_code="TR4", fix_data=fix_data)

    check = ValidateFunctionNameCheck()
    assert FixOutcome.APPLIED not in check.fix(filepath, [violation], "x = 1\n", ast.parse("x = 1\n")).outcomes


def test_fix_applies_safe_suggestion(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    assert FixOutcome.APPLIED in check.fix(filepath, violations, source, tree).outcomes
    assert violations[0].fix_data is not None
    assert "def is_data() -> bool:" in filepath.read_text()


@pytest.mark.parametrize(
    ("decorator", "definition"),
    [
        ("contextmanager", "def get_transaction():"),
        ("asynccontextmanager", "async def get_connection():"),
        ("contextlib.contextmanager", "def get_lock():"),
        ("contextlib.asynccontextmanager", "async def get_session():"),
    ],
    ids=["sync", "async", "qualified-sync", "qualified-async"],
)
def test_context_manager_names_are_not_reported(tmp_path: Path, decorator: str, definition: str) -> None:
    filepath = tmp_path / "mod.py"
    source = (
        f'import contextlib\n\n@{decorator}\n{definition}\n    """Load the managed resource."""\n    yield object()\n'
    )
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)

    assert violations == []
    assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, tree).outcomes
    assert filepath.read_text() == source


def test_sync_lazy_accessor_property_suggestion_is_not_fixable(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    source = (
        "class Backend:\n"
        "    def get_connection(self):\n"
        "        if not self._connection:\n"
        "            self._connection = connect()\n"
        "        return self._connection\n"
    )
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)

    assert len(violations) == 1
    assert violations[0].fixable is False
    assert violations[0].message == (
        "Function 'get_connection' should use @property 'connection' (synchronous lazy accessor)"
    )
    assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, tree).outcomes
    assert filepath.read_text() == source


def test_fix_skips_unsafe_suggestion(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    source = 'class Reader:\n    def get_data(self):\n        f = open("f.txt")\n        return f.read()\n'
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, tree).outcomes
    assert filepath.read_text() == source


def test_check_does_not_mark_unfixable_violation_fixable(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    source = 'class Reader:\n    def get_data(self):\n        f = open("f.txt")\n        return f.read()\n'
    filepath.write_text(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, ast.parse(source), source)

    assert len(violations) == 1
    assert violations[0].fixable is False


def test_check_marks_rename_unfixable_when_git_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(autofix.shutil, "which", lambda _name: None)

    with caplog.at_level("WARNING"):
        assert _check_fixability(tmp_path) is False

    assert "git is unavailable" in caplog.text


def test_check_marks_rename_unfixable_when_repository_root_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(autofix.subprocess, "run", raises(subprocess.SubprocessError, "simulated root lookup failure"))

    with caplog.at_level("WARNING"):
        assert _check_fixability(tmp_path) is False

    assert "Could not safely establish repository scope" in caplog.text


def test_check_marks_rename_unfixable_when_repository_root_lookup_returns_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        autofix.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 2, "", "simulated root lookup error"),
    )

    with caplog.at_level("WARNING"):
        assert _check_fixability(tmp_path) is False

    assert "Could not safely establish repository scope" in caplog.text


def test_check_marks_rename_unfixable_when_repository_reference_search_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail_reference_search(command: Sequence[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, str(tmp_path), "")
        raise subprocess.SubprocessError("simulated reference search failure")

    monkeypatch.setattr(autofix.subprocess, "run", fail_reference_search)

    with caplog.at_level("WARNING"):
        assert _check_fixability(tmp_path) is False

    assert "Could not safely search repository references" in caplog.text


def test_check_marks_rename_unfixable_when_repository_search_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def repository_search_error(command: Sequence[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, str(tmp_path), "")
        return subprocess.CompletedProcess(command, 2, "", "simulated search error")

    monkeypatch.setattr(autofix.subprocess, "run", repository_search_error)

    with caplog.at_level("WARNING"):
        assert _check_fixability(tmp_path) is False

    assert "Could not safely search repository references" in caplog.text


@pytest.mark.parametrize("tracked", [True, False], ids=["tracked", "untracked"])
def test_fix_refuses_a_rename_referenced_by_another_repository_file(tmp_path: Path, *, tracked: bool) -> None:
    git, filepath, source = _repository_with_fixable_target(tmp_path)
    _add_external_reference(filepath, git, tracked=tracked)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, ast.parse(source), source)

    assert len(violations) == 1
    assert violations[0].fixable is False
    assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, ast.parse(source)).outcomes
    assert filepath.read_text() == source


def test_fix_rechecks_repository_references_before_writing(tmp_path: Path) -> None:
    git, filepath, source = _repository_with_fixable_target(tmp_path)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, ast.parse(source), source)
    assert len(violations) == 1
    assert violations[0].fixable is True

    _add_external_reference(filepath, git)

    assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, ast.parse(source)).outcomes
    assert filepath.read_text() == source


def test_fix_returns_a_failure_outcome_when_apply_fix_fails_without_raising(
    tmp_path: Path,
) -> None:
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    with restricted_permissions(tmp_path, 0o555, restore=0o755):
        assert FixOutcome.APPLIED not in check.fix(filepath, violations, source, tree).outcomes

    assert filepath.read_text() == source


def test_fix_marks_violation_errored_and_continues_when_apply_fix_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    monkeypatch.setattr(module, "apply_fix", raises(RuntimeError, "simulated apply_fix failure"))

    with caplog.at_level("DEBUG"):
        fix_result = check.fix(filepath, violations, source, tree)
    assert fix_result.outcomes == (FixOutcome.ERRORED,)
    assert filepath.read_text() == source
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_fix_marks_violation_rejected_when_apply_fix_raises_fix_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    def raise_fix_validation_error(*_args: object, **_kws: object) -> FixOutcome:
        raise FixValidationError(filepath, SyntaxError("simulated"))

    monkeypatch.setattr(module, "apply_fix", raise_fix_validation_error)

    fix_result = check.fix(filepath, violations, source, tree)
    assert fix_result.outcomes == (FixOutcome.REJECTED,)
    assert filepath.read_text() == source


def test_fix_rejects_only_the_violation_whose_write_produces_invalid_syntax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> FixOutcome:
        if suggestion.func_name == "get_active":
            raise FixValidationError(fp, SyntaxError("simulated"))
        return original_apply_fix(fp, suggestion)

    monkeypatch.setattr(module, "apply_fix", flaky_apply_fix)

    fix_result = check.fix(filepath, violations, source, tree)
    assert FixOutcome.APPLIED in fix_result.outcomes

    fixed_content = filepath.read_text()

    assert "def get_config" not in fixed_content
    assert 'def get_active(user: dict) -> bool:\n    return user.get("status") == "active"\n' in fixed_content
    assert fix_result.outcomes == (FixOutcome.APPLIED, FixOutcome.REJECTED)
