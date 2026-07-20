from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

import pre_commit_hooks.ast_checks.validate_function_name as vfn_module
from pre_commit_hooks import ast_checks
from pre_commit_hooks.ast_checks import (
    ALL_CHECKS,
    CheckOrchestrator,
    expand_directories,
    filter_excluded_files,
    load_checks,
    main,
)
from pre_commit_hooks.ast_checks._base import Violation, atomic_write_text, is_fix_errored, is_fix_rejected, is_fixed
from pre_commit_hooks.ast_checks.excessive_blank_lines import ExcessiveBlankLinesCheck
from pre_commit_hooks.ast_checks.forbid_vars import ForbidVarsCheck
from pre_commit_hooks.ast_checks.redundant_super_init import RedundantSuperInitCheck
from tests.factories import ViolationFactory

if TYPE_CHECKING:
    import argparse
    import ast
    from collections.abc import Callable

    from pre_commit_hooks.ast_checks import ASTCheck
    from pre_commit_hooks.ast_checks.validate_function_name.analysis import Suggestion


@pytest.mark.parametrize(
    ("files", "patterns", "expected"),
    [
        (["a.py", "b.py"], [], ["a.py", "b.py"]),
        (["a.py", "b.py", "migrations/0001_init.py"], ["migrations/*.py"], ["a.py", "b.py"]),
        (["src/main.py", "vendor/lib/thing.py"], ["vendor"], ["src/main.py"]),
        (["src/main.py"], ["nonexistent/*.py"], ["src/main.py"]),
    ],
    ids=["no-patterns-returns-all", "excludes-matching-file", "excludes-matching-parent-dir", "no-match-keeps-file"],
)
def test_filter_excluded_files(files: list[str], patterns: list[str], expected: list[str]) -> None:
    assert filter_excluded_files(files, patterns) == expected


def test_expand_directories_leaves_plain_files_untouched(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    assert expand_directories([str(filepath), "also/does/not/exist.py"]) == [
        str(filepath),
        "also/does/not/exist.py",
    ]


def test_expand_directories_globs_python_files_outside_a_git_repo(tmp_path: Path) -> None:
    # No git repo here, so this exercises the rglob fallback rather than
    # `git ls-files`.
    (tmp_path / "pkg").mkdir()
    py_file = tmp_path / "pkg" / "module.py"
    py_file.write_text("x = 1\n")
    (tmp_path / "pkg" / "notes.txt").write_text("not python\n")

    assert expand_directories([str(tmp_path)]) == [str(py_file.resolve())]


def test_expand_directories_uses_git_ls_files_inside_a_git_repo(tmp_path: Path) -> None:
    # Regression: a directory argument (e.g. `ruff-extra-rules src/`, the
    # form this project's own dev docs use — see AGENTS.md) used to reach
    # CheckOrchestrator.process_files() as a single unexpanded path. Inside
    # a git repo, git grep's own directory pathspec support made it recurse
    # and match files *inside* that directory, but those resolved paths
    # never matched the literal directory path in git_grep_filter's own
    # input map, so every result was silently discarded as unresolvable —
    # the run reported zero violations, exit code 0, having checked
    # nothing. This also proves git-tracked-only, .gitignore-aware
    # expansion: an untracked file under the directory is excluded.
    git = shutil.which("git")
    assert git is not None

    original_dir = Path.cwd()
    try:
        os.chdir(tmp_path)
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        tracked = tmp_path / "tracked.py"
        tracked.write_text("x = 1\n")
        subprocess.run([git, "add", "tracked.py"], check=True, cwd=tmp_path)  # noqa: S603

        (tmp_path / "untracked.py").write_text("y = 1\n")

        assert expand_directories([str(tmp_path)]) == [str(tracked.resolve())]
    finally:
        os.chdir(original_dir)


def test_expand_directories_skips_tracked_file_deleted_from_working_tree(tmp_path: Path) -> None:
    # Regression: `git ls-files` reports the *index*, not the working tree
    # -- a tracked file removed from disk with a plain `rm` (not `git rm`)
    # still shows up in its output even though it no longer exists. A
    # directory scan isn't asking about that specific file by name, so a
    # stale index entry must be dropped silently rather than surfacing a
    # fake "could not be read" error for a file the user never named.
    git = shutil.which("git")
    assert git is not None

    original_dir = Path.cwd()
    try:
        os.chdir(tmp_path)
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        deleted = tmp_path / "deleted.py"
        deleted.write_text("x = 1\n")
        subprocess.run([git, "add", "deleted.py"], check=True, cwd=tmp_path)  # noqa: S603
        deleted.unlink()

        assert expand_directories([str(tmp_path)]) == []
    finally:
        os.chdir(original_dir)


def test_expand_directories_falls_back_when_git_ls_files_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Inside a git repo, but git itself fails (e.g. missing, or the
    # subprocess times out) — falls back to the plain recursive glob
    # instead of propagating the failure.
    (tmp_path / "pkg").mkdir()
    py_file = tmp_path / "pkg" / "module.py"
    py_file.write_text("x = 1\n")

    with mock.patch("subprocess.run") as mock_run, caplog.at_level("DEBUG"):
        mock_run.side_effect = FileNotFoundError("git not found")
        matches = expand_directories([str(tmp_path)])

    assert matches == [str(py_file.resolve())]
    # Regression: this is self-healing (falls back to an equivalent glob
    # scan), so an ERROR-level .exception() call here used to leak a raw
    # traceback onto the user's stderr by default (nothing in this codebase
    # configures logging, so Python's own lastResort handler prints
    # WARNING+ straight to stderr) for a condition nothing actually failed
    # at from the user's perspective.
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_directory_with_no_python_files_returns_zero(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not python\n")

    assert main([str(tmp_path)]) == 0


def test_main_directory_argument_checks_files_inside_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git = shutil.which("git")
    assert git is not None

    original_dir = Path.cwd()
    try:
        os.chdir(tmp_path)
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        filepath = tmp_path / "module.py"
        filepath.write_text("data = 1\n")
        subprocess.run([git, "add", "module.py"], check=True, cwd=tmp_path)  # noqa: S603

        exit_code = main([str(tmp_path), "--select", "forbid-vars"])

        assert exit_code == 1
        assert "TRI001" in capsys.readouterr().err
    finally:
        os.chdir(original_dir)


def test_process_files_handles_utf8_bom(tmp_path: Path) -> None:
    # A UTF-8 BOM must not make the orchestrator silently skip the file.
    # filepath.read_text(encoding="utf-8") decodes a leading BOM as a
    # literal U+FEFF character, which ast.parse rejects as a syntax error —
    # reading with utf-8-sig strips it transparently instead (and is
    # identical to utf-8 for files without one).
    filepath = tmp_path / "with_bom.py"
    filepath.write_bytes(b"\xef\xbb\xbfdata = 1\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert len(violations[str(filepath)]) == 1
    assert violations[str(filepath)][0].error_code == "TRI001"


def test_apply_fixes_handles_utf8_bom(tmp_path: Path) -> None:
    # The re-read before each check's fix() call must also strip a BOM. The
    # fixed file keeps its original BOM on write (detected encoding is
    # "utf-8-sig", the same encoding used to write back) — reading it back
    # with "utf-8-sig" strips it again, same as the original read.
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
    # A later check's fix() must not use line numbers from before an
    # earlier check's fix already rewrote the file in the same --fix run.
    # excessive-blank-lines runs (and fixes) before redundant-assignment in
    # ALL_CHECKS order. Collapsing the 3 blank lines after the module
    # docstring down to 2 removes one line, shifting `x = "foo"`/`print(x)`
    # up by one — so if redundant-assignment's fix() were handed the
    # violation positions collected before that collapse, it would edit
    # the wrong (now-shifted) lines and silently fail to inline `x`.
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


def test_apply_fixes_refreshes_non_fixable_checks_position_too(tmp_path: Path) -> None:
    # Regression: only checks that themselves had a fixable violation this
    # run got their positions recomputed against the file's post-fix state.
    # redundant-super-init is never fixable (RedundantSuperInitCheck.fix()
    # always returns False), so it never participated in that loop -- its
    # reported line stayed frozen at whatever _check_file's original,
    # pre-fix pass saw. redundant-assignment's own fix deletes a whole
    # line above it, shifting every subsequent line up by one; the
    # non-fixable violation's reported line must reflect the file's real,
    # final line, not the stale pre-fix one (ch. 7: "MUST report line and
    # column information accurately when available").
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def compute():\n"
        "    x = 1\n"
        "    return x\n"
        "\n\n"
        "class Base:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n\n"
        "class Child(Base):\n"
        "    def __init__(self, **kwargs):\n"
        "        super().__init__(**kwargs)\n"
    )

    checks = load_checks(select={"redundant-super-init", "redundant-assignment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    final_content = filepath.read_text()
    actual_super_init_line = next(
        i for i, line in enumerate(final_content.splitlines(), start=1) if "def __init__(self, **kwargs)" in line
    )

    super_init_violation = next(v for v in violations[str(filepath)] if v.check_id == "redundant-super-init")
    assert super_init_violation.line == actual_super_init_line


def test_apply_fixes_refreshes_a_participating_checks_own_left_open_violation(tmp_path: Path) -> None:
    # Regression: a check that *did* participate in the fix loop (it had
    # some fixable violation this run) only had its own positions
    # recomputed once, immediately before its own fix() call -- a violation
    # it left open that call (e.g. validate-function-name's should_autofix
    # guard refusing to touch a method, while still renaming an unrelated
    # free function in the same fix() call) never got revisited afterward.
    # redundant-assignment runs *after* validate-function-name in
    # ALL_CHECKS order and deletes a line above the still-open method
    # violation, shifting it down by one -- the final reconciliation pass
    # must catch this even though validate-function-name itself already
    # ran its own fix() this call (ch. 7: "MUST report line and column
    # information accurately when available").
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def get_ready(user: dict) -> bool:\n"
        '    return user.get("status") == "ready"\n'
        "\n\n"
        "def compute():\n"
        "    x = 1\n"
        "    return x\n"
        "\n\n"
        "class Widget:\n"
        "    def get_active(self) -> bool:\n"
        '        return self.status == "active"\n'
    )

    checks = load_checks(select={"validate-function-name", "redundant-assignment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    final_content = filepath.read_text()
    # The free function was renamed; the method (never auto-fixed, per
    # should_autofix's own "methods are never auto-fixed" rule) was left
    # open, and the redundant assignment above it was inlined away.
    assert "is_ready" in final_content
    assert "def get_active(self)" in final_content
    assert "x = 1" not in final_content
    actual_method_line = next(
        i for i, line in enumerate(final_content.splitlines(), start=1) if "def get_active(self)" in line
    )

    method_violation = next(
        v for v in violations[str(filepath)] if v.check_id == "validate-function-name" and "get_active" in v.message
    )
    assert not is_fixed(method_violation)
    assert method_violation.line == actual_method_line


def test_refresh_stale_positions_never_drops_an_unrelated_open_violation_sharing_a_check_id_with_a_terminal_one(
    tmp_path: Path,
) -> None:
    # Regression: a check_id with a rejected/errored/failed entry must be
    # skipped entirely by the reconciliation pass, not just have that one
    # terminal entry protected -- a fresh check() call has no reliable way
    # to distinguish "this is the same still-present violation as the
    # terminal one" from "this is a different, unrelated violation that
    # merely happens to share message text with it" (e.g. two identically
    # named functions in different scopes) without a stable per-violation
    # identity this codebase doesn't have (ch. 34: "MUST prefer a visible
    # failure over a silent incorrect result"). Filtering the fresh result
    # by message alone would have silently dropped the unrelated one
    # instead of just leaving its position stale.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    # Same message on purpose -- e.g. two identically-named functions in
    # different scopes would produce identical violation text.
    shared_message = "Forbidden variable name 'data' found. Consider renaming to 'response'."
    terminal = ViolationFactory.build(
        check_id="forbid-vars", message=shared_message, fixable=True, fix_data={"fix_failed": True}
    )
    open_violation = ViolationFactory.build(check_id="forbid-vars", message=shared_message, fixable=True, fix_data=None)
    violations = [terminal, open_violation]

    orchestrator._refresh_stale_positions(filepath, violations)

    # Both entries survive untouched -- neither was silently dropped.
    assert violations == [terminal, open_violation]


def test_refresh_stale_positions_returns_when_final_read_fails(tmp_path: Path) -> None:
    # The file existed a moment ago (this same _apply_fixes call already
    # read it successfully), so a failure here means something changed
    # concurrently -- conservatively leave `violations` untouched rather
    # than guess.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")
    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations: list[Violation] = []

    filepath.unlink()
    orchestrator._refresh_stale_positions(filepath, violations)

    assert violations == []


def test_refresh_stale_positions_returns_when_final_parse_fails(tmp_path: Path) -> None:
    # A syntax error in the file's final state (e.g. an external edit
    # racing with this run) must not crash the reconciliation pass.
    filepath = tmp_path / "module.py"
    filepath.write_text("def broken(:\n")
    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations: list[Violation] = []

    orchestrator._refresh_stale_positions(filepath, violations)

    assert violations == []


def test_refresh_stale_positions_records_rule_failure_when_check_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A check crashing during this final reconciliation pass must be
    # isolated (ch. 5) like every other check failure, not left to crash
    # the whole run or silently leave the check's own stale entries in
    # `violations`.
    def boom(*_args: object, **_kwargs: object) -> list[Violation]:
        msg = "simulated check failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(ForbidVarsCheck, "check", boom)

    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")
    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    # A still-open (unmarked) violation for this check_id, so there's
    # something for the reconciliation pass to actually attempt refreshing.
    stale_violation = ViolationFactory.build(check_id="forbid-vars", fixable=True, fix_data=None)
    violations = [stale_violation]

    orchestrator._refresh_stale_positions(filepath, violations)

    assert (str(filepath), "forbid-vars") in orchestrator.rule_failures
    # Left exactly as it was -- the check crashed before it could report
    # anything current to replace it with.
    assert violations == [stale_violation]


def test_fix_honors_pep263_encoding_declaration(tmp_path: Path) -> None:
    # A file with a non-UTF-8 PEP 263 encoding cookie must be read, fixed,
    # and written back in its declared encoding, not assumed to be UTF-8.
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
    # Lines untouched by a fix must keep their original CRLF endings.
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
    # ExcessiveBlankLinesCheck has no prefilter pattern (checks
    # everything), so with only it enabled, no git-grep pre-filtering
    # happens at all.
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\nimport os\n")

    orchestrator = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].error_code == "TRI002"


def test_process_files_no_candidates_after_prefilter_returns_empty(
    tmp_path: Path,
) -> None:
    # A file that doesn't contain any check's prefilter pattern is dropped
    # before parsing, and produces no entry in the result.
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    orchestrator = CheckOrchestrator(checks=[RedundantSuperInitCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}


@pytest.mark.parametrize(
    "write_file",
    [
        None,
        lambda p: p.write_bytes(b"# -*- coding: totally-bogus-enc -*-\ndata = 1\n"),
        lambda p: p.write_bytes(b"# -*- coding: ascii -*-\nx = 1  # caf\xe9\n"),
        lambda p: p.write_text("def foo(:\n"),
    ],
    ids=["missing-file", "bad-encoding-cookie", "undecodable-content", "invalid-syntax"],
)
def test_process_files_unreadable_file_is_skipped(tmp_path: Path, write_file: Callable[[Path], None] | None) -> None:
    filepath = tmp_path / "module.py"
    if write_file is not None:
        write_file(filepath)

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}


def test_process_files_records_unprocessable_file(tmp_path: Path) -> None:
    # Regression: a file _check_file() can't parse used to vanish from the
    # result with no trace at all — indistinguishable from a clean file with
    # zero violations. ExcessiveBlankLinesCheck has no prefilter pattern (see
    # its get_prefilter_pattern()), so the file reaches _check_file()
    # regardless of its content.
    filepath = tmp_path / "module.py"
    filepath.write_text("def foo(:\n")

    orchestrator = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}
    assert orchestrator.unprocessable_files == [str(filepath)]


def test_process_files_resets_unprocessable_files_between_calls(tmp_path: Path) -> None:
    # A file that failed to parse on the first call must not keep showing up
    # as unprocessable on a later call where it isn't even part of the input.
    bad_filepath = tmp_path / "bad.py"
    bad_filepath.write_text("def foo(:\n")
    good_filepath = tmp_path / "good.py"
    good_filepath.write_text("x = 1\n")

    orchestrator = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()])
    orchestrator.process_files([str(bad_filepath)])
    assert orchestrator.unprocessable_files == [str(bad_filepath)]

    orchestrator.process_files([str(good_filepath)])
    assert orchestrator.unprocessable_files == []


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
    # Changing which checks are enabled between runs must invalidate the
    # cache entry from a previous run with a different check set.
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\ndata = 1\n")

    forbid_vars_only = CheckOrchestrator(checks=[ForbidVarsCheck()])
    forbid_vars_only.process_files([str(filepath)])

    both_checks = CheckOrchestrator(checks=[ForbidVarsCheck(), ExcessiveBlankLinesCheck()])
    violations = both_checks.process_files([str(filepath)])

    error_codes = {v.error_code for v in violations[str(filepath)]}
    assert error_codes == {"TRI001", "TRI002"}


def test_generate_cache_key_changes_when_source_tree_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: replaces the hand-bumped CACHE_VERSION that a developer
    # had to remember to update whenever a check's own code changed
    # (commit 0e3efba). The cache key must change on its own when the
    # hashed source tree changes, without anyone bumping anything.
    fake_root = tmp_path / "pre_commit_hooks"
    fake_root.mkdir()
    (fake_root / "module.py").write_text("x = 1\n")
    monkeypatch.setattr(ast_checks, "_PACKAGE_ROOT", fake_root)

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    key_before = orchestrator._generate_cache_key()

    (fake_root / "module.py").write_text("x = 2\n")
    key_after = orchestrator._generate_cache_key()

    assert key_before != key_after


def test_generate_cache_key_changes_when_python_version_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    # ast.parse()'s output for identical source isn't guaranteed stable
    # across Python minor versions, so a .cache directory shared across an
    # interpreter upgrade must not silently reuse the old interpreter's
    # results. Patches the `sys` name binding inside the ast_checks module
    # (not the real global sys module) so only _generate_cache_key() sees a
    # different version.
    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    key_before = orchestrator._generate_cache_key()

    fake_sys = types.SimpleNamespace(
        version_info=types.SimpleNamespace(major=sys.version_info.major, minor=sys.version_info.minor + 1)
    )
    monkeypatch.setattr(ast_checks, "sys", fake_sys)
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


def test_process_files_check_exception_is_logged_and_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A check that raises must not prevent other checks from running or
    # crash the whole file's processing.
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


def test_process_files_check_exception_records_rule_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: a check whose check() raises used to leave no trace at all
    # outside a debug log line — indistinguishable from that check having
    # run cleanly and found nothing. If it's the only check enabled, this
    # used to make the whole file (and the whole run) look completely clean.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    forbid_vars = ForbidVarsCheck()

    def boom(*_args: object, **_kwargs: object) -> list[Violation]:
        raise ValueError("simulated check failure")

    monkeypatch.setattr(forbid_vars, "check", boom)

    orchestrator = CheckOrchestrator(checks=[forbid_vars])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}
    assert orchestrator.rule_failures == [(str(filepath), "forbid-vars")]


def test_process_files_rule_failure_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A result collected while a check crashed must never be cached: caching
    # it would let a later run's cache hit keep serving the crash's "empty"
    # result forever (until the tree hash changes), rather than actually
    # retrying the check.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    forbid_vars = ForbidVarsCheck()
    original_check = forbid_vars.check
    calls = {"n": 0}

    def flaky_check(fp: Path, tree: ast.Module, source: str) -> list[Violation]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("simulated check failure")
        return original_check(fp, tree, source)

    monkeypatch.setattr(forbid_vars, "check", flaky_check)

    orchestrator = CheckOrchestrator(checks=[forbid_vars])
    first = orchestrator.process_files([str(filepath)])
    assert first == {}
    assert orchestrator.rule_failures == [(str(filepath), "forbid-vars")]

    second = orchestrator.process_files([str(filepath)])
    assert second[str(filepath)][0].error_code == "TRI001"


def test_apply_fixes_skips_check_with_no_fixable_violations(tmp_path: Path) -> None:
    # redundant-super-init never marks violations fixable; when mixed with
    # a fixable forbid-vars violation in the same file, its check must be
    # skipped in the fix loop rather than attempting (and no-op'ing) a fix.
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


def test_apply_fixes_does_not_mark_fixed_a_violation_fix_left_untouched(tmp_path: Path) -> None:
    # Regression: validate-function-name's fix() loops over violations and
    # can skip some (should_autofix refuses to rename methods) while fixing
    # others in the same call. _apply_fixes used to mark every violation of
    # a check as fixed whenever fix() returned True at all, regardless of
    # whether that specific violation was actually touched — a rename that
    # should_autofix skipped was reported [FIXED] even though the file
    # still had the old name.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import json\n\n\n"
        "def get_config():\n"
        '    with open("config.json") as f:\n'
        "        return json.load(f)\n\n\n"
        "class Reader:\n"
        "    def get_data(self):\n"
        '        f = open("f.txt")\n'
        "        return f.read()\n"
    )

    checks = load_checks(select={"validate-function-name"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_func_name = {v.fix_data["suggestion"].func_name: v for v in violations[str(filepath)] if v.fix_data}

    get_config_fix_data = by_func_name["get_config"].fix_data
    get_data_fix_data = by_func_name["get_data"].fix_data
    assert get_config_fix_data is not None
    assert get_data_fix_data is not None
    assert get_config_fix_data["fixed"] is True
    assert not get_data_fix_data.get("fixed")
    fixed_content = filepath.read_text()
    assert "def get_config" not in fixed_content
    assert "def get_data(self):" in fixed_content


def test_apply_fixes_distinguishes_violations_with_identical_messages(tmp_path: Path) -> None:
    # Regression: a free function and an unrelated method can produce
    # byte-identical violation messages (same func_name, same suggested
    # name, same reason). Marking "fixed" by message text alone would lose
    # their identity — after the free function is renamed, its own old
    # message is still "present" via the method's untouched violation, so
    # the fixed one must not be reported as still-fixable just because
    # something with the same text remains.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def get_data():\n"
        '    with open("f.txt") as f:\n'
        "        return f.read()\n"
        "\n\n"
        "class Reader:\n"
        "    def get_data(self):\n"
        '        f = open("g.txt")\n'
        "        return f.read()\n"
    )

    checks = load_checks(select={"validate-function-name"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_line = {v.line: v for v in violations[str(filepath)]}
    assert by_line[1].message == by_line[7].message

    free_function_fix_data = by_line[1].fix_data
    method_fix_data = by_line[7].fix_data
    assert free_function_fix_data is not None
    assert method_fix_data is not None
    assert free_function_fix_data.get("fixed") is True
    assert not method_fix_data.get("fixed")
    assert "def load_data():" in filepath.read_text()
    assert "def get_data(self):" in filepath.read_text()


def test_apply_fixes_marks_violation_rejected_when_fix_produces_invalid_syntax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulates a bug in a check's fix logic that would write invalid
    # syntax: atomic_write_text() must refuse the write, and only that
    # check's own violation is reported as rejected — an unrelated check's
    # violation in the same file must still be fixed normally, matching the
    # existing per-check try/except isolation (one check's bad fix doesn't
    # block another's unrelated one).
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\ndata = requests.get(url)\n")

    forbid_vars = ForbidVarsCheck()

    def broken_fix(fp: Path, *_args: object, **_kwargs: object) -> None:
        atomic_write_text(fp, "def broken(:\n", "utf-8")

    monkeypatch.setattr(forbid_vars, "fix", broken_fix)

    checks: list[ASTCheck] = [forbid_vars, ExcessiveBlankLinesCheck()]
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_check = {v.check_id: v for v in violations[str(filepath)]}
    forbid_vars_violation = by_check["forbid-vars"]
    blank_lines_violation = by_check["excessive-blank-lines"]

    assert is_fix_rejected(forbid_vars_violation)
    assert not (forbid_vars_violation.fix_data and forbid_vars_violation.fix_data.get("fixed"))
    assert not is_fix_rejected(blank_lines_violation)
    assert blank_lines_violation.fix_data == {"fixed": True}
    assert "data = requests.get(url)" in filepath.read_text()


def test_apply_fixes_marks_violation_errored_when_fix_raises_unexpectedly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bug in a check's own fix() (raising something other than
    # FixValidationError) must be marked distinctly from a rejected fix:
    # fix() never even produced output to validate here, so this isn't
    # "atomic_write_text() refused a bad write" but "the check's fix logic
    # itself blew up." An unrelated check's fix in the same file must still
    # apply normally.
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\ndata = requests.get(url)\n")

    forbid_vars = ForbidVarsCheck()

    def broken_fix(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated fix bug")

    monkeypatch.setattr(forbid_vars, "fix", broken_fix)

    checks: list[ASTCheck] = [forbid_vars, ExcessiveBlankLinesCheck()]
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_check = {v.check_id: v for v in violations[str(filepath)]}
    forbid_vars_violation = by_check["forbid-vars"]
    blank_lines_violation = by_check["excessive-blank-lines"]

    assert is_fix_errored(forbid_vars_violation)
    assert not is_fix_rejected(forbid_vars_violation)
    assert not (forbid_vars_violation.fix_data and forbid_vars_violation.fix_data.get("fixed"))
    assert not is_fix_errored(blank_lines_violation)
    assert blank_lines_violation.fix_data == {"fixed": True}
    assert "data = requests.get(url)" in filepath.read_text()


def test_apply_fixes_marks_already_resolved_violation_fixed_not_errored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a check that writes more than once per fix() call
    # (looping over violations individually, like validate_function_name)
    # can commit some violations before a later write raises. Marking every
    # violation in the batch [FIX ERRORED] regardless would misreport an
    # already-applied fix as "not applied", leaving the user unable to tell
    # which change actually landed on disk. Only the violation(s) still
    # present after the crash must be marked errored.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\nresult = things.fetchall()\n")

    forbid_vars = ForbidVarsCheck()

    def partial_then_raise(fp: Path, *_args: object, **_kwargs: object) -> None:
        # Simulates a multi-write check that already committed the fix for
        # "data" before crashing while attempting "result".
        atomic_write_text(fp, "response = requests.get(url)\nresult = things.fetchall()\n", "utf-8")
        raise RuntimeError("simulated fix bug partway through")

    monkeypatch.setattr(forbid_vars, "fix", partial_then_raise)

    orchestrator = CheckOrchestrator(checks=[forbid_vars], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_line = {v.line: v for v in violations[str(filepath)]}
    data_violation = by_line[1]
    result_violation = by_line[2]

    assert data_violation.fix_data is not None
    assert data_violation.fix_data.get("fixed") is True
    assert not is_fix_errored(data_violation)

    assert is_fix_errored(result_violation)
    assert not (result_violation.fix_data and result_violation.fix_data.get("fixed"))

    fixed_content = filepath.read_text()
    assert "response = requests.get(url)" in fixed_content
    assert "result = things.fetchall()" in fixed_content


def test_apply_fixes_records_rule_failure_when_fix_raises_after_resolving_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: if fix() commits every requested edit successfully and
    # then raises afterwards (e.g. during unrelated cleanup), every
    # violation ends up correctly marked [FIXED] and none are left to mark
    # [FIX ERRORED] — but the exception itself must still be recorded
    # somewhere, or a genuine internal failure would be completely
    # invisible behind what looks like a fully successful fix.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    forbid_vars = ForbidVarsCheck()

    def fix_then_raise(fp: Path, *_args: object, **_kwargs: object) -> None:
        atomic_write_text(fp, "response = requests.get(url)\n", "utf-8")
        raise RuntimeError("simulated cleanup bug after a successful fix")

    monkeypatch.setattr(forbid_vars, "fix", fix_then_raise)

    orchestrator = CheckOrchestrator(checks=[forbid_vars], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    violation = violations[str(filepath)][0]
    assert violation.fix_data is not None
    assert violation.fix_data.get("fixed") is True
    assert not is_fix_errored(violation)
    assert orchestrator.rule_failures == [(str(filepath), "forbid-vars")]
    assert filepath.read_text() == "response = requests.get(url)\n"


def test_apply_fixes_marks_only_the_rejected_violation_of_a_multi_write_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # validate-function-name's fix() writes once per violation rather than
    # once per check. When one of those writes is rejected, the orchestrator
    # must attribute the rejection to that specific violation, not the
    # whole check — an earlier rename that already committed must still be
    # reported [FIXED], not swept into [FIX REJECTED] alongside it.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def get_config():\n"
        '    with open("config.json") as f:\n'
        "        return f.read()\n"
        "\n\n"
        "def get_active(user: dict) -> bool:\n"
        '    return user.get("status") == "active"\n'
    )

    original_apply_fix = vfn_module.apply_fix

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> bool:
        if suggestion.func_name == "get_active":
            atomic_write_text(fp, "def broken(:\n", "utf-8")
        return original_apply_fix(fp, suggestion)

    monkeypatch.setattr(vfn_module, "apply_fix", flaky_apply_fix)

    checks = load_checks(select={"validate-function-name"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_func_name = {v.fix_data["suggestion"].func_name: v for v in violations[str(filepath)] if v.fix_data}
    get_config_violation = by_func_name["get_config"]
    get_active_violation = by_func_name["get_active"]
    get_config_fix_data = get_config_violation.fix_data
    assert get_config_fix_data is not None

    assert get_config_fix_data.get("fixed") is True
    assert not is_fix_rejected(get_config_violation)
    assert is_fix_rejected(get_active_violation)
    assert not (get_active_violation.fix_data and get_active_violation.fix_data.get("fixed"))

    fixed_content = filepath.read_text()
    assert "def get_config" not in fixed_content
    assert 'def get_active(user: dict) -> bool:\n    return user.get("status") == "active"\n' in fixed_content


def test_apply_fixes_marks_errored_violation_of_a_multi_write_check_when_apply_fix_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: validate-function-name's own fix() already catches any
    # exception apply_fix() raises other than FixValidationError internally
    # (logging it and moving on to the next violation), so it never
    # propagates to CheckOrchestrator._apply_fixes' own [FIX ERRORED]
    # handling at all — that handling is only reachable for single-write
    # checks. The check itself must mark the specific violation it failed
    # to fix, the same way it already does for a rejected fix.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def get_config():\n"
        '    with open("config.json") as f:\n'
        "        return f.read()\n"
        "\n\n"
        "def get_active(user: dict) -> bool:\n"
        '    return user.get("status") == "active"\n'
    )

    original_apply_fix = vfn_module.apply_fix

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> bool:
        if suggestion.func_name == "get_active":
            raise RuntimeError("simulated apply_fix bug")
        return original_apply_fix(fp, suggestion)

    monkeypatch.setattr(vfn_module, "apply_fix", flaky_apply_fix)

    checks = load_checks(select={"validate-function-name"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_func_name = {v.fix_data["suggestion"].func_name: v for v in violations[str(filepath)] if v.fix_data}
    get_config_violation = by_func_name["get_config"]
    get_active_violation = by_func_name["get_active"]
    get_config_fix_data = get_config_violation.fix_data
    assert get_config_fix_data is not None

    assert get_config_fix_data.get("fixed") is True
    assert not is_fix_errored(get_config_violation)
    assert is_fix_errored(get_active_violation)
    assert not is_fix_rejected(get_active_violation)
    assert not (get_active_violation.fix_data and get_active_violation.fix_data.get("fixed"))

    fixed_content = filepath.read_text()
    assert "def get_config" not in fixed_content
    assert "def get_active(user: dict) -> bool:" in fixed_content


def _disappear_before_refetch(
    orchestrator: CheckOrchestrator, _forbid_vars: ForbidVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the file can't be re-read inside _apply_fixes (e.g. deleted by a
    # concurrent process), that check's fix is skipped rather than crashing.
    original_read = orchestrator._read_source
    calls = {"n": 0}

    def flaky_read(fp: Path) -> tuple[str, str] | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_read(fp)
        return None

    monkeypatch.setattr(orchestrator, "_read_source", flaky_read)


def _disappear_after_fix(
    orchestrator: CheckOrchestrator, _forbid_vars: ForbidVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the file can't be re-read for the post-fix verification (e.g.
    # deleted right after the fix wrote it), the fix isn't reported as
    # fixed even though it actually happened — this run just can't confirm
    # it, so it stays conservative rather than guessing.
    original_read = orchestrator._read_source
    calls = {"n": 0}

    def flaky_read(fp: Path) -> tuple[str, str] | None:
        calls["n"] += 1
        # Call 1 is _check_file's own initial read, call 2 is _apply_fixes'
        # pre-fix read — both must succeed so the real fix actually runs.
        # Call 3 is the new post-fix verification read.
        if calls["n"] <= 2:
            return original_read(fp)
        return None

    monkeypatch.setattr(orchestrator, "_read_source", flaky_read)


def _recompute_finds_no_fixable_violations(
    _orchestrator: CheckOrchestrator, forbid_vars: ForbidVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_check = forbid_vars.check
    calls = {"n": 0}

    def flaky_check(fp: Path, tree: ast.Module, source: str) -> list[Violation]:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_check(fp, tree, source)
        return []

    monkeypatch.setattr(forbid_vars, "check", flaky_check)


def _recompute_raises(
    _orchestrator: CheckOrchestrator, forbid_vars: ForbidVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A crash in _apply_fixes' own pre-fix recompute call is distinct from
    # fix() itself raising (which is caught separately and marked [FIX
    # ERRORED], see test_apply_fixes_marks_violation_errored_when_fix_raises_unexpectedly)
    # — fix() is never even reached here. The original, already-reported
    # violation must stay exactly as it was rather than silently vanishing.
    original_check = forbid_vars.check
    calls = {"n": 0}

    def flaky_check(fp: Path, tree: ast.Module, source: str) -> list[Violation]:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_check(fp, tree, source)
        raise ValueError("simulated recompute failure")

    monkeypatch.setattr(forbid_vars, "check", flaky_check)


def _fix_returns_false(
    _orchestrator: CheckOrchestrator, forbid_vars: ForbidVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forbid_vars, "fix", lambda *_a, **_k: False)


def _fix_raises(
    _orchestrator: CheckOrchestrator, forbid_vars: ForbidVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kws: object) -> bool:
        raise RuntimeError("simulated fix failure")

    monkeypatch.setattr(forbid_vars, "fix", boom)


@pytest.mark.parametrize(
    "configure",
    [
        _disappear_before_refetch,
        _disappear_after_fix,
        _recompute_finds_no_fixable_violations,
        _recompute_raises,
        _fix_returns_false,
        _fix_raises,
    ],
    ids=[
        "file-disappears-before-refetch",
        "file-disappears-after-fix",
        "recompute-finds-no-fixable-violations",
        "recompute-raises",
        "fix-returns-false",
        "fix-raises",
    ],
)
def test_apply_fixes_marks_nothing_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configure: Callable[[CheckOrchestrator, ForbidVarsCheck, pytest.MonkeyPatch], None],
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    forbid_vars = ForbidVarsCheck()
    orchestrator = CheckOrchestrator(checks=[forbid_vars], fix_mode=True)
    configure(orchestrator, forbid_vars, monkeypatch)

    violations = orchestrator.process_files([str(filepath)])
    v = violations[str(filepath)][0]
    assert not (v.fix_data and v.fix_data.get("fixed"))


def test_load_checks_explicit_check_args_none_default() -> None:
    # Passing check_args explicitly (not relying on the None default)
    # takes the same path as leaving it unset.
    checks = load_checks(select={"forbid-vars"}, check_args={})
    assert len(checks) == 1
    assert checks[0].check_id == "forbid-vars"


def test_load_checks_ignore_set_skips_matching_check() -> None:
    checks = load_checks(ignore={"forbid-vars"})
    check_ids = {c.check_id for c in checks}
    assert "forbid-vars" not in check_ids
    assert len(check_ids) == len(ALL_CHECKS) - 1


def test_load_checks_ignore_composes_with_select() -> None:
    # Regression: `select` used to make `ignore` a no-op entirely (an
    # `elif` instead of two independent checks), so `--select`+`--ignore`
    # couldn't be combined the way `ruff check --select`/`--ignore` can.
    checks = load_checks(select={"forbid-vars", "redundant-super-init"}, ignore={"forbid-vars"})
    assert {c.check_id for c in checks} == {"redundant-super-init"}


def test_load_checks_check_specific_args_are_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No shipped check currently has a configurable `__init__`, so this
    # exercises the generic re-instantiate-with-kwargs branch in
    # `load_checks` against a synthetic check rather than a real one.
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


def test_main_unparseable_file_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # Regression: an unparseable file used to be silently dropped with exit
    # code 0 — indistinguishable from a clean run with nothing to report.
    # Content includes "data" so the file clears every check's prefilter
    # pattern and actually reaches parsing rather than being dropped as a
    # non-candidate beforehand.
    filepath = tmp_path / "broken.py"
    filepath.write_text("data = foo(:\n")

    with caplog.at_level("DEBUG"):
        assert main([str(filepath)]) == 1
    assert f"{filepath}: error: could not be read or parsed; file skipped" in capsys.readouterr().err
    # The clean diagnostic above already reports this; a raw traceback
    # alongside it on stderr would just be redundant noise (ch. 7: "MUST NOT
    # emit uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_permission_denied_file_returns_one_inside_git_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # Regression: inside a real git repo, the prefilter's `git grep`
    # subprocess (not the Python-only fallback the other tmp_path-based
    # tests exercise, since they aren't git repos) used to report a
    # permission-denied file only via a "failed to stat" line on its own
    # stderr, without changing its exit code for the files it *could*
    # read. main() never inspected that stderr, so the file was silently
    # dropped before ever reaching _check_file — the whole run reported
    # exit code 0, as if the file never existed. ForbidVarsCheck has a
    # prefilter pattern ("data" is one of its forbidden names), so this
    # only reproduces via the check that actually calls into git grep.
    git = shutil.which("git")
    assert git is not None

    original_dir = Path.cwd()
    try:
        os.chdir(tmp_path)
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        filepath = tmp_path / "module.py"
        filepath.write_text("data = 1\n")
        subprocess.run([git, "add", "module.py"], check=True, cwd=tmp_path)  # noqa: S603
        filepath.chmod(0o000)

        try:
            with caplog.at_level("DEBUG"):
                exit_code = main(["module.py", "--select", "forbid-vars"])
        finally:
            filepath.chmod(0o644)

        assert exit_code == 1
        assert "module.py: error: could not be read or parsed; file skipped" in capsys.readouterr().err
        # The clean diagnostic above already reports this; a raw traceback
        # alongside it on stderr would just be redundant noise (ch. 7: "MUST
        # NOT emit uncontrolled human-oriented text into a machine-readable
        # output stream").
        assert all(record.levelname == "DEBUG" for record in caplog.records)
    finally:
        os.chdir(original_dir)


def test_main_check_crash_returns_one_and_reports_check_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: a check that crashes on every file it sees used to make
    # the whole run look clean (exit code 0, nothing printed) whenever no
    # other check reported a violation for the same files.
    def boom(*_args: object, **_kwargs: object) -> list[Violation]:
        raise ValueError("simulated check failure")

    monkeypatch.setattr(ForbidVarsCheck, "check", boom)

    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    exit_code = main([str(filepath), "--select", "forbid-vars"])
    assert exit_code == 1

    assert f"{filepath}: error: check 'forbid-vars' raised an unexpected exception" in capsys.readouterr().err


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


def test_main_reports_column_alongside_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Violation.col is computed accurately (forbid-vars reports the assigned
    # name's own ast.col_offset, converted to a character offset) but
    # main()'s printed line used to drop it entirely, reporting only the
    # line -- ch. 7: "MUST report line and column information accurately
    # when available". "data" starts at the 0-indexed character offset 4;
    # main() reports the conventional 1-based column, 5.
    filepath = tmp_path / "module.py"
    filepath.write_text("def process():\n    data = requests.get(url)\n    return data\n")

    exit_code = main([str(filepath), "--select", "forbid-vars"])
    assert exit_code == 1

    assert f"{filepath}:2:5: TRI001:" in capsys.readouterr().err


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


def test_main_fix_flag_reports_rejected_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A fix rejected for invalid syntax must be reported distinctly from
    # both [FIXED] and the ordinary [FIXABLE]/"Run with --fix" hint, since
    # re-running --fix would just fail identically again.
    def broken_fix(fp: Path, *_args: object, **_kwargs: object) -> None:
        atomic_write_text(fp, "def broken(:\n", "utf-8")

    monkeypatch.setattr(ForbidVarsCheck, "fix", broken_fix)

    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "forbid-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIX REJECTED]" in err
    assert "please report it" in err
    assert "https://github.com/alessio-locatelli/ruff-extra-rules/issues" in err
    assert "[FIXED]" not in err
    assert "Run with --fix" not in err
    assert filepath.read_text() == "data = requests.get(url)\n"
    # [FIX REJECTED] above already reports this; a raw traceback alongside
    # it on stderr would just be redundant noise (ch. 7: "MUST NOT emit
    # uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_fix_flag_reports_errored_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A fix() that raises something other than FixValidationError must be
    # reported distinctly from [FIXED], [FIX REJECTED], and the ordinary
    # [FIXABLE]/"Run with --fix" hint — re-running --fix would just crash
    # identically again, so suggesting it would be misleading.
    def broken_fix(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated fix bug")

    monkeypatch.setattr(ForbidVarsCheck, "fix", broken_fix)

    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "forbid-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIX ERRORED]" in err
    assert "please report it" in err
    assert "https://github.com/alessio-locatelli/ruff-extra-rules/issues" in err
    assert "[FIXED]" not in err
    assert "[FIX REJECTED]" not in err
    assert "Run with --fix" not in err
    assert filepath.read_text() == "data = requests.get(url)\n"
    # [FIX ERRORED] above already reports this; a raw traceback alongside it
    # on stderr would just be redundant noise (ch. 7: "MUST NOT emit
    # uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_fix_flag_reports_failed_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # A fix() that catches its own OSError (disk full, permission denied)
    # and returns False per ASTCheck.fix()'s own documented contract must
    # be reported distinctly from [FIXED] and the ordinary [FIXABLE]/"Run
    # with --fix" hint -- re-running --fix would just fail identically
    # again, and unlike [FIX ERRORED] this isn't a bug in the check's own
    # logic, so the hint must not claim it is.
    subdir = tmp_path / "readonly"
    subdir.mkdir()
    filepath = subdir / "module.py"
    filepath.write_text("data = requests.get(url)\n")
    subdir.chmod(0o555)
    try:
        with caplog.at_level("DEBUG"):
            exit_code = main([str(filepath), "--select", "forbid-vars", "--fix"])
    finally:
        subdir.chmod(0o755)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "[FIX FAILED]" in err
    assert "bug" not in err
    assert "[FIXED]" not in err
    assert "[FIX ERRORED]" not in err
    assert "[FIX REJECTED]" not in err
    assert "Run with --fix" not in err
    assert filepath.read_text() == "data = requests.get(url)\n"
    # [FIX FAILED] above already reports this; a raw traceback alongside it
    # on stderr would just be redundant noise (ch. 7: "MUST NOT emit
    # uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_reports_rule_failure_when_reread_fails_mid_fix_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: _apply_fixes() re-reads the file before recomputing each
    # check's fresh violations (a previous check's fix in the same loop may
    # have already modified it). If that re-read itself fails, the loop used
    # to just `continue` with zero signal anywhere -- the stale violation
    # was left unmarked and reported as an ordinary [FIXABLE], as if --fix
    # had never even been attempted for it.
    original_read_source = CheckOrchestrator._read_source
    calls = 0

    def read_source_fails_on_second_call(self: CheckOrchestrator, filepath: Path) -> tuple[str, str] | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            return None
        return original_read_source(self, filepath)

    monkeypatch.setattr(CheckOrchestrator, "_read_source", read_source_fails_on_second_call)

    filepath = tmp_path / "module.py"
    filepath.write_text(
        "data = requests.get(url)\n\n\nclass Base:\n    def __init__(self):\n        pass\n\n\n"
        "class Child(Base):\n    def __init__(self, **kwargs):\n        super().__init__(**kwargs)\n"
    )

    # A second, never-fixable check alongside forbid-vars: its own
    # violation must be left alone by the marking loop below (only
    # forbid-vars' violation matches check.check_id), exercising that the
    # loop's `if` condition can also be False for a violation from an
    # unrelated check_id, not just True for a matching, fixable one.
    checks = load_checks(select={"forbid-vars", "redundant-super-init"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert (str(filepath), "forbid-vars") in orchestrator.rule_failures
    forbid_vars_violation = next(v for v in violations[str(filepath)] if v.check_id == "forbid-vars")
    super_init_violation = next(v for v in violations[str(filepath)] if v.check_id == "redundant-super-init")
    assert is_fix_errored(forbid_vars_violation)
    assert not is_fix_errored(super_init_violation)
    # The re-read failure means nothing was ever written back.
    assert "data = requests.get(url)" in filepath.read_text()


def test_main_reports_rule_failure_when_recompute_raises_mid_fix_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Regression: _apply_fixes() recomputes fresh violations via check.check()
    # against the current file state before calling check.fix(). If that
    # recompute itself raises, only the broad outer `except Exception` used
    # to catch it -- logging the exception but never recording a
    # rule_failure or marking any violation, so the run could look
    # completely clean depending on what else was reported for the file.
    original_check = ForbidVarsCheck.check
    calls = 0

    def check_raises_on_second_call(
        self: ForbidVarsCheck, filepath: Path, tree: ast.Module, source: str
    ) -> list[Violation]:
        nonlocal calls
        calls += 1
        if calls == 2:
            msg = "simulated recompute failure"
            raise RuntimeError(msg)
        return original_check(self, filepath, tree, source)

    monkeypatch.setattr(ForbidVarsCheck, "check", check_raises_on_second_call)

    filepath = tmp_path / "module.py"
    filepath.write_text(
        "data = requests.get(url)\n\n\nclass Base:\n    def __init__(self):\n        pass\n\n\n"
        "class Child(Base):\n    def __init__(self, **kwargs):\n        super().__init__(**kwargs)\n"
    )

    # A second, never-fixable check alongside forbid-vars: its own
    # violation must be left alone by the marking loop below (only
    # forbid-vars' violation matches check.check_id), exercising that the
    # loop's `if` condition can also be False for a violation from an
    # unrelated check_id, not just True for a matching, fixable one.
    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "forbid-vars,redundant-super-init", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert f"{filepath}: error: check 'forbid-vars' raised an unexpected exception" in err
    assert "[FIX ERRORED]" in err
    assert "Run with --fix" not in err
    # Nothing was ever written back -- the recompute failed before fix() could run.
    assert "data = requests.get(url)" in filepath.read_text()
    # The error line above already reports this; a raw traceback alongside
    # it on stderr would just be redundant noise (ch. 7: "MUST NOT emit
    # uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_reports_rule_failure_when_fix_raises_after_resolving_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Regression: fix() can commit every requested edit and then raise
    # afterwards (e.g. during unrelated cleanup) — every violation ends up
    # correctly reported [FIXED], but the exception itself must still
    # surface in the run's own output, or a genuine internal failure would
    # be completely invisible behind what reads as full success.
    def fix_then_raise(_self: object, filepath: Path, *_args: object, **_kwargs: object) -> None:
        atomic_write_text(filepath, "response = requests.get(url)\n", "utf-8")
        raise RuntimeError("simulated cleanup bug after a successful fix")

    monkeypatch.setattr(ForbidVarsCheck, "fix", fix_then_raise)

    filepath = tmp_path / "module.py"
    filepath.write_text("data = requests.get(url)\n")

    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "forbid-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIXED]" in err
    assert "[FIX ERRORED]" not in err
    assert f"{filepath}: error: check 'forbid-vars' raised an unexpected exception" in err
    assert filepath.read_text() == "response = requests.get(url)\n"
    # The error line above already reports this; a raw traceback alongside
    # it on stderr would just be redundant noise (ch. 7: "MUST NOT emit
    # uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


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
    # No shipped check currently registers its own CLI argument, so this
    # exercises main()'s add_cli_arguments -> parse_args ->
    # cli_kwargs_from_args -> check_args wiring end-to-end against a
    # synthetic check.
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


@pytest.mark.parametrize("flag", ["--select", "--ignore"], ids=["select", "ignore"])
def test_main_unknown_check_name_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    exit_code = main([str(filepath), flag, "not-a-real-check"])
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
    # Regression: `--select`+`--ignore` together used to behave like
    # `--select` alone, silently dropping `--ignore` (see
    # `test_load_checks_ignore_composes_with_select`).
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


def test_main_malformed_cli_argument_exits_via_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    # argparse's own error handling for a malformed argument (e.g. an
    # unknown flag) bypasses main()'s own return value entirely via
    # sys.exit(2) — a third, separate value from this project's own 0/1
    # exit-code contract, worth locking down explicitly rather than leaving
    # it as undocumented, incidental argparse behavior.
    with pytest.raises(SystemExit) as exc_info:
        main(["--not-a-real-flag"])

    assert exc_info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
