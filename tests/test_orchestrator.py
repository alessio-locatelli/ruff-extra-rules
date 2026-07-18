"""Tests for CheckOrchestrator (ast_checks/__init__.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pre_commit_hooks import ast_checks
from pre_commit_hooks.ast_checks import (
    ALL_CHECKS,
    CheckOrchestrator,
    filter_excluded_files,
    load_checks,
    main,
)
from pre_commit_hooks.ast_checks._base import Violation
from pre_commit_hooks.ast_checks.excessive_blank_lines import ExcessiveBlankLinesCheck
from pre_commit_hooks.ast_checks.forbid_vars import ForbidVarsCheck
from pre_commit_hooks.ast_checks.redundant_super_init import RedundantSuperInitCheck

if TYPE_CHECKING:
    import argparse
    import ast
    from pathlib import Path

    import pytest


def test_filter_excluded_files_no_patterns_returns_all() -> None:
    files = ["a.py", "b.py"]
    assert filter_excluded_files(files, []) == files


def test_filter_excluded_files_excludes_matching_file() -> None:
    files = ["a.py", "b.py", "migrations/0001_init.py"]
    filtered_files = filter_excluded_files(files, ["migrations/*.py"])
    assert filtered_files == ["a.py", "b.py"]


def test_filter_excluded_files_excludes_matching_parent_dir() -> None:
    files = ["src/main.py", "vendor/lib/thing.py"]
    filtered_files = filter_excluded_files(files, ["vendor"])
    assert filtered_files == ["src/main.py"]


def test_filter_excluded_files_no_match_keeps_file() -> None:
    files = ["src/main.py"]
    assert filter_excluded_files(files, ["nonexistent/*.py"]) == files


def test_process_files_handles_utf8_bom(tmp_path: Path) -> None:
    """A UTF-8 BOM must not make the orchestrator silently skip the file.

    filepath.read_text(encoding="utf-8") decodes a leading BOM as a literal
    U+FEFF character, which ast.parse rejects as a syntax error — reading
    with utf-8-sig strips it transparently instead (and is identical to
    utf-8 for files without one).
    """
    filepath = tmp_path / "with_bom.py"
    filepath.write_bytes(b"\xef\xbb\xbfdata = 1\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert len(violations[str(filepath)]) == 1
    assert violations[str(filepath)][0].error_code == "TRI001"


def test_apply_fixes_handles_utf8_bom(tmp_path: Path) -> None:
    """The re-read before each check's fix() call must also strip a BOM.

    The fixed file keeps its original BOM on write (detected encoding is
    "utf-8-sig", the same encoding used to write back) — reading it back
    with "utf-8-sig" strips it again, same as the original read.
    """
    filepath = tmp_path / "with_bom.py"
    filepath.write_bytes(b"\xef\xbb\xbfdata = requests.get(url)\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])
    fix_data = violations[str(filepath)][0].fix_data

    assert fix_data is not None
    assert fix_data["fixed"] is True
    assert filepath.read_bytes().startswith(b"\xef\xbb\xbf")
    assert filepath.read_text(encoding="utf-8-sig") == "response = requests.get(url)\n"


def test_apply_fixes_recomputes_stale_positions(tmp_path: Path) -> None:
    """A later check's fix() must not use line numbers from before an earlier
    check's fix already rewrote the file in the same --fix run.

    excessive-blank-lines runs (and fixes) before redundant-assignment in
    ALL_CHECKS order. Collapsing the 3 blank lines after the module docstring
    down to 2 removes one line, shifting `x = "foo"`/`print(x)` up by one —
    so if redundant-assignment's fix() were handed the violation positions
    collected before that collapse, it would edit the wrong (now-shifted)
    lines and silently fail to inline `x`.
    """
    filepath = tmp_path / "stale_positions.py"
    filepath.write_text('"""Module docstring."""\n\n\n\ndef func_scope():\n    x = "foo"\n    print(x)\n')

    checks = load_checks(select={"excessive-blank-lines", "redundant-assignment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    redundant_assignment_fixed = any(
        v.check_id == "redundant-assignment" and v.fix_data and v.fix_data.get("fixed")
        for v in violations[str(filepath)]
    )
    assert redundant_assignment_fixed

    file_content = filepath.read_text(encoding="utf-8")
    assert 'x = "foo"' not in file_content
    assert "print(" in file_content
    assert '"foo"' in file_content


def test_fix_honors_pep263_encoding_declaration(tmp_path: Path) -> None:
    """A file with a non-UTF-8 PEP 263 encoding cookie must be read, fixed,
    and written back in its declared encoding, not assumed to be UTF-8.
    """
    source = "# -*- coding: latin-1 -*-\nresult = func(\n    x\n)  # caf\xe9\n"
    filepath = tmp_path / "latin1.py"
    filepath.write_bytes(source.encode("latin-1"))

    checks = load_checks(select={"misplaced-comment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].fix_data == {"fixed": True}
    fixed_content = filepath.read_bytes().decode("latin-1")
    assert "x  # caf\xe9" in fixed_content
    assert ")\n" in fixed_content


def test_fix_preserves_crlf_line_endings(tmp_path: Path) -> None:
    """Lines untouched by a fix must keep their original CRLF endings."""
    filepath = tmp_path / "crlf.py"
    filepath.write_bytes(b"result = func(\r\n    x\r\n)  # comment\r\n\r\nother = 1\r\n")

    checks = load_checks(select={"misplaced-comment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].fix_data == {"fixed": True}
    fixed_content = filepath.read_bytes()
    assert b"\r\nother = 1\r\n" in fixed_content
    assert b"x  # comment" in fixed_content


def test_process_files_empty_filepaths_returns_empty() -> None:
    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    assert orchestrator.process_files([]) == {}


def test_process_files_no_prefilter_pattern_checks_all_files(tmp_path: Path) -> None:
    """ExcessiveBlankLinesCheck has no prefilter pattern (checks everything),
    so with only it enabled, no git-grep pre-filtering happens at all.
    """
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\nimport os\n")

    orchestrator = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].error_code == "TRI002"


def test_process_files_no_candidates_after_prefilter_returns_empty(
    tmp_path: Path,
) -> None:
    """A file that doesn't contain any check's prefilter pattern is dropped
    before parsing, and produces no entry in the result.
    """
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    orchestrator = CheckOrchestrator(checks=[RedundantSuperInitCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}


def test_process_files_second_call_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    first = orchestrator.process_files([str(filepath)])
    assert first[str(filepath)][0].error_code == "TRI001"

    def boom(*_args: object, **_kws: object) -> None:
        raise AssertionError("_check_file should not run on a cache hit")

    monkeypatch.setattr(orchestrator, "_check_file", boom)
    second = orchestrator.process_files([str(filepath)])
    assert second[str(filepath)][0].error_code == "TRI001"


def test_process_files_different_check_set_forces_recheck(tmp_path: Path) -> None:
    """Changing which checks are enabled between runs must invalidate the
    cache entry from a previous run with a different check set.
    """
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\ndata = 1\n")

    forbid_vars_only = CheckOrchestrator(checks=[ForbidVarsCheck()])
    forbid_vars_only.process_files([str(filepath)])

    both_checks = CheckOrchestrator(checks=[ForbidVarsCheck(), ExcessiveBlankLinesCheck()])
    violations = both_checks.process_files([str(filepath)])

    error_codes = {v.error_code for v in violations[str(filepath)]}
    assert error_codes == {"TRI001", "TRI002"}


def test_generate_cache_key_changes_when_source_tree_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: replaces the hand-bumped CACHE_VERSION that a developer
    had to remember to update whenever a check's own code changed (commit
    0e3efba). The cache key must change on its own when the hashed source
    tree changes, without anyone bumping anything.
    """
    fake_root = tmp_path / "pre_commit_hooks"
    fake_root.mkdir()
    (fake_root / "module.py").write_text("x = 1\n")
    monkeypatch.setattr(ast_checks, "_PACKAGE_ROOT", fake_root)

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    key_before = orchestrator._generate_cache_key()

    (fake_root / "module.py").write_text("x = 2\n")
    key_after = orchestrator._generate_cache_key()

    assert key_before != key_after


def test_get_cached_violations_ignores_corrupted_cache_entry(
    tmp_path: Path,
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    orchestrator.cache.set_cached_result(filepath, "ruff-extra-rules", {"violations": [{}]})

    cached_violations = orchestrator._get_cached_violations(filepath)
    assert cached_violations is None


def test_cache_violations_serialization_error_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])

    def boom(*_args: object, **_kws: object) -> None:
        raise TypeError("simulated cache backend failure")

    monkeypatch.setattr(orchestrator.cache, "set_cached_result", boom)

    # Must not raise, just skip caching for this file.
    violations = orchestrator.process_files([str(filepath)])
    assert violations[str(filepath)][0].error_code == "TRI001"


def test_process_files_missing_file_is_skipped(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.py"

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations = orchestrator.process_files([str(missing)])

    assert violations == {}


def test_process_files_bad_encoding_cookie_is_skipped(tmp_path: Path) -> None:
    filepath = tmp_path / "bad_enc.py"
    filepath.write_bytes(b"# -*- coding: totally-bogus-enc -*-\ndata = 1\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}


def test_process_files_undecodable_content_is_skipped(tmp_path: Path) -> None:
    filepath = tmp_path / "bad_decode.py"
    filepath.write_bytes(b"# -*- coding: ascii -*-\nx = 1  # caf\xe9\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}


def test_process_files_invalid_syntax_is_skipped(tmp_path: Path) -> None:
    filepath = tmp_path / "bad_syntax.py"
    filepath.write_text("def foo(:\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}


def test_process_files_check_exception_is_logged_and_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A check that raises must not prevent other checks from running or
    crash the whole file's processing.
    """
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\ndata = 1\n")

    forbid_vars = ForbidVarsCheck()

    def boom(*_args: object, **_kws: object) -> None:
        raise ValueError("simulated check failure")

    monkeypatch.setattr(forbid_vars, "check", boom)

    orchestrator = CheckOrchestrator(checks=[forbid_vars, ExcessiveBlankLinesCheck()])
    violations = orchestrator.process_files([str(filepath)])

    error_codes = {v.error_code for v in violations[str(filepath)]}
    assert error_codes == {"TRI002"}


def test_apply_fixes_skips_check_with_no_fixable_violations(tmp_path: Path) -> None:
    """redundant-super-init never marks violations fixable; when mixed with
    a fixable forbid-vars violation in the same file, its check must be
    skipped in the fix loop rather than attempting (and no-op'ing) a fix.
    """
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "data = requests.get(url)\n"
        "\n\n"
        "class Base:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n\n"
        "class Child(Base):\n"
        "    def __init__(self, **kwargs):\n"
        "        super().__init__(**kwargs)\n"
    )

    checks = load_checks(select={"forbid-vars", "redundant-super-init"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_check = {v.check_id: v for v in violations[str(filepath)]}
    forbid_vars_fix_data = by_check["forbid-vars"].fix_data
    assert forbid_vars_fix_data is not None
    assert forbid_vars_fix_data.get("fixed") is True
    assert by_check["redundant-super-init"].fixable is False


def test_apply_fixes_file_disappears_before_refetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the file can't be re-read inside _apply_fixes (e.g. deleted by a
    concurrent process), that check's fix is skipped rather than crashing.
    """
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()], fix_mode=True)

    original_read = orchestrator._read_source
    calls = {"n": 0}

    def flaky_read(fp: Path) -> tuple[str, str] | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_read(fp)
        return None

    monkeypatch.setattr(orchestrator, "_read_source", flaky_read)

    violations = orchestrator.process_files([str(filepath)])
    v = violations[str(filepath)][0]
    assert not (v.fix_data and v.fix_data.get("fixed"))


def test_apply_fixes_skips_when_recompute_finds_no_fixable_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    forbid_vars = ForbidVarsCheck()
    original_check = forbid_vars.check
    calls = {"n": 0}

    def flaky_check(fp: Path, tree: ast.Module, source: str) -> list[Violation]:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_check(fp, tree, source)
        return []

    monkeypatch.setattr(forbid_vars, "check", flaky_check)

    orchestrator = CheckOrchestrator(checks=[forbid_vars], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])
    v = violations[str(filepath)][0]
    assert not (v.fix_data and v.fix_data.get("fixed"))


def test_apply_fixes_marks_nothing_fixed_when_fix_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    forbid_vars = ForbidVarsCheck()
    monkeypatch.setattr(forbid_vars, "fix", lambda *_a, **_k: False)

    orchestrator = CheckOrchestrator(checks=[forbid_vars], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])
    v = violations[str(filepath)][0]
    assert not (v.fix_data and v.fix_data.get("fixed"))


def test_apply_fixes_exception_in_fix_is_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    forbid_vars = ForbidVarsCheck()

    def boom(*_args: object, **_kws: object) -> bool:
        raise RuntimeError("simulated fix failure")

    monkeypatch.setattr(forbid_vars, "fix", boom)

    orchestrator = CheckOrchestrator(checks=[forbid_vars], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])
    v = violations[str(filepath)][0]
    assert not (v.fix_data and v.fix_data.get("fixed"))


def test_load_checks_explicit_check_args_none_default() -> None:
    """Passing check_args explicitly (not relying on the None default)
    takes the same path as leaving it unset.
    """
    checks = load_checks(select={"forbid-vars"}, check_args={})
    assert len(checks) == 1
    assert checks[0].check_id == "forbid-vars"


def test_load_checks_ignore_set_skips_matching_check() -> None:
    checks = load_checks(ignore={"forbid-vars"})
    check_ids = {c.check_id for c in checks}
    assert "forbid-vars" not in check_ids
    assert len(check_ids) == len(ALL_CHECKS) - 1


def test_load_checks_ignore_composes_with_select() -> None:
    """Regression: `select` used to make `ignore` a no-op entirely (an
    `elif` instead of two independent checks), so `--select`+`--ignore`
    couldn't be combined the way `ruff check --select`/`--ignore` can.
    """
    checks = load_checks(select={"forbid-vars", "redundant-super-init"}, ignore={"forbid-vars"})
    assert {c.check_id for c in checks} == {"redundant-super-init"}


def test_load_checks_check_specific_args_are_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No shipped check currently has a configurable `__init__`, so this
    exercises the generic re-instantiate-with-kwargs branch in `load_checks`
    against a synthetic check rather than a real one.
    """

    class ConfigurableCheck:
        check_id = "configurable"

        def __init__(self, custom: str = "default") -> None:
            self.custom = custom

    monkeypatch.setattr(ast_checks, "ALL_CHECKS", [*ALL_CHECKS, ConfigurableCheck])

    checks = load_checks(
        select={"configurable"},
        check_args={"configurable": {"custom": "custom_value"}},
    )
    assert len(checks) == 1
    check = checks[0]
    assert isinstance(check, ConfigurableCheck)
    assert check.custom == "custom_value"


def test_load_checks_skips_check_whose_init_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenCheck:
        def __init__(self) -> None:
            raise RuntimeError("simulated broken check")

    monkeypatch.setattr(ast_checks, "ALL_CHECKS", [*ALL_CHECKS, BrokenCheck])

    assert len(load_checks()) == len(ALL_CHECKS)


def test_load_checks_skips_check_when_custom_args_raise() -> None:
    checks = load_checks(
        select={"forbid-vars"},
        check_args={"forbid-vars": {"not_a_real_kwarg": 1}},
    )
    assert checks == []


def test_main_list_checks(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list-checks"]) == 0

    out = capsys.readouterr().out
    assert "Available checks:" in out
    assert "forbid-vars: TRI001" in out


def test_main_no_filenames_returns_zero() -> None:
    assert main([]) == 0


def test_main_no_violations_returns_zero(tmp_path: Path) -> None:
    filepath = tmp_path / "clean.py"
    filepath.write_text("x = 1\n")

    assert main([str(filepath)]) == 0


def test_main_reports_non_fixable_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "class Base:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n\n"
        "class Child(Base):\n"
        "    def __init__(self, **kwargs):\n"
        "        super().__init__(**kwargs)\n"
    )

    exit_code = main([str(filepath), "--select", "redundant-super-init"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "TRI003" in err
    assert "[FIXABLE]" not in err
    assert "[FIXED]" not in err
    assert "Run with --fix" not in err


def test_main_reports_fixable_violation_without_fix_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\nprint(data)\n")

    exit_code = main([str(filepath), "--select", "forbid-vars"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIXABLE]" in err
    assert "Run with --fix to inline automatically." in err


def test_main_fix_flag_marks_violation_fixed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\nprint(data)\n")

    exit_code = main([str(filepath), "--select", "forbid-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIXED]" in err
    assert "Run with --fix" not in err


def test_main_exclude_pattern_excludes_all_files_returns_zero(
    tmp_path: Path,
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    exit_code = main([str(filepath), "--exclude", "*.py"])
    assert exit_code == 0


def test_main_check_specific_cli_arg_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No shipped check currently registers its own CLI argument, so this
    exercises main()'s add_cli_arguments -> parse_args -> cli_kwargs_from_args
    -> check_args wiring end-to-end against a synthetic check.
    """

    class ConfigurableCheck:
        check_id = "configurable"
        error_code = "CFG001"

        def __init__(self, marker: str = "default") -> None:
            self.marker = marker

        def get_prefilter_pattern(self) -> list[str] | None:
            return None

        def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
            return [
                Violation(
                    check_id=self.check_id,
                    error_code=self.error_code,
                    line=1,
                    col=0,
                    message=self.marker,
                    fixable=False,
                )
            ]

        @classmethod
        def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--configurable-marker")

        @classmethod
        def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]:
            return {"marker": args.configurable_marker}

    monkeypatch.setattr(ast_checks, "ALL_CHECKS", [*ALL_CHECKS, ConfigurableCheck])

    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    exit_code = main(
        [
            str(filepath),
            "--select",
            "configurable",
            "--configurable-marker",
            "custom-message",
        ]
    )
    assert exit_code == 1

    assert "custom-message" in capsys.readouterr().err


def test_main_unknown_select_check_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    exit_code = main([str(filepath), "--select", "not-a-real-check"])
    assert exit_code == 1

    assert "Unknown checks: not-a-real-check" in capsys.readouterr().err


def test_main_unknown_ignore_check_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    exit_code = main([str(filepath), "--ignore", "not-a-real-check"])
    assert exit_code == 1

    assert "Unknown checks: not-a-real-check" in capsys.readouterr().err


def test_main_ignoring_all_checks_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    all_ids = ",".join(sorted(cls().check_id for cls in ALL_CHECKS))
    exit_code = main([str(filepath), "--ignore", all_ids])
    assert exit_code == 1

    assert "Error: No checks enabled" in capsys.readouterr().err


def test_main_select_and_ignore_compose(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: `--select`+`--ignore` together used to behave like
    `--select` alone, silently dropping `--ignore` (see
    `test_load_checks_ignore_composes_with_select`).
    """
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    exit_code = main(
        [
            str(filepath),
            "--select",
            "forbid-vars,redundant-super-init",
            "--ignore",
            "forbid-vars",
        ]
    )
    assert exit_code == 0

    assert "TRI001" not in capsys.readouterr().err
