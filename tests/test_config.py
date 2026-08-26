from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, NamedTuple

import pytest

from pre_commit_hooks import ruff_extra_rules, ruff_extra_rules_ty
from pre_commit_hooks.ast_checks import ALL_CHECKS
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks._config import discover, resolve
from pre_commit_hooks.ast_checks._options import add_check_arguments
from pre_commit_hooks.ast_checks.meaningless_vars import MeaninglessVarsLevel
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import AggressivenessLevel
from pre_commit_hooks.ast_checks.redundant_dict_get.local import ProofLevel
from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import ConfidenceLevel
from tests._helpers import restricted_permissions

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from pre_commit_hooks.ast_checks._base import ASTCheck

pytestmark = pytest.mark.uses_project_config

_UNSUGGESTABLE = "data = 1\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_config(directory: Path, body: str) -> Path:
    path = directory / "pyproject.toml"
    path.write_text(body)
    return path


class _ExtendSelectCase(NamedTuple):
    config_body: str | None
    extra_args: list[str]
    suppression_code: str
    expected_exit_code: int
    expected_diagnostic_code: str | None


def test_discovery_finds_the_table_in_the_starting_directory(project: Path) -> None:
    path = _write_config(project, "[tool.ruff-extra-rules]\nfix = true\n")

    found = discover(project)

    assert found is not None
    assert found[0] == path
    assert found[1] == {"fix": True}


def test_discovery_walks_past_a_pyproject_without_our_table(project: Path) -> None:
    root_config = _write_config(project, "[tool.ruff-extra-rules]\nfix = true\n")
    package = project / "services" / "api"
    package.mkdir(parents=True)
    _write_config(package, '[project]\nname = "api"\n')

    found = discover(package)

    assert found is not None
    assert found[0] == root_config


def test_discovery_never_climbs_above_the_git_root(tmp_path: Path) -> None:
    _write_config(tmp_path, "[tool.ruff-extra-rules]\nfix = true\n")
    repository = tmp_path / "repository"
    inner = repository / "src"
    inner.mkdir(parents=True)
    (repository / ".git").mkdir()

    assert discover(inner) is None


def test_discovery_returns_nothing_when_no_table_exists_anywhere(project: Path) -> None:
    _write_config(project, '[project]\nname = "thing"\n')

    assert discover(project) is None


def test_discovery_outside_a_git_repository_walks_to_the_filesystem_root(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert discover(nested) is None


def test_an_unreadable_config_file_is_reported_rather_than_skipped(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(project, "[tool.ruff-extra-rules]\nfix = true\n")

    with restricted_permissions(path, 0o000, restore=0o644):
        exit_code = main(["--config", str(path)])

    assert exit_code == 2
    assert "Could not read" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("[tool.ruff-extra-rules\nfix = true\n", "Failed to parse"),
        ("[tool.ruff-extra-rules]\nfxi = true\n", "Unknown field `fxi`"),
        ('[tool.ruff-extra-rules]\nfix = "yes"\n', "expected a boolean"),
        ('[tool.ruff-extra-rules]\nexclude = "a"\n', "expected a list of strings"),
        ('[tool.ruff-extra-rules]\nexclude = ["[bad"]\n', "Invalid file pattern"),
        ("[tool.ruff-extra-rules]\nselect = [1]\n", "expected a list of strings"),
        ("[tool.ruff-extra-rules]\nextend-select = [1]\n", "expected a list of strings"),
        ('[tool.ruff-extra-rules]\nselect = ["nope"]\n', "Unknown check `nope`"),
        ('[tool.ruff-extra-rules]\nextend-select = ["nope"]\n', "Unknown check `nope`"),
        ("[tool.ruff-extra-rules]\nmeaningless-vars = 1\n", "must be a table"),
        ('[tool.ruff-extra-rules.meaningless-vars]\nlvl = "permissive"\n', "Unknown field `lvl`"),
        ('[tool.ruff-extra-rules.meaningless-vars]\nlevel = "loud"\n', "expected one of: `conservative`"),
        ("[tool]\nruff-extra-rules = 1\n", "must be a table"),
    ],
    ids=[
        "malformed-toml",
        "unknown-global-field",
        "non-boolean-fix",
        "non-list-exclude",
        "uncompilable-exclude-pattern",
        "non-string-select-entry",
        "non-string-extend-select-entry",
        "unknown-check-in-select",
        "unknown-check-in-extend-select",
        "non-table-check-section",
        "unknown-option-field",
        "invalid-option-value",
        "non-table-tool-section",
    ],
)
def test_invalid_configuration_exits_two_and_names_its_source(
    project: Path, capsys: pytest.CaptureFixture[str], body: str, needle: str
) -> None:
    path = _write_config(project, body)
    filepath = project / "module.py"
    filepath.write_text("x = 1\n")

    assert main([str(filepath)]) == 2
    err = capsys.readouterr().err
    assert needle in err
    assert str(path) in err


@pytest.mark.parametrize(
    ("body", "argv", "needle"),
    [
        ('[tool.ruff-extra-rules]\nfix = "yes"\n', ["--fix"], "expected a boolean"),
        ('[tool.ruff-extra-rules]\nexclude = "a"\n', ["--exclude", "*.py"], "expected a list of strings"),
        ("[tool.ruff-extra-rules]\nselect = [1]\n", ["--select", "meaningless-vars"], "expected a list of strings"),
        ("[tool.ruff-extra-rules]\nignore = [1]\n", ["--ignore", "meaningless-vars"], "expected a list of strings"),
        ('[tool.ruff-extra-rules]\nselect = ["nope"]\n', ["--select", "meaningless-vars"], "Unknown check `nope`"),
    ],
    ids=["fix", "exclude", "select", "ignore", "unknown-check-in-select"],
)
def test_an_invalid_setting_is_rejected_even_when_the_command_line_overrides_it(
    project: Path, capsys: pytest.CaptureFixture[str], body: str, argv: list[str], needle: str
) -> None:
    path = _write_config(project, body)
    filepath = project / "module.py"
    filepath.write_text("x = 1\n")

    assert main([str(filepath), *argv]) == 2

    err = capsys.readouterr().err
    assert needle in err
    assert str(path) in err


def test_an_invalid_option_value_for_a_sibling_hooks_check_is_rejected(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(project, '[tool.ruff-extra-rules.redundant-type-conversion]\nlevel = "bad"\n')
    filepath = project / "module.py"
    filepath.write_text("x = 1\n")

    assert ruff_extra_rules.main([str(filepath)]) == 2

    err = capsys.readouterr().err
    assert "expected one of: `conservative`" in err
    assert str(path) in err


def test_unknown_field_error_lists_the_valid_ones(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(project, "[tool.ruff-extra-rules]\nfxi = true\n")
    filepath = project / "module.py"
    filepath.write_text("x = 1\n")

    assert main([str(filepath)]) == 2

    err = capsys.readouterr().err
    for valid in (
        "`fix`",
        "`select`",
        "`extend-select`",
        "`ignore`",
        "`exclude`",
        "`per-file-ignores`",
        "`meaningless-vars`",
    ):
        assert valid in err


@pytest.mark.parametrize(
    ("check_id", "option_value", "expected"),
    [
        ("meaningless-vars", "permissive", MeaninglessVarsLevel.PERMISSIVE),
        ("redundant-assignment", "permissive", AggressivenessLevel.PERMISSIVE),
        ("redundant-type-conversion", "permissive", ConfidenceLevel.PERMISSIVE),
        ("redundant-dict-get", "aggressive", ProofLevel.AGGRESSIVE),
        ("meaningless-vars", "conservative", MeaninglessVarsLevel.CONSERVATIVE),
    ],
    ids=["tr1", "tr5", "tr6", "tr9", "explicit-default"],
)
def test_a_checks_option_reaches_its_constructor_from_the_config_file(
    project: Path, check_id: str, option_value: str, expected: object
) -> None:
    _write_config(project, f'[tool.ruff-extra-rules.{check_id}]\nlevel = "{option_value}"\n')
    check_class = next(cls for cls in ALL_CHECKS if cls().check_id == check_id)

    resolved = resolve(
        _parsed([], enabled=[check_class]),
        enabled_check_classes=[check_class],
        all_check_classes=ALL_CHECKS,
    )

    assert resolved.check_kwargs[check_id] == {"level": expected}


def _parsed(argv: list[str], *, enabled: Sequence[type[ASTCheck]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--select", action="append")
    parser.add_argument("--extend-select", action="append")
    parser.add_argument("--ignore", action="append")
    parser.add_argument("--exclude")
    parser.add_argument("--per-file-ignores", action="append")
    parser.add_argument("--config")
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--fix", action="store_true", default=None)
    for check_class in enabled:
        add_check_arguments(parser, check_class().check_id, check_class.OPTIONS)
    return parser.parse_args(argv)


def test_command_line_option_overrides_the_config_file(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(project, '[tool.ruff-extra-rules.meaningless-vars]\nlevel = "conservative"\n')
    filepath = project / "module.py"
    filepath.write_text(_UNSUGGESTABLE)

    assert main([str(filepath), "--select", "meaningless-vars"]) == 0
    assert main([str(filepath), "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]) == 1

    assert "TR1" in capsys.readouterr().err


def test_config_file_option_applies_when_no_flag_is_given(project: Path) -> None:
    _write_config(project, '[tool.ruff-extra-rules.meaningless-vars]\nlevel = "permissive"\n')
    filepath = project / "module.py"
    filepath.write_text(_UNSUGGESTABLE)

    assert main([str(filepath), "--select", "meaningless-vars"]) == 1


@pytest.mark.parametrize(
    "case",
    [
        _ExtendSelectCase('[tool.ruff-extra-rules]\nextend-select = ["unused-pytriage"]\n', [], "TR1", 1, "TR8"),
        _ExtendSelectCase(
            '[tool.ruff-extra-rules]\nselect = ["redundant-assignment"]\nextend-select = ["unused-pytriage"]\n',
            [],
            "TR5",
            1,
            "TR8",
        ),
        _ExtendSelectCase(None, ["--extend-select", "unused-pytriage"], "TR1", 1, "TR8"),
        _ExtendSelectCase(
            '[tool.ruff-extra-rules]\nextend-select = ["unused-pytriage"]\n',
            ["--extend-select", "redundant-assignment"],
            "TR1",
            0,
            None,
        ),
        _ExtendSelectCase(
            '[tool.ruff-extra-rules]\nextend-select = ["unused-pytriage"]\nignore = ["unused-pytriage"]\n',
            [],
            "TR1",
            0,
            None,
        ),
    ],
    ids=[
        "config-default-selection",
        "config-explicit-selection",
        "command-line-default-selection",
        "command-line-replaces-configured-extension",
        "ignore-disables-extended-check",
    ],
)
def test_extend_select_enables_unused_pytriage(
    project: Path, capsys: pytest.CaptureFixture[str], case: _ExtendSelectCase
) -> None:
    if case.config_body is not None:
        _write_config(project, case.config_body)
    filepath = project / "module.py"
    filepath.write_text(f"while True:  # pytriage: {case.suppression_code}\n    break\n")

    assert ruff_extra_rules.main([str(filepath), *case.extra_args, "--no-fix"]) == case.expected_exit_code
    err = capsys.readouterr().err
    if case.expected_diagnostic_code is None:
        assert err == ""
    else:
        assert case.expected_diagnostic_code in err


def test_fix_from_the_config_file_rewrites_the_file(project: Path) -> None:
    _write_config(project, "[tool.ruff-extra-rules]\nfix = true\n")
    filepath = project / "module.py"
    filepath.write_text("def f():\n    value = compute()\n    return value\n")

    main([str(filepath), "--select", "redundant-assignment"])

    assert "return compute()" in filepath.read_text()


def test_no_fix_overrides_fix_from_the_config_file(project: Path) -> None:
    _write_config(project, "[tool.ruff-extra-rules]\nfix = true\n")
    filepath = project / "module.py"
    original = "def f():\n    value = compute()\n    return value\n"
    filepath.write_text(original)

    assert main([str(filepath), "--select", "redundant-assignment", "--no-fix"]) == 1
    assert filepath.read_text() == original


def test_isolated_ignores_the_config_file(project: Path) -> None:
    _write_config(project, '[tool.ruff-extra-rules.meaningless-vars]\nlevel = "permissive"\n')
    filepath = project / "module.py"
    filepath.write_text(_UNSUGGESTABLE)

    assert main([str(filepath), "--isolated", "--select", "meaningless-vars"]) == 0


def test_isolated_and_config_together_are_rejected(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_config(project, "[tool.ruff-extra-rules]\nfix = true\n")

    assert main(["--isolated", "--config", str(path)]) == 2
    assert "cannot be used together" in capsys.readouterr().err


def test_config_flag_uses_the_named_file_without_searching(project: Path) -> None:
    _write_config(project, '[tool.ruff-extra-rules.meaningless-vars]\nlevel = "conservative"\n')
    elsewhere = project / "other"
    elsewhere.mkdir()
    override = _write_config(elsewhere, '[tool.ruff-extra-rules.meaningless-vars]\nlevel = "permissive"\n')
    filepath = project / "module.py"
    filepath.write_text(_UNSUGGESTABLE)

    assert main([str(filepath), "--config", str(override), "--select", "meaningless-vars"]) == 1


def test_a_relative_config_path_still_anchors_exclude_at_that_files_directory(project: Path) -> None:
    _write_config(project, '[tool.ruff-extra-rules]\nfix = true\nexclude = ["vendor/**"]\n')
    vendored = project / "vendor"
    vendored.mkdir()
    excluded = vendored / "module.py"
    original = "def f():\n    value = compute()\n    return value\n"
    excluded.write_text(original)

    exit_code = main([str(excluded), "--config", "pyproject.toml", "--select", "redundant-assignment"])

    assert exit_code == 0
    assert excluded.read_text() == original


@pytest.mark.parametrize(
    ("target", "needle"),
    [("missing.toml", "no such file"), ("bare.toml", "has no `[tool.ruff-extra-rules]` table")],
    ids=["missing-file", "file-without-our-table"],
)
def test_config_flag_rejects_an_unusable_path(
    project: Path, capsys: pytest.CaptureFixture[str], target: str, needle: str
) -> None:
    (project / "bare.toml").write_text('[project]\nname = "thing"\n')

    assert main(["--config", str(project / target)]) == 2
    assert needle in capsys.readouterr().err


def test_selecting_nothing_from_the_config_file_exits_zero(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(project, "[tool.ruff-extra-rules]\nselect = []\n")
    filepath = project / "module.py"
    filepath.write_text(_UNSUGGESTABLE)

    assert main([str(filepath)]) == 0
    assert capsys.readouterr().err == ""


def test_exclude_from_the_config_file_is_anchored_at_the_project_root(project: Path) -> None:
    _write_config(project, '[tool.ruff-extra-rules]\nexclude = ["vendor/**"]\nfix = false\n')
    vendored = project / "vendor" / "nested"
    vendored.mkdir(parents=True)
    excluded = vendored / "module.py"
    excluded.write_text(_UNSUGGESTABLE)
    kept = project / "src"
    kept.mkdir()
    checked = kept / "vendor_lookalike.py"
    checked.write_text(_UNSUGGESTABLE)

    argv = ["--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]

    assert main([str(excluded), *argv]) == 0
    assert main([str(checked), *argv]) == 1


@pytest.mark.parametrize(
    ("entrypoint", "flag"),
    [
        (ruff_extra_rules.main, "--redundant-type-conversion-level"),
        (ruff_extra_rules_ty.main, "--meaningless-vars-level"),
    ],
    ids=["default-hook-takes-ty-option", "ty-hook-takes-default-option"],
)
def test_a_sibling_hooks_option_is_accepted_on_the_command_line(
    entrypoint: Callable[[list[str] | None], int], flag: str
) -> None:
    assert entrypoint(["--isolated", flag, "permissive"]) == 0


def test_a_sibling_hooks_option_does_not_change_this_hooks_own_behaviour(project: Path) -> None:
    filepath = project / "module.py"
    filepath.write_text(_UNSUGGESTABLE)

    exit_code = ruff_extra_rules.main(["--isolated", "--redundant-type-conversion-level", "permissive", str(filepath)])

    assert exit_code == 0


def test_a_sibling_hooks_check_section_is_accepted_but_not_applied(project: Path) -> None:
    _write_config(project, '[tool.ruff-extra-rules.redundant-type-conversion]\nlevel = "permissive"\n')
    filepath = project / "module.py"
    filepath.write_text("x = 1\n")

    assert ruff_extra_rules.main([str(filepath)]) == 0


@pytest.mark.parametrize(
    "entrypoint",
    [ruff_extra_rules.main, ruff_extra_rules_ty.main],
    ids=["default", "ty"],
)
def test_both_published_hooks_read_the_same_table(
    project: Path, capsys: pytest.CaptureFixture[str], entrypoint: Callable[[list[str] | None], int]
) -> None:
    _write_config(project, '[tool.ruff-extra-rules]\nselect = ["not-a-check"]\n')
    filepath = project / "module.py"
    filepath.write_text("x = 1\n")

    assert entrypoint([str(filepath)]) == 2
    assert "Unknown check `not-a-check`" in capsys.readouterr().err


def test_the_cache_directory_is_anchored_at_the_discovered_project_root(project: Path) -> None:
    _write_config(project, "[tool.ruff-extra-rules]\nfix = false\n")
    source = project / "src"
    source.mkdir()
    filepath = source / "module.py"
    filepath.write_text("x = 1\n")

    with pytest.MonkeyPatch.context() as inner:
        inner.chdir(source)
        main([str(filepath), "--select", "meaningless-vars"])

    assert (project / ".cache" / "pre_commit_hooks").is_dir()
    assert not (source / ".cache").exists()


def test_an_uncompilable_exclude_pattern_on_the_command_line_is_rejected(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    filepath = project / "module.py"
    filepath.write_text("x = 1\n")

    assert main([str(filepath), "--exclude", "[bad"]) == 2
    assert "Invalid file pattern" in capsys.readouterr().err
