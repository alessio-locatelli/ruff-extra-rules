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
        _install_sigterm_handler()  # must not raise

    # ch. 31: "MUST NOT consider a test that merely checks that the process
    # did not crash sufficient evidence of correctness" -- also verify the
    # actual degraded behavior: no handler was installed, and the fallback
    # is logged rather than silent.
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A SIGTERM delivered mid-run (e.g. prek's own timeout, or a CI job
    killing this process) must unwind through run()'s KeyboardInterrupt
    handling -- proven with a real `os.kill()`-delivered signal, not a
    mocked exception, since converting SIGTERM into a catchable exception is
    exactly the new behavior under test. `atomic_write_text()`'s own
    try/finally cleanup on an arbitrary exception is already covered
    elsewhere (test_atomic_write_text's directory-raises case in
    tests/test_base.py); what's new here is that a real SIGTERM actually
    reaches that cleanup at all instead of terminating the process outright.
    """
    filepaths = []
    for i in range(10):
        filepath = tmp_path / f"module_{i}.py"
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
    # Stopped before every file was processed -- the signal actually cut
    # the run short rather than being silently ignored.
    assert calls < len(filepaths)
    # Every fix that did run replaced its target atomically: no dangling
    # temp file left behind by a write the signal happened to interrupt.
    assert list(tmp_path.glob("*.tmp")) == []
    # Every file on disk is still valid Python -- either untouched or fully
    # fixed, never a partial write.
    for filepath_str in filepaths:
        content = Path(filepath_str).read_text()
        assert content in {"data = requests.get(url)\n", "response = requests.get(url)\n"}


def test_real_invocation_does_not_leak_a_traceback_onto_stderr(tmp_path: Path) -> None:
    """Only a real subprocess -- not an in-process call -- can observe this:
    pytest's own logging-capture plugin hides what an actual end user's
    terminal would show (ch. 7: "MUST NOT emit uncontrolled human-oriented
    text into a machine-readable output stream").
    """
    filepath = tmp_path / "unreadable.py"
    filepath.write_text("data = requests.get(url)\n")

    with restricted_permissions(filepath, 0o000, restore=0o644):
        completed_process = subprocess.run(  # noqa: S603
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


def test_verbose_flag_surfaces_the_underlying_exception_on_stderr(tmp_path: Path) -> None:
    """The clean diagnostic line above ("could not be read or parsed") never
    says *why* -- that detail only exists in a `logger.debug(...,
    exc_info=True)` call that's silent by default (see
    test_real_invocation_does_not_leak_a_traceback_onto_stderr, above, and
    _orchestrator.py's _read_source docstring). Ch. 27: "MUST provide a
    debug or verbose mode when normal output is insufficient for
    troubleshooting" -- --verbose is that mode, raising the root logger to
    DEBUG so the same failure additionally prints its real traceback.
    """
    filepath = tmp_path / "unreadable.py"
    filepath.write_text("data = requests.get(url)\n")

    with restricted_permissions(filepath, 0o000, restore=0o644):
        completed_process = subprocess.run(  # noqa: S603
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


def test_verbose_flag_does_not_change_violations_or_exit_code(tmp_path: Path) -> None:
    """Ch. 27: "MUST ensure that debug logging does not change lint
    results" / "does not change auto-fix behavior" -- --verbose only
    reconfigures logging, so an ordinary (non-crashing) violation must be
    reported identically with or without it.
    """
    filepath = tmp_path / "violates.py"
    filepath.write_text("data = requests.get(url)\n")

    def _run(*extra_args: str) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            "-m",
            "pre_commit_hooks.ast_checks",
            "--select",
            "meaningless-vars",
            "--meaningless-vars-level",
            "permissive",
            *extra_args,
        ]
        return subprocess.run([*cmd, filepath], capture_output=True, text=True, check=False, timeout=30)  # noqa: S603

    quiet = _run()
    verbose = _run("--verbose")

    assert quiet.returncode == verbose.returncode == 1
    assert quiet.stdout == verbose.stdout == ""
    violation_line = f"{filepath}:1:1: TR1:"
    assert any(line.startswith(violation_line) for line in quiet.stderr.splitlines())
    assert any(line.startswith(violation_line) for line in verbose.stderr.splitlines())
