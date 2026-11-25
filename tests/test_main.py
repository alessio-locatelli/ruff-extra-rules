from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks.__main__ import _install_sigterm_handler, _raise_keyboard_interrupt, run
from pre_commit_hooks.ast_checks._orchestrator import CheckOrchestrator
from tests._helpers import raises, restricted_permissions

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from pre_commit_hooks.ast_checks import ASTCheck
    from pre_commit_hooks.ast_checks._base import SuppressionUsage, Violation


@pytest.fixture(autouse=True)
def _restore_sigterm_handler() -> Iterator[None]:
    original = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, original)


def test_raise_keyboard_interrupt_raises_keyboard_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        _raise_keyboard_interrupt(signal.SIGTERM, None)


def test_install_sigterm_handler_registers_handler() -> None:
    _install_sigterm_handler()

    assert signal.getsignal(signal.SIGTERM) is _raise_keyboard_interrupt


def test_install_sigterm_handler_degrades_when_signal_signal_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original_handler = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(
        signal, "signal", raises(ValueError, "signal only works in main thread of the main interpreter")
    )

    with caplog.at_level("DEBUG"):
        _install_sigterm_handler()

    assert signal.getsignal(signal.SIGTERM) is original_handler
    assert "Could not install a SIGTERM handler" in caplog.text


def test_run_prints_message_and_returns_one_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _interrupted(_argv: list[str] | None = None) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("pre_commit_hooks.ast_checks.__main__.main", _interrupted)

    assert run([]) == 1
    assert "Interrupted." in capsys.readouterr().err


def test_run_returns_mains_exit_code_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pre_commit_hooks.ast_checks.__main__.main", lambda _argv=None: 0)

    assert run([]) == 0


def test_real_sigterm_mid_run_stops_gracefully_without_leftover_temp_files(
    git_worktree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    filepaths = []
    for i in range(10):
        filepath = git_worktree / f"module_{i}.py"
        filepath.write_text("data = requests.get(url)\n")
        filepaths.append(str(filepath))

    original_check_file = CheckOrchestrator._check_file
    calls = 0

    def _check_file_then_send_sigterm_on_third_call(
        self: CheckOrchestrator,
        filepath: Path,
        checks: list[ASTCheck],
        *,
        record_only: Sequence[ASTCheck] = (),
        prior_suppression_usages: tuple[SuppressionUsage, ...] = (),
        prior_active_error_codes: frozenset[str] = frozenset(),
    ) -> list[Violation] | None:
        nonlocal calls
        calls += 1
        violations = original_check_file(
            self,
            filepath,
            checks,
            record_only=record_only,
            prior_suppression_usages=prior_suppression_usages,
            prior_active_error_codes=prior_active_error_codes,
        )
        if calls == 3:
            os.kill(os.getpid(), signal.SIGTERM)
        return violations

    monkeypatch.setattr(CheckOrchestrator, "_check_file", _check_file_then_send_sigterm_on_third_call)

    exit_code = run(["--select", "meaningless-vars", "--fix", *filepaths])

    assert exit_code == 1
    assert "Interrupted." in capsys.readouterr().err
    assert calls < len(filepaths)
    assert list(git_worktree.glob("*.tmp")) == []
    for filepath_str in filepaths:
        content = Path(filepath_str).read_text()
        assert content in {"data = requests.get(url)\n", "response = requests.get(url)\n"}


def test_real_invocation_does_not_leak_a_traceback_onto_stderr(git_worktree: Path) -> None:
    filepath = git_worktree / "unreadable.py"
    filepath.write_text("data = requests.get(url)\n")

    with restricted_permissions(filepath, 0o000, restore=0o644):
        completed_process = subprocess.run(  # noqa: S603 -- Trusted test command.
            [sys.executable, "-m", "pre_commit_hooks.ast_checks", "--select", "meaningless-vars", filepath],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    assert completed_process.returncode == 1
    assert "Traceback" not in completed_process.stdout
    assert "Traceback" not in completed_process.stderr
    assert f"{filepath}: error: could not be read or parsed; file skipped" in completed_process.stderr


def test_verbose_flag_surfaces_the_underlying_exception_on_stderr(git_worktree: Path) -> None:
    filepath = git_worktree / "unreadable.py"
    filepath.write_text("data = requests.get(url)\n")

    with restricted_permissions(filepath, 0o000, restore=0o644):
        completed_process = subprocess.run(  # noqa: S603 -- Trusted test command.
            [
                sys.executable,
                "-m",
                "pre_commit_hooks.ast_checks",
                "--verbose",
                "--select",
                "meaningless-vars",
                filepath,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    assert completed_process.returncode == 1
    assert f"{filepath}: error: could not be read or parsed; file skipped" in completed_process.stderr
    assert "Traceback (most recent call last):" in completed_process.stderr
    assert "PermissionError" in completed_process.stderr


def test_verbose_flag_does_not_change_violations_or_exit_code(git_worktree: Path) -> None:
    filepath = git_worktree / "violates.py"
    filepath.write_text("data = requests.get(url)\n")

    def _run(*extra_args: str) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            "-m",
            "pre_commit_hooks.ast_checks",
            "--select",
            "meaningless-vars",
            "--meaningless-vars-level",
            "aggressive",
            *extra_args,
        ]
        return subprocess.run(  # noqa: S603 -- Trusted test command.
            [*cmd, filepath], capture_output=True, text=True, check=False, timeout=30
        )

    quiet = _run()
    verbose = _run("--verbose")

    assert quiet.returncode == verbose.returncode == 1
    assert quiet.stdout == verbose.stdout == ""
    violation_line = f"{filepath}:1:1: TR1:"
    assert any(line.startswith(violation_line) for line in quiet.stderr.splitlines())
    assert any(line.startswith(violation_line) for line in verbose.stderr.splitlines())


def test_linting_outside_a_git_worktree_fails_even_without_a_prefilter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "example.py").write_text("x = 1\n")

    assert run(["--select=excessive-blank-lines", "example.py"]) == 2
    assert "not a git repository" in capsys.readouterr().err


def test_linting_rejects_inputs_outside_the_worktree(git_worktree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outside_file = git_worktree.parent / f"{git_worktree.name}.py"
    outside_file.write_text("x = 1\n")

    assert run(["--select=excessive-blank-lines", str(outside_file)]) == 2
    assert f"input path is outside the Git working tree: {outside_file}" in capsys.readouterr().err


@pytest.mark.usefixtures("git_worktree")
def test_linting_reports_missing_git(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PATH", "")

    assert run(["--select=excessive-blank-lines", "example.py"]) == 2
    assert "Ensure Git is installed and available on PATH" in capsys.readouterr().err


def test_failed_git_grep_aborts_before_fixing(
    git_worktree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = "data = input()\n"
    filepath = git_worktree / "example.py"
    filepath.write_text(source)
    real_run = subprocess.run
    searches: list[list[str]] = []

    def fail_search(
        command: list[str], *, capture_output: bool, check: bool, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        if command[:3] == ["git", "--literal-pathspecs", "grep"]:
            searches.append(command)
            return subprocess.CompletedProcess(command, 128, b"", b"Git search failure")
        return real_run(command, capture_output=capture_output, check=check, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fail_search)

    assert run(["--isolated", "--select=meaningless-vars,validate-function-name", "--fix", str(filepath)]) == 2
    assert "git grep failed (Git search failure)" in capsys.readouterr().err
    assert len(searches) == 1
    assert filepath.read_text() == source


@pytest.mark.parametrize("argument", ["nested", "nested/example.py", "."], ids=["directory", "file", "indexed-file"])
@pytest.mark.parametrize("gitfile", [False, True], ids=["embedded-repo", "gitfile-worktree"])
def test_linting_rejects_nested_worktree_inputs(
    git_worktree: Path, capsys: pytest.CaptureFixture[str], argument: str, gitfile: bool
) -> None:
    nested = git_worktree / "nested"
    nested.mkdir()
    (nested / "example.py").write_text("data = 1\n")
    if argument == ".":
        subprocess.run(["git", "add", "nested/example.py"], check=True)  # noqa: S607 -- Git is required on PATH.
    command = ["git", "init", "-q"]
    if gitfile:
        command.extend(("--separate-git-dir", str(git_worktree / ".git" / "nested-metadata")))
    subprocess.run(command, check=True, cwd=nested)  # noqa: S603 -- Trusted test command.

    assert run(["--select=meaningless-vars", argument]) == 2
    assert f"Run the linter from the Git working tree at {nested}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argument", [".", "linked.py", "linked_dir/example.py"], ids=["discovered", "explicit", "ancestor"]
)
@pytest.mark.parametrize(
    ("external", "check"),
    [(False, "meaningless-vars"), (True, "excessive-blank-lines")],
    ids=["internal-prefiltered", "external-unfiltered"],
)
def test_linting_rejects_symlink_inputs(
    git_worktree: Path, capsys: pytest.CaptureFixture[str], argument: str, external: bool, check: str
) -> None:
    target_root = git_worktree.parent / f"{git_worktree.name}-target" if external else git_worktree / "target"
    target_root.mkdir()
    target = target_root / "example.py"
    target.write_text("data = 1\n")
    if argument == "linked_dir/example.py":
        link = git_worktree / "linked_dir"
        link.symlink_to(target_root, target_is_directory=True)
    else:
        link = git_worktree / "linked.py"
        link.symlink_to(target)

    assert run([f"--select={check}", argument]) == 2
    assert f"symbolic link: {link}" in capsys.readouterr().err


def test_discovery_rejects_a_dangling_symlink(git_worktree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dangling = git_worktree / "dangling.py"
    dangling.symlink_to("missing.py")

    assert run(["--select=meaningless-vars", "."]) == 2
    assert f"symbolic link: {dangling}" in capsys.readouterr().err


def test_linting_accepts_parent_paths_within_the_worktree(
    git_worktree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    nested = git_worktree / "src"
    nested.mkdir()
    monkeypatch.chdir(nested)
    (git_worktree / "example.py").write_text("data = 1\n")

    assert run(["--select=meaningless-vars", "--meaningless-vars-level=aggressive", "../example.py"]) == 1
    assert "../example.py:1:1: TR1:" in capsys.readouterr().err
    assert run(["--select=meaningless-vars", "../../example.py"]) == 2
    assert "outside the Git working tree: ../../example.py" in capsys.readouterr().err


def test_parent_path_out_of_nested_repository_uses_the_outer_worktree(
    git_worktree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nested = git_worktree / "nested"
    nested.mkdir()
    subprocess.run(["git", "init", "-q"], check=True, cwd=nested)  # noqa: S607 -- Git is required on PATH.
    (git_worktree / "example.py").write_text("data = 1\n")

    assert run(["--select=meaningless-vars", "--meaningless-vars-level=aggressive", "nested/../example.py"]) == 1
    assert "nested/../example.py:1:1: TR1:" in capsys.readouterr().err


def test_an_empty_git_directory_does_not_make_a_nested_worktree(
    git_worktree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nested = git_worktree / "nested"
    (nested / ".git").mkdir(parents=True)
    (nested / "example.py").write_text("data = 1\n")

    assert run(["--select=meaningless-vars", "--meaningless-vars-level=aggressive", "nested"]) == 1
    assert f"{nested / 'example.py'}:1:1: TR1:" in capsys.readouterr().err


@pytest.mark.uses_project_config
@pytest.mark.parametrize("argument", [".", "vendor/example.py"], ids=["discovered", "explicit"])
def test_excluded_symbolic_link_skips_worktree_validation(
    git_worktree: Path, capsys: pytest.CaptureFixture[str], argument: str
) -> None:
    (git_worktree / "pyproject.toml").write_text('[tool.ruff-extra-rules]\nexclude = ["vendor/**"]\n')
    vendor = git_worktree / "vendor"
    vendor.mkdir()
    (vendor / "example.py").symlink_to("missing.py")

    assert run(["--select=meaningless-vars", argument]) == 0
    assert capsys.readouterr().err == ""
