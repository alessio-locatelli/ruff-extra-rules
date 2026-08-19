from __future__ import annotations

import ast
import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, NoReturn
from unittest import mock

import pytest

import pre_commit_hooks.ast_checks.redundant_type_conversion.session as tri006_session_module
import pre_commit_hooks.ast_checks.validate_function_name as vfn_module
from pre_commit_hooks import ruff_extra_rules, ruff_extra_rules_ty
from pre_commit_hooks._cache import CacheManager
from pre_commit_hooks._filelock import locked
from pre_commit_hooks._lsp import LSPError
from pre_commit_hooks.ast_checks import ALL_CHECKS, _cli, _discovery, _orchestrator
from pre_commit_hooks.ast_checks._base import (
    CheckUnavailableError,
    FixOutcome,
    FixResult,
    Violation,
    atomic_write_text,
)
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks._diagnostics import report
from pre_commit_hooks.ast_checks._discovery import ExcludePattern, expand_directories, filter_excluded_files
from pre_commit_hooks.ast_checks._options import CheckOption, EnumOption
from pre_commit_hooks.ast_checks._orchestrator import (
    CheckOrchestrator,
    _group_by_check_id,
    _set_fix_outcomes,
    load_checks,
)
from pre_commit_hooks.ast_checks._per_file_ignores import PerFileIgnore, PerFileIgnoreList
from pre_commit_hooks.ast_checks.excessive_blank_lines import ExcessiveBlankLinesCheck
from pre_commit_hooks.ast_checks.meaningless_vars import MeaninglessVarsCheck, MeaninglessVarsLevel
from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import AggressivenessLevel
from pre_commit_hooks.ast_checks.redundant_super_init import RedundantSuperInitCheck
from pre_commit_hooks.ast_checks.redundant_type_conversion import daemon as tri006_daemon
from tests._helpers import raises, restricted_permissions
from tests.factories import ViolationFactory

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pre_commit_hooks.ast_checks import ASTCheck
    from pre_commit_hooks.ast_checks.validate_function_name.analysis import Suggestion


class _Response:
    __slots__ = ()

    def json(self) -> str:
        return "response value"


def _exec_module(source: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile(source, "<orchestrator-fixture>", "exec", dont_inherit=True), namespace)  # noqa: S102
    return namespace


# Every expectation below was measured against `ruff 0.16.1`'s own
# `exclude` handling before being written down; see ADR-0046.
@pytest.mark.parametrize(
    ("files", "patterns", "expected"),
    [
        (["a.py", "b.py"], [], ["a.py", "b.py"]),
        (["a.py", "migrations/0001_init.py"], ["migrations/*.py"], ["a.py"]),
        (["a.py", "migrations/versions/0001_init.py"], ["migrations/*.py"], ["a.py"]),
        (["a.py", "migrations/0001_init.py"], ["./migrations/**"], ["a.py"]),
        (["a.py", "migrations/0001_init.py"], ["migrations/../a.py"], ["migrations/0001_init.py"]),
        (["src/main.py", "vendor/lib/thing.py"], ["vendor"], ["src/main.py"]),
        (["src/main.py"], ["nonexistent/*.py"], ["src/main.py"]),
        (["tests/fixtures/x/deep.py", "a.py"], ["tests/fixtures/**"], ["a.py"]),
        (["tests/fixtures/shallow.py", "a.py"], ["tests/fixtures/**"], ["a.py"]),
        (["tests/fixtures/x/deep.py", "a.py"], ["tests/fixtures"], ["a.py"]),
        (["src/vendor/v.py", "vendor/w.py"], ["vendor/*"], ["src/vendor/v.py"]),
        (["src/vendor/v.py", "vendor/w.py"], ["src/vendor/*"], ["vendor/w.py"]),
        (["src/vendor/v.py", "a.py"], ["src/*"], ["a.py"]),
        (["sub/tests/t.py", "a.py"], ["tests/fixtures/**"], ["sub/tests/t.py", "a.py"]),
        (["sub/tests/t.py", "a.py"], ["tests"], ["a.py"]),
    ],
    ids=[
        "no-patterns-returns-all",
        "excludes-matching-file",
        "star-spans-separators",
        "a-leading-dot-component-is-resolved",
        "a-parent-component-is-resolved",
        "excludes-matching-parent-dir",
        "no-match-keeps-file",
        "double-star-is-recursive",
        "double-star-matches-direct-child",
        "bare-directory-excludes-its-subtree",
        "anchored-pattern-does-not-match-deeper-namesake",
        "anchored-pattern-matches-its-own-path",
        "star-in-directory-position-prunes-subtree",
        "anchored-pattern-is-rooted-not-suffix-matched",
        "separatorless-pattern-matches-any-component",
    ],
)
def test_filter_excluded_files(tmp_path: Path, files: list[str], patterns: list[str], expected: list[str]) -> None:
    absolute = [str(tmp_path / name) for name in files]
    anchored = [ExcludePattern(pattern, tmp_path) for pattern in patterns]

    filtered = filter_excluded_files(absolute, anchored)

    assert filtered == [str(tmp_path / name) for name in expected]


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
    # A directory argument (e.g. `ruff-extra-rules src/`, the
    # form this project's own dev docs use — see AGENTS.md) must not reach
    # CheckOrchestrator.process_files() as a single unexpanded path.
    # Inside a git repo, git grep's own directory pathspec support makes
    # it recurse and match files *inside* that directory, but those
    # resolved paths never match the literal directory path in
    # git_grep_filter's own input map, so every result would be silently
    # discarded as unresolvable — the run would report zero violations,
    # exit code 0, having checked nothing.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        tracked = tmp_path / "tracked.py"
        tracked.write_text("x = 1\n")
        subprocess.run([git, "add", "tracked.py"], check=True, cwd=tmp_path)  # noqa: S603

        assert expand_directories([str(tmp_path)]) == [str(tracked.resolve())]


def test_expand_directories_includes_untracked_file_inside_a_git_repo(tmp_path: Path) -> None:
    # A brand-new file that hasn't been `git add`ed yet must not be
    # silently dropped from a directory-argument expansion, even though
    # git_grep_filter already always searches that same file when it's
    # named explicitly on the CLI instead (ADR 0024's
    # `--untracked --no-exclude-standard`) — naming the containing
    # directory must not produce a different, false-clean result for the
    # identical file.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        tracked = tmp_path / "tracked.py"
        tracked.write_text("x = 1\n")
        subprocess.run([git, "add", "tracked.py"], check=True, cwd=tmp_path)  # noqa: S603

        untracked = tmp_path / "untracked.py"
        untracked.write_text("y = 1\n")

        assert expand_directories([str(tmp_path)]) == [str(tracked.resolve()), str(untracked.resolve())]


def test_expand_directories_excludes_gitignored_file_but_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A `.gitignore`d file is still excluded from a directory scan — ADR
    # 0015's "avoid sweeping in .venv/build artifacts" rationale still
    # applies — but its exclusion must now be visible (ch. 34/35) rather
    # than the run silently reporting zero violations for a directory that
    # actually contains an unexamined `.py` file (#67).
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        (tmp_path / ".gitignore").write_text("ignored.py\n")
        (tmp_path / "ignored.py").write_text("z = 1\n")

        with caplog.at_level("WARNING"):
            matches = expand_directories([str(tmp_path)])

        assert matches == []
        assert "ignored.py" in caplog.text


def test_expand_directories_warns_when_ignored_path_streaming_is_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603
        monkeypatch.setattr(_discovery, "_CAN_STREAM_IGNORED_STATUS", False)

        with caplog.at_level("WARNING"):
            matches = expand_directories([str(tmp_path)])

        assert matches == []
        assert "could not be inspected on this platform" in caplog.text


def test_expand_directories_warns_about_an_ignored_directory_with_an_unrecognized_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # `git status --ignored` collapses an entirely-ignored directory to one
    # `!! path/` line without confirming it holds a `.py` file (ADR 0028) --
    # an arbitrary directory name (not a known packaging/tooling artifact,
    # ADR 0029) is still reported on that unconfirmed basis.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        (tmp_path / ".gitignore").write_text("vendored/\n")
        vendored = tmp_path / "vendored"
        vendored.mkdir()
        (vendored / "module.py").write_text("z = 1\n")

        with caplog.at_level("WARNING"):
            matches = expand_directories([str(tmp_path)])

        assert matches == []
        assert "vendored" in caplog.text


def test_expand_directories_does_not_warn_about_known_non_source_directories(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # __pycache__/, *.egg-info/, and .cache/ get created by this
    # project's own routine `pytest`/`build`/`uv sync` commands (see ADR
    # 0029), so warning about them unconditionally would fire on
    # essentially every directory-argument run -- none of them is ever
    # used for hand-written source, so all are excluded from this warning.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        (tmp_path / ".gitignore").write_text("__pycache__/\n*.egg-info/\n.cache/\n")

        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "module.cpython-314.pyc").write_bytes(b"")

        egg_info = tmp_path / "pkg.egg-info"
        egg_info.mkdir()
        (egg_info / "PKG-INFO").write_text("")

        cache = tmp_path / ".cache"
        cache.mkdir()
        (cache / "some_entry.json").write_text("{}")

        with caplog.at_level("WARNING"):
            matches = expand_directories([str(tmp_path)])

        assert matches == []
        assert not any(record.levelname == "WARNING" for record in caplog.records)


def test_expand_directories_does_not_warn_about_ruff_cache_even_though_its_own_gitignore_is_nested(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Unlike __pycache__/.cache/etc. (see ADR 0029), `.ruff_cache`
    # isn't named in a top-level .gitignore at all -- ruff writes its own
    # nested `.ruff_cache/.gitignore` containing `*`, which is what makes
    # `git status --ignored` collapse the directory to a single ignored-
    # directory line. The denylist match is by directory name alone, so it
    # must still apply here even though no top-level .gitignore rule names
    # ".ruff_cache" directly.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        ruff_cache = tmp_path / ".ruff_cache"
        ruff_cache.mkdir()
        (ruff_cache / ".gitignore").write_text("*\n")
        (ruff_cache / "0" / "content").mkdir(parents=True)

        with caplog.at_level("WARNING"):
            matches = expand_directories([str(tmp_path)])

        assert matches == []
        assert not any(record.levelname == "WARNING" for record in caplog.records)


def test_expand_directories_warns_about_ignored_file_regardless_of_status_showuntrackedfiles_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # `git status`'s own `--ignored` reporting is otherwise
    # governed by the ambient `status.showUntrackedFiles` config -- "no"
    # suppresses every `!!` record, silently defeating this warning's whole
    # purpose for a user who's set that (a real, documented git setting),
    # so the probe must pin `--untracked-files=normal` explicitly rather
    # than trust git's own default.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603
        subprocess.run([git, "config", "status.showUntrackedFiles", "no"], check=True, cwd=tmp_path)  # noqa: S603

        (tmp_path / ".gitignore").write_text("ignored.py\n")
        (tmp_path / "ignored.py").write_text("z = 1\n")

        with caplog.at_level("WARNING"):
            matches = expand_directories([str(tmp_path)])

        assert matches == []
        assert "ignored.py" in caplog.text


def test_expand_directories_caps_gitignore_warning_at_a_bounded_number_of_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A directory that isn't *entirely* ignored (e.g. a
    # generated/ subtree with one tracked file alongside many individually
    # -ignored `.py` outputs) can't collapse to a single git-status line --
    # each ignored path is reported separately. The warning must stay
    # bounded rather than materializing, sorting, and printing an unbounded
    # list for a large generated/vendored tree.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        (tmp_path / "README.md").write_text("keep this directory partially tracked\n")
        subprocess.run([git, "add", "README.md"], check=True, cwd=tmp_path)  # noqa: S603

        num_ignored = 1_000
        (tmp_path / ".gitignore").write_text("".join(f"ignored{i}.py\n" for i in range(num_ignored)))
        for i in range(num_ignored):
            (tmp_path / f"ignored{i}.py").write_text("z = 1\n")

        with caplog.at_level("WARNING"):
            expand_directories([str(tmp_path)])

        assert "at least 20 gitignored path(s)" in caplog.text
        assert "showing first 20" in caplog.text


def _patch_status_popen(monkeypatch: pytest.MonkeyPatch, status_command: list[str] | None) -> None:
    real_popen = subprocess.Popen

    def fake_popen(cmd: list[str], *args: object, **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        if "status" in cmd:
            if status_command is None:
                raise FileNotFoundError("git not found")
            return real_popen(status_command, **kwargs)
        return real_popen(cmd, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "Popen", fake_popen)


@pytest.fixture
def git_repository(tmp_path: Path) -> str:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q"], check=True, cwd=tmp_path)  # noqa: S603
    return git


@pytest.mark.parametrize(
    "status_failure",
    [
        "missing",
        "stderr",
        "stderr-with-paths",
        "malformed",
        "irrelevant",
        "nonzero",
        "timeout",
        "continuous",
        "unterminated",
    ],
    ids=[
        "status-subprocess-missing",
        "status-reports-stderr",
        "status-reports-stderr-with-ignored-paths",
        "status-reports-malformed-output",
        "status-reports-irrelevant-output",
        "status-exits-unsuccessfully",
        "status-times-out",
        "status-emits-continuously",
        "status-never-terminates-a-record",
    ],
)
def test_expand_directories_skips_gitignore_warning_when_git_status_probe_is_unreliable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    git_repository: str,
    monkeypatch: pytest.MonkeyPatch,
    status_failure: str,
) -> None:
    # Self-healing: if the extra `git status --ignored` probe used to detect
    # gitignored exclusions worth warning about is itself unreliable --
    # missing entirely (e.g. git errors or times out), or succeeding with a
    # per-path stderr error next to incomplete stdout (the same "don't trust
    # a result alongside stderr" rule git_grep_filter and the primary git
    # ls-files call both already follow) -- the directory listing must still
    # succeed; only the warning is skipped, silently.
    if status_failure == "timeout":
        monkeypatch.setattr(_discovery, "_GIT_STATUS_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(_discovery, "_PROCESS_STOP_TIMEOUT_SECONDS", 0.01)
    if status_failure == "continuous":
        monkeypatch.setattr(_discovery, "_GIT_STATUS_TIMEOUT_SECONDS", 1)
        monotonic_values = iter([0.0, 0.0, 0.0, 1.0])
        monkeypatch.setattr(_discovery.time, "monotonic", lambda: next(monotonic_values))

    with contextlib.chdir(tmp_path):
        tracked = tmp_path / "tracked.py"
        tracked.write_text("x = 1\n")
        subprocess.run([git_repository, "add", "tracked.py"], check=True, cwd=tmp_path)  # noqa: S603

        scripts = {
            "malformed": "import os; os.write(1, b'!! ignored.py')",
            "irrelevant": "import os; os.write(1, b'!! ignored.txt\\0')",
            "nonzero": "import sys; sys.exit(1)",
            "stderr": "import sys; sys.stderr.write('failed')",
            "stderr-with-paths": "import os; os.write(2, b'failed'); os.write(1, b'!! ignored.py\\0' * 20)",
            "timeout": (
                "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "os.write(1, b'?\\0'); time.sleep(30)"
            ),
            "continuous": "import os\nwhile True:\n os.write(1, b'??\\0')",
            "unterminated": "import os; os.write(1, b'!' * 70_000)",
        }
        status_command = None if status_failure == "missing" else [sys.executable, "-c", scripts[status_failure]]
        _patch_status_popen(monkeypatch, status_command)

        with caplog.at_level("WARNING"):
            matches = expand_directories([str(tmp_path)])

        assert matches == [str(tracked.resolve())]
        assert not any(record.levelname == "WARNING" for record in caplog.records)


def test_expand_directories_stops_ignored_status_at_the_reporting_threshold(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, git_repository: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with contextlib.chdir(tmp_path):
        (tmp_path / "tracked.py").write_text("x = 1\n")
        subprocess.run([git_repository, "add", "tracked.py"], check=True, cwd=tmp_path)  # noqa: S603

        _patch_status_popen(
            monkeypatch, [sys.executable, "-c", "import os\nwhile True:\n os.write(1, b'!! ignored.py\\0')"]
        )
        start = time.monotonic()
        with caplog.at_level("WARNING"):
            matches = expand_directories([str(tmp_path)])
        elapsed = time.monotonic() - start

        assert matches == [str((tmp_path / "tracked.py").resolve())]
        assert "at least 20 gitignored path(s)" in caplog.text
        assert elapsed < 3


def test_stop_ignored_status_process_escalates_after_termination_timeout() -> None:
    process = mock.Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired([], 1), None]

    _discovery._stop_process(process)

    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2


def test_expand_directories_skips_tracked_file_deleted_from_working_tree(tmp_path: Path) -> None:
    # `git ls-files` reports the *index*, not the working tree
    # -- a tracked file removed from disk with a plain `rm` (not `git rm`)
    # still shows up in its output even though it no longer exists. A
    # directory scan isn't asking about that specific file by name, so a
    # stale index entry must be dropped silently rather than surfacing a
    # fake "could not be read" error for a file the user never named.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        deleted = tmp_path / "deleted.py"
        deleted.write_text("x = 1\n")
        subprocess.run([git, "add", "deleted.py"], check=True, cwd=tmp_path)  # noqa: S603
        deleted.unlink()

        assert expand_directories([str(tmp_path)]) == []


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
    # This is self-healing (falls back to an equivalent glob
    # scan), so an ERROR-level .exception() call here would leak a raw
    # traceback onto the user's stderr by default (nothing in this codebase
    # configures logging, so Python's own lastResort handler prints
    # WARNING+ straight to stderr) for a condition nothing actually failed
    # at from the user's perspective.
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_expand_directories_includes_a_file_with_a_non_utf8_name(tmp_path: Path) -> None:
    # `git ls-files -z`'s stdout is decoded via `text=True`. A
    # filename that's a valid path on Linux (paths are just bytes, never
    # required to be valid UTF-8) but isn't decodable as UTF-8 must not
    # crash a directory scan that contains it -- `errors="surrogateescape"`
    # (the same handler `os.fsdecode()` already uses for filesystem paths)
    # round-trips it back to the identical file on disk instead.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        bad_path_bytes = str(tmp_path).encode() + b"/bad-\xff\xfe.py"
        with Path(os.fsdecode(bad_path_bytes)).open("wb") as f:
            f.write(b"y = 1\n")

        matches = expand_directories([str(tmp_path)])

        assert len(matches) == 1
        assert Path(matches[0]).exists()


def test_main_directory_with_no_python_files_returns_zero(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not python\n")

    assert main([str(tmp_path)]) == 0


def test_main_directory_argument_checks_files_inside_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        filepath = tmp_path / "module.py"
        filepath.write_text("data = 1\n")
        subprocess.run([git, "add", "module.py"], check=True, cwd=tmp_path)  # noqa: S603

        exit_code = main([str(tmp_path), "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"])

        assert exit_code == 1
        assert "TR1" in capsys.readouterr().err


def test_main_fix_renames_enclosing_references_in_class_bodies_and_methods(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def outer(response):\n"
        "    data: Payload = response.json()\n"
        "\n"
        "    class CapturesOuter:\n"
        "        captured = data\n"
        "\n"
        "        def method(self):\n"
        "            return data\n"
        "\n"
        "    class OwnAttribute:\n"
        "        data = 'class value'\n"
        "\n"
        "        def method(self):\n"
        "            return data\n"
        "\n"
        "    return CapturesOuter().method(), CapturesOuter.captured, OwnAttribute().method()\n"
    )

    assert main([str(filepath), "--fix", "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]) == 1

    assert filepath.read_text() == (
        "def outer(response):\n"
        "    payload: Payload = response.json()\n"
        "\n"
        "    class CapturesOuter:\n"
        "        captured = payload\n"
        "\n"
        "        def method(self):\n"
        "            return payload\n"
        "\n"
        "    class OwnAttribute:\n"
        "        data = 'class value'\n"
        "\n"
        "        def method(self):\n"
        "            return payload\n"
        "\n"
        "    return CapturesOuter().method(), CapturesOuter.captured, OwnAttribute().method()\n"
    )

    namespace = _exec_module(filepath.read_text())

    assert namespace["outer"](_Response()) == ("response value", "response value", "response value")


def test_main_fix_assigns_distinct_names_to_reverse_order_nested_closures(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def outer(response, response2):\n"
        "    def inner():\n"
        "        result: Payload = response2.json()\n"
        "        return data, result\n"
        "\n"
        "    data: Payload = response.json()\n"
        "    return inner()\n"
    )

    assert main([str(filepath), "--fix", "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]) == 1

    assert filepath.read_text() == (
        "def outer(response, response2):\n"
        "    def inner():\n"
        "        payload_2: Payload = response2.json()\n"
        "        return payload, payload_2\n"
        "\n"
        "    payload: Payload = response.json()\n"
        "    return inner()\n"
    )


_GENERIC_METHOD_ANNOTATIONS_SOURCE = (
    "def outer(response):\n"
    "    data: Payload = response.json()\n"
    "\n"
    "    class Container:\n"
    "        data = int\n"
    "\n"
    "        def method[T: data](self, value: data) -> data:\n"
    "            return data\n"
    "\n"
    "    return Container\n"
)

_GENERIC_METHOD_ANNOTATIONS_EXPECTED = (
    "def outer(response):\n"
    "    payload: Payload = response.json()\n"
    "\n"
    "    class Container:\n"
    "        data = int\n"
    "\n"
    "        def method[T: data](self, value: data) -> data:\n"
    "            return payload\n"
    "\n"
    "    return Container\n"
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            _GENERIC_METHOD_ANNOTATIONS_SOURCE,
            _GENERIC_METHOD_ANNOTATIONS_EXPECTED,
            id="generic-method-annotations",
        ),
        pytest.param(
            "def outer(response):\n"
            "    data: Payload = response.json()\n"
            "\n"
            "    class Container:\n"
            "        thunk = lambda: (data := 'lambda value')\n"
            "        captured = data\n"
            "\n"
            "    return Container.captured\n",
            "def outer(response):\n"
            "    payload: Payload = response.json()\n"
            "\n"
            "    class Container:\n"
            "        thunk = lambda: (data := 'lambda value')\n"
            "        captured = payload\n"
            "\n"
            "    return Container.captured\n",
            id="lambda-local-binding",
        ),
        pytest.param(
            "def outer(response):\n"
            "    data: Payload = response.json()\n"
            "\n"
            "    class Container:\n"
            "        def method(value=(data := 'class value')):\n"
            "            return value\n"
            "\n"
            "        captured = data\n"
            "\n"
            "    return Container.data, Container.captured\n",
            "def outer(response):\n"
            "    payload: Payload = response.json()\n"
            "\n"
            "    class Container:\n"
            "        def method(value=(data := 'class value')):\n"
            "            return value\n"
            "\n"
            "        captured = data\n"
            "\n"
            "    return Container.data, Container.captured\n",
            id="method-default-binding",
        ),
    ],
)
def test_main_fix_preserves_class_bindings(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(source)

    assert main([str(filepath), "--fix", "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]) == 1

    assert filepath.read_text() == expected


def test_generic_method_annotations_preserve_class_binding_at_runtime() -> None:
    namespace = _exec_module(_GENERIC_METHOD_ANNOTATIONS_EXPECTED)

    container = namespace["outer"](_Response())
    method = container.method

    assert method.__type_params__[0].__bound__ is int
    assert method.__annotations__ == {"value": int, "return": int}
    assert container().method(None) == "response value"


def test_main_fix_preserves_class_bindings_in_nested_class_headers_and_comprehension_iterables(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def outer(response):\n"
        "    data: Payload = response.json()\n"
        "\n"
        "    class Container:\n"
        "        data = int\n"
        "\n"
        "        class Nested(data):\n"
        "            value = data\n"
        "\n"
        "        thunk = lambda: data\n"
        "\n"
        "        values = [data for _ in (data,)]\n"
        "\n"
        "    return Container.Nested.__bases__[0], Container.Nested.value, Container.thunk(), Container.values\n"
    )

    assert main([str(filepath), "--fix", "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]) == 1

    assert filepath.read_text() == (
        "def outer(response):\n"
        "    payload: Payload = response.json()\n"
        "\n"
        "    class Container:\n"
        "        data = int\n"
        "\n"
        "        class Nested(data):\n"
        "            value = payload\n"
        "\n"
        "        thunk = lambda: payload\n"
        "\n"
        "        values = [payload for _ in (data,)]\n"
        "\n"
        "    return Container.Nested.__bases__[0], Container.Nested.value, Container.thunk(), Container.values\n"
    )

    namespace = _exec_module(filepath.read_text())

    assert namespace["outer"](_Response()) == (int, "response value", "response value", ["response value"])


def test_main_directory_argument_matches_explicit_file_argument_for_untracked_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An untracked file's violation must be reported the same way whether
    # it's named explicitly or reached by expanding its containing
    # directory.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        untracked = tmp_path / "untracked.py"
        untracked.write_text("result = 2\n")

        assert main([str(untracked), "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]) == 1
        assert "TR1" in capsys.readouterr().err

        assert main([str(tmp_path), "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]) == 1
        assert "TR1" in capsys.readouterr().err


def test_main_directory_scan_does_not_crash_on_a_file_with_a_non_utf8_name(tmp_path: Path) -> None:
    # expand_directories() safely includes a file whose name
    # isn't valid UTF-8 (see test_expand_directories_includes_a_file_with_a_
    # non_utf8_name), but that file still has to survive _prefilter.py's own
    # git_grep_filter() call once meaningless-vars' prefilter pattern runs against
    # it -- that call decodes git grep's stdout the same way, and would
    # crash right there instead of reporting the violation.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        bad_path_bytes = str(tmp_path).encode() + b"/bad-\xff\xfe.py"
        with Path(os.fsdecode(bad_path_bytes)).open("wb") as f:
            f.write(b"result = 1\n")

        assert main([str(tmp_path), "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]) == 1


def test_process_files_handles_utf8_bom(tmp_path: Path) -> None:
    # A UTF-8 BOM must not make the orchestrator silently skip the file.
    # filepath.read_text(encoding="utf-8") decodes a leading BOM as a
    # literal U+FEFF character, which ast.parse rejects as a syntax error —
    # reading with utf-8-sig strips it transparently instead (and is
    # identical to utf-8 for files without one).
    filepath = tmp_path / "with_bom.py"
    filepath.write_bytes(b"\xef\xbb\xbfdata = 1\n")

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)])
    violations = orchestrator.process_files([str(filepath)])

    assert len(violations[str(filepath)]) == 1
    assert violations[str(filepath)][0].error_code == "TR1"


def test_apply_fixes_handles_utf8_bom(tmp_path: Path) -> None:
    # The re-read before each check's fix() call must also strip a BOM. The
    # fixed file keeps its original BOM on write (detected encoding is
    # "utf-8-sig", the same encoding used to write back) — reading it back
    # with "utf-8-sig" strips it again, same as the original read.
    filepath = tmp_path / "with_bom.py"
    filepath.write_bytes(
        b"\xef\xbb\xbfimport requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].fix_outcome is FixOutcome.APPLIED
    assert filepath.read_bytes().startswith(b"\xef\xbb\xbf")
    assert filepath.read_text(encoding="utf-8-sig") == (
        "import requests\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n"
    )


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
        v.check_id == "redundant-assignment" and v.fix_outcome is FixOutcome.APPLIED for v in violations[str(filepath)]
    )
    assert redundant_assignment_fixed

    file_content = filepath.read_text(encoding="utf-8")
    assert 'x = "foo"' not in file_content
    assert "print(" in file_content
    assert '"foo"' in file_content


def test_apply_fixes_refreshes_non_fixable_checks_position_too(tmp_path: Path) -> None:
    # only checks that themselves had a fixable violation this
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


def test_apply_fixes_refreshes_non_fixable_checks_position_after_an_aborted_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same requirement as the successful-fix case above, but the line shift
    # comes from the external edit that aborts meaningless-vars' own fix,
    # not from a fix this run itself applied -- the final refresh pass must
    # not be gated on this run's own fix succeeding, since the file's
    # content is known to have changed regardless of who changed it.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\n\n"
        "def request():\n"
        "    data = requests.get(url)\n"
        "    return data.status_code\n"
        "\n\n"
        "class Base:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n\n"
        "class Child(Base):\n"
        "    def __init__(self, **kwargs):\n"
        "        super().__init__(**kwargs)\n"
    )

    def racing_fix(
        _self: MeaninglessVarsCheck, fp: Path, _violations: object, source: str, *_args: object, **_kwargs: object
    ) -> None:
        # Simulate a concurrent process deleting a blank line above the
        # redundant-super-init violation while this fix was in flight,
        # shifting it up by one line.
        edited = source.replace("return data.status_code\n\n\nclass Base:", "return data.status_code\n\nclass Base:", 1)
        fp.write_text(edited)
        atomic_write_text(
            fp,
            "def request():\n    response = requests.get(url)\n    return response.status_code\n",
            "utf-8",
            source,
        )

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", racing_fix)

    checks = load_checks(select={"meaningless-vars", "redundant-super-init"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    final_content = filepath.read_text()
    actual_super_init_line = next(
        i for i, line in enumerate(final_content.splitlines(), start=1) if "def __init__(self, **kwargs)" in line
    )

    super_init_violation = next(v for v in violations[str(filepath)] if v.check_id == "redundant-super-init")
    assert super_init_violation.line == actual_super_init_line


def test_apply_fixes_refreshes_a_participating_checks_own_left_open_violation(tmp_path: Path) -> None:
    # a check that *did* participate in the fix loop (it had
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
    assert method_violation.fix_outcome is not FixOutcome.APPLIED
    assert method_violation.line == actual_method_line


def test_refresh_stale_positions_never_drops_an_unrelated_open_violation_sharing_a_check_id_with_a_terminal_one(
    tmp_path: Path,
) -> None:
    # a check_id with a rejected/errored/failed entry must be
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

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    # Same message on purpose -- e.g. two identically-named functions in
    # different scopes would produce identical violation text.
    shared_message = "Meaningless variable name 'data' found. Consider renaming to 'response'."
    terminal = ViolationFactory.build(
        check_id="meaningless-vars", message=shared_message, fixable=True, fix_outcome=FixOutcome.FAILED
    )
    open_violation = ViolationFactory.build(
        check_id="meaningless-vars", message=shared_message, fixable=True, fix_outcome=FixOutcome.DECLINED
    )
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
    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    violations: list[Violation] = []

    filepath.unlink()
    orchestrator._refresh_stale_positions(filepath, violations)

    assert violations == []


def test_refresh_stale_positions_returns_when_final_parse_fails(tmp_path: Path) -> None:
    # A syntax error in the file's final state (e.g. an external edit
    # racing with this run) must not crash the reconciliation pass.
    filepath = tmp_path / "module.py"
    filepath.write_text("def broken(:\n")
    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
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
    monkeypatch.setattr(MeaninglessVarsCheck, "check", raises(RuntimeError, "simulated check failure"))

    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")
    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    # A still-open (unmarked) violation for this check_id, so there's
    # something for the reconciliation pass to actually attempt refreshing.
    stale_violation = ViolationFactory.build(
        check_id="meaningless-vars", fixable=True, fix_data=None, fix_outcome=FixOutcome.DECLINED
    )
    violations = [stale_violation]

    orchestrator._refresh_stale_positions(filepath, violations)

    assert (str(filepath), "meaningless-vars") in orchestrator.rule_failures  # pytriage: TR6
    # Left exactly as it was -- the check crashed before it could report
    # anything current to replace it with.
    assert violations == [stale_violation]


def test_fix_result_requires_one_outcome_per_violation() -> None:
    violations = [ViolationFactory.build(), ViolationFactory.build()]

    with pytest.raises(ValueError, match="one outcome per violation"):
        _set_fix_outcomes(violations, FixResult((FixOutcome.APPLIED,)))


@pytest.mark.parametrize(
    "outcome",
    [FixOutcome.REJECTED, FixOutcome.ERRORED, FixOutcome.DECLINED],
)
def test_post_fix_verification_preserves_a_reported_outcome_when_the_file_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: FixOutcome
) -> None:
    filepath = tmp_path / "module.py"
    violation = ViolationFactory.build(fix_outcome=outcome)
    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    monkeypatch.setattr(CheckOrchestrator, "_read_source", lambda _self, _filepath: None)

    assert orchestrator._mark_resolved_and_get_still_present(filepath, MeaninglessVarsCheck(), [violation]) == {
        (violation.line, violation.col, violation.message)
    }
    assert violation.fix_outcome is outcome


def test_post_fix_verification_declines_an_unconfirmed_applied_outcome(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    source = "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    filepath.write_text(source)
    check = MeaninglessVarsCheck()
    (violation,) = check.check(filepath, ast.parse(source), source)
    violation.fix_outcome = FixOutcome.APPLIED
    orchestrator = CheckOrchestrator(checks=[check])

    assert orchestrator._mark_resolved_and_get_still_present(filepath, check, [violation]) == {
        (violation.line, violation.col, violation.message)
    }
    assert violation.fix_outcome is FixOutcome.DECLINED


def test_fix_honors_pep263_encoding_declaration(tmp_path: Path) -> None:
    # A file with a non-UTF-8 PEP 263 encoding cookie must be read, fixed,
    # and written back in its declared encoding, not assumed to be UTF-8.
    source = "# -*- coding: latin-1 -*-\nresult = func(\n    x\n)  # caf\xe9\n"
    filepath = tmp_path / "latin1.py"
    filepath.write_bytes(source.encode("latin-1"))

    checks = load_checks(select={"misplaced-comment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].fix_outcome is FixOutcome.APPLIED
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

    assert violations[str(filepath)][0].fix_outcome is FixOutcome.APPLIED
    fixed_content = filepath.read_bytes()
    assert b"\r\nother = 1\r\n" in fixed_content
    assert b"x  # comment" in fixed_content


def test_process_files_empty_filepaths_returns_empty() -> None:
    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    assert orchestrator.process_files([]) == {}


def test_process_files_no_prefilter_pattern_checks_all_files(tmp_path: Path) -> None:
    # ExcessiveBlankLinesCheck has no prefilter pattern (checks
    # everything), so with only it enabled, no git-grep pre-filtering
    # happens at all.
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\nimport os\n")

    orchestrator = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].error_code == "TR2"


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
    ("check", "expect_unprocessable"),
    [
        pytest.param(RedundantSuperInitCheck(), False, id="prefilter-skip-forfeits-unprocessable-reporting"),
        pytest.param(ExcessiveBlankLinesCheck(), True, id="none-prefilter-still-reports-unprocessable"),
    ],
)
def test_process_files_unprocessable_reporting_is_gated_by_prefilter_match(
    tmp_path: Path, check: ASTCheck, expect_unprocessable: bool
) -> None:
    # ADR-0052: whether an invalid-syntax file even reaches _check_file()
    # -- and so whether it's reported via unprocessable_files at all --
    # depends on whether some enabled check's own prefilter pattern
    # matches its content. RedundantSuperInitCheck's own pattern doesn't
    # match this content; ExcessiveBlankLinesCheck's None pattern parses
    # (and reports) any content.
    filepath = tmp_path / "module.py"
    filepath.write_text("def foo(:\n")

    orchestrator = CheckOrchestrator(checks=[check])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}
    assert orchestrator.unprocessable_files == ([str(filepath)] if expect_unprocessable else [])  # pytriage: TR6


def test_process_files_none_pattern_check_still_sees_a_file_other_checks_patterns_miss(tmp_path: Path) -> None:
    # Each enabled check's own prefilter pattern must gate that check
    # individually -- combining them into one OR'd filter before running
    # any check would drop a file for every check whenever it failed to
    # match ANY enabled check's pattern, including excessive-blank-lines,
    # whose own get_prefilter_pattern() returns None (see
    # test_process_files_no_prefilter_pattern_checks_all_files) precisely
    # so it sees every file regardless of what else is enabled. This file
    # has a real excessive-blank-lines violation but contains none of
    # meaningless-vars' own patterns ("data"/"result"), so a combined
    # filter would drop it before it was ever parsed.
    filepath = tmp_path / "module.py"
    filepath.write_text('"""Doc."""\n\n\n\nclass Foo:\n    pass\n')

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck(), ExcessiveBlankLinesCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].error_code == "TR2"


def test_process_files_applies_each_checks_prefilter_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file must only run through a check whose own
    # prefilter pattern it actually matches -- not through every enabled
    # check merely because some *other* check's pattern matched. This file
    # contains meaningless-vars' own pattern ("data") but not
    # redundant-super-init's ("super().__init__"), so redundant-super-init's
    # check() must never even be called against it.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    redundant_super_init = RedundantSuperInitCheck()
    spy_check = mock.MagicMock(wraps=redundant_super_init.check)
    monkeypatch.setattr(RedundantSuperInitCheck, "check", spy_check)

    orchestrator = CheckOrchestrator(
        checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE), redundant_super_init]
    )
    violations = orchestrator.process_files([str(filepath)])

    spy_check.assert_not_called()
    assert violations[str(filepath)][0].error_code == "TR1"


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

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}


def test_process_files_resets_unprocessable_files_between_calls(tmp_path: Path) -> None:
    # A file that failed to parse on the first call must not keep showing up
    # as unprocessable on a later call where it isn't even part of the input.
    bad_filepath = tmp_path / "bad.py"
    bad_filepath.write_text("def foo(:\n")
    good_filepath = tmp_path / "good.py"
    good_filepath.write_text("x = 1\n")

    orchestrator = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()])
    orchestrator.process_files([str(bad_filepath)])
    assert orchestrator.unprocessable_files == [str(bad_filepath)]  # pytriage: TR6

    orchestrator.process_files([str(good_filepath)])
    assert orchestrator.unprocessable_files == []


def test_process_files_second_call_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)])
    first = orchestrator.process_files([str(filepath)])
    assert first[str(filepath)][0].error_code == "TR1"

    monkeypatch.setattr(
        CheckOrchestrator, "_check_file", raises(AssertionError, "_check_file should not run on a cache hit")
    )
    second = orchestrator.process_files([str(filepath)])
    assert second[str(filepath)][0].error_code == "TR1"


def test_cache_hit_and_cache_miss_report_equivalent_violations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ch. 9: "MUST ensure that cache hits and cache misses produce
    # equivalent lint results." fix_data is deliberately dropped from the
    # cached JSON blob (see _cache_violations' own comment, since it may
    # hold AST nodes) -- report-only mode is the only mode that ever reads
    # from the cache (fix mode always bypasses it), and report-only mode
    # never touches fix_data, so every field a printed diagnostic actually
    # depends on must still match exactly between a fresh check and a
    # cached one.
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\ndata = 1\n")
    checks: list[ASTCheck] = [MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE), ExcessiveBlankLinesCheck()]

    cache_miss_orchestrator = CheckOrchestrator(checks=checks)
    cache_miss = cache_miss_orchestrator.process_files([str(filepath)])[str(filepath)]
    assert {v.error_code for v in cache_miss} == {"TR1", "TR2"}

    cache_hit_orchestrator = CheckOrchestrator(checks=checks)
    monkeypatch.setattr(
        CheckOrchestrator, "_check_file", raises(AssertionError, "_check_file should not run on a cache hit")
    )
    cache_hit = cache_hit_orchestrator.process_files([str(filepath)])[str(filepath)]

    def as_comparable(v: Violation) -> tuple[str, str, int, int, str, bool]:
        return (v.check_id, v.error_code, v.line, v.col, v.message, v.fixable)

    assert [as_comparable(v) for v in cache_hit] == [as_comparable(v) for v in cache_miss]


def test_process_files_different_check_set_forces_recheck(tmp_path: Path) -> None:
    # Changing which checks are enabled between runs must invalidate the
    # cache entry from a previous run with a different check set.
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\ndata = 1\n")

    meaningless_vars_only = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)])
    meaningless_vars_only.process_files([str(filepath)])

    both_checks = CheckOrchestrator(
        checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE), ExcessiveBlankLinesCheck()]
    )
    violations = both_checks.process_files([str(filepath)])

    error_codes = {v.error_code for v in violations[str(filepath)]}
    assert error_codes == {"TR1", "TR2"}


def test_generate_cache_version_changes_when_source_tree_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cache version must change on its own when the hashed source tree
    # changes, without a developer having to remember to hand-bump
    # CACHE_VERSION whenever a check's own code changes.
    fake_root = tmp_path / "pre_commit_hooks"
    fake_root.mkdir()
    (fake_root / "module.py").write_text("x = 1\n")
    monkeypatch.setattr(_orchestrator, "_PACKAGE_ROOT", fake_root)

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    version_before = orchestrator._generate_cache_version()

    (fake_root / "module.py").write_text("x = 2\n")
    version_after = orchestrator._generate_cache_version()

    assert version_before != version_after


def test_generate_cache_version_changes_when_python_version_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    # ast.parse()'s output for identical source isn't guaranteed stable
    # across Python minor versions, so a .cache directory shared across an
    # interpreter upgrade must not silently reuse the old interpreter's
    # results. Patches the `sys` name binding inside the _orchestrator
    # module (not the real global sys module) so only
    # _generate_cache_version() sees a different version.
    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    version_before = orchestrator._generate_cache_version()

    fake_sys = types.SimpleNamespace(
        version_info=types.SimpleNamespace(major=sys.version_info.major, minor=sys.version_info.minor + 1)
    )
    monkeypatch.setattr(_orchestrator, "sys", fake_sys)
    version_after = orchestrator._generate_cache_version()

    assert version_before != version_after


def test_the_hook_name_changes_when_cacheable_check_config_changes() -> None:
    # Two orchestrators enabling the same check with different config (e.g.
    # this repo's own default vs. permissive --meaningless-vars-level hooks)
    # must get distinct hook_names -- see ADR-0044's hook_name/cache_version
    # split, which replaces folding the config fingerprint into
    # cache_version (that approach made two such orchestrators collide on
    # one file's cache blob, since a version mismatch wipes the whole blob,
    # not just the mismatched hook_name's own entry).
    default = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    permissive = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)])

    assert default.cache.hook_name != permissive.cache.hook_name
    assert default._generate_cache_version() == permissive._generate_cache_version()


def test_get_cached_violations_ignores_corrupted_cache_entry(
    tmp_path: Path,
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    orchestrator.cache.set_cached_result(filepath, orchestrator.cache.hook_name, {"violations": [{}]})

    cached_violations = orchestrator._get_cached_violations(filepath, orchestrator.cache.hook_name)
    assert cached_violations is None


def test_cache_violations_serialization_error_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)])

    monkeypatch.setattr(CacheManager, "set_cached_result", raises(TypeError, "simulated cache backend failure"))

    # Must not raise, just skip caching for this file.
    violations = orchestrator.process_files([str(filepath)])
    assert violations[str(filepath)][0].error_code == "TR1"


def test_process_files_check_exception_is_logged_and_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A check that raises must not prevent other checks from running or
    # crash the whole file's processing.
    filepath = tmp_path / "module.py"
    filepath.write_text("\n\n\ndata = 1\n")

    monkeypatch.setattr(MeaninglessVarsCheck, "check", raises(ValueError, "simulated check failure"))

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck(), ExcessiveBlankLinesCheck()])
    violations = orchestrator.process_files([str(filepath)])

    error_codes = {v.error_code for v in violations[str(filepath)]}
    assert error_codes == {"TR2"}


def test_process_files_check_exception_records_rule_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A check whose check() raises must not leave no trace at all outside
    # a debug log line — that would be indistinguishable from that check
    # having run cleanly and found nothing. If it's the only check
    # enabled, that would make the whole file (and the whole run) look
    # completely clean.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    monkeypatch.setattr(MeaninglessVarsCheck, "check", raises(ValueError, "simulated check failure"))

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert violations == {}
    assert orchestrator.rule_failures == [(str(filepath), "meaningless-vars")]  # pytriage: TR6


def test_process_files_rule_failure_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A result collected while a check crashed must never be cached: caching
    # it would let a later run's cache hit keep serving the crash's "empty"
    # result forever (until the tree hash changes), rather than actually
    # retrying the check.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    meaningless_vars = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
    original_check = meaningless_vars.check
    calls = {"n": 0}

    def flaky_check(_self: MeaninglessVarsCheck, fp: Path, tree: ast.Module, source: str) -> list[Violation]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("simulated check failure")
        return original_check(fp, tree, source)

    monkeypatch.setattr(MeaninglessVarsCheck, "check", flaky_check)

    orchestrator = CheckOrchestrator(checks=[meaningless_vars])
    first = orchestrator.process_files([str(filepath)])
    assert first == {}
    assert orchestrator.rule_failures == [(str(filepath), "meaningless-vars")]  # pytriage: TR6

    second = orchestrator.process_files([str(filepath)])
    assert second[str(filepath)][0].error_code == "TR1"


def test_process_files_unavailable_checks_result_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A result collected while a cacheable check was unavailable must never
    # be cached either -- otherwise a later run, after the prerequisite
    # recovers, would keep serving the stale "clean" cached result instead
    # of actually retrying the now-available check.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    meaningless_vars = MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)
    original_check = meaningless_vars.check
    calls = {"n": 0}

    def flaky_check(_self: MeaninglessVarsCheck, fp: Path, tree: ast.Module, source: str) -> list[Violation]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise CheckUnavailableError("some prerequisite is missing")
        return original_check(fp, tree, source)

    monkeypatch.setattr(MeaninglessVarsCheck, "check", flaky_check)

    orchestrator = CheckOrchestrator(checks=[meaningless_vars])
    first = orchestrator.process_files([str(filepath)])
    assert first == {}
    assert orchestrator.unavailable_checks == [("meaningless-vars", "some prerequisite is missing")]

    second = orchestrator.process_files([str(filepath)])
    assert second[str(filepath)][0].error_code == "TR1"


_CLEAN_MARKER = "# marked-clean\n"
_FLAGGED_LINE = "flagged_call()\n"
_SECOND_FLAGGED_LINE = "flagged_other()\n"
_UNRELATED_LINE = "unrelated_call()\n"


def test_process_files_a_sibling_hook_names_own_write_does_not_serve_a_stale_entry_after_content_changes(
    tmp_path: Path,
) -> None:
    # See ADR-0044: several hook_names can now share one file's cache blob.
    # `probe_a`'s own entry, cached against the file's original content,
    # must not survive being served once `probe_b`'s own write updates the
    # blob for content that changed since -- CacheManager.set_cached_
    # result() must drop it rather than leave it paired with the blob's
    # new, updated file_hash/mtime/size. The content change here comes from
    # outside either orchestrator (e.g. a concurrent edit, or a separate
    # tool) rather than from a fix in this same run, since CheckOrchestrator
    # itself now never caches a run that changed the file on its own (see
    # test_fix_mode_does_not_cache_a_run_that_actually_changed_the_file) --
    # this is CacheManager's own general-purpose protection underneath
    # that, independent of how the content came to change.
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    probe_a = CheckOrchestrator(checks=[_MarkerFixableCheck(check_id="probe-a")])
    first = probe_a.process_files([str(filepath)])
    assert first[str(filepath)][0].check_id == "probe-a"  # pytriage: TR6
    assert probe_a.cache.get_cached_result(filepath, probe_a.cache.hook_name) is not None

    filepath.write_text(f"x = 1\n{_CLEAN_MARKER}")

    probe_b = CheckOrchestrator(checks=[_MarkerFixableCheck(check_id="probe-b")])
    second = probe_b.process_files([str(filepath)])
    assert second == {}
    assert probe_b.cache.get_cached_result(filepath, probe_b.cache.hook_name) is not None

    assert probe_a.cache.get_cached_result(filepath, probe_a.cache.hook_name) is None

    rechecked_a = CheckOrchestrator(checks=[_MarkerFixableCheck(check_id="probe-a")])
    assert rechecked_a.process_files([str(filepath)]) == {}


class _AlwaysRerunProbeCheck:
    """Minimal `ASTCheck` double with `cacheable = False`, for exercising
    `CheckOrchestrator`'s always-rerun split independent of any real
    check's own implementation (e.g. `redundant-type-conversion`, which
    needs a live `ty` process rather than a plain in-memory fixture).
    """

    __slots__ = ("call_count", "message")

    check_id = "always-rerun-probe"
    error_code = "ZZZ001"
    cacheable = False
    tracks_direct_inputs = False
    OPTIONS: ClassVar[tuple[CheckOption, ...]] = ()

    def __init__(self, message: str = "probe") -> None:
        self.message = message
        self.call_count = 0

    def get_prefilter_pattern(self) -> list[str] | None:
        return None

    def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
        self.call_count += 1
        return [
            Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=1,
                col=0,
                message=self.message,
                fixable=False,
            )
        ]

    def fix(
        self, _filepath: Path, _violations: list[Violation], _source: str, _tree: ast.Module, _encoding: str = "utf-8"
    ) -> FixResult:
        return FixResult.for_violations(_violations, FixOutcome.DECLINED)

    def reconcile_direct_inputs(self, _already_processed: list[Path]) -> list[Path]:
        return []

    def record_direct_input(self, _filepath: Path, _source: str) -> None:
        return


class _MarkerFixableCheck(_AlwaysRerunProbeCheck):
    """A cacheable, fixable variant of `_AlwaysRerunProbeCheck`: flags a
    violation whenever a fixed marker line is absent from the source, and
    its own fix() appends that marker. Two instances with different
    `check_id`s (and so different `hook_name`s, per ADR-0044) can share a
    file's cache blob while each independently converges to clean once its
    own fix runs -- lets a test deterministically change a file's content
    via a real fix pass and observe what a *different* hook_name's own
    cache entry does across that change.
    """

    __slots__ = ("check_id",)

    error_code = "ZZZ003"
    cacheable = True

    def __init__(self, check_id: str) -> None:
        super().__init__()
        self.check_id = check_id

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> list[Violation]:
        if _CLEAN_MARKER in source:
            return []
        return [
            Violation(
                check_id=self.check_id, error_code=self.error_code, line=1, col=0, message="marker absent", fixable=True
            )
        ]

    def fix(
        self, filepath: Path, _violations: list[Violation], source: str, _tree: ast.Module, encoding: str = "utf-8"
    ) -> FixResult:
        atomic_write_text(filepath, source + _CLEAN_MARKER, encoding, source)
        return FixResult.for_violations(_violations, FixOutcome.APPLIED)


class _MarkerRemovingAlwaysRerunCheck(_AlwaysRerunProbeCheck):
    """Non-cacheable `ASTCheck` double whose own fix() strips
    `_MarkerFixableCheck`'s marker back out of the file -- lets a test
    demonstrate that an always-rerun check's own fix can invalidate a
    cacheable sibling's already-cached "clean" result for the same file.
    """

    __slots__ = ()

    check_id = "marker-remover"

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> list[Violation]:
        if _CLEAN_MARKER not in source:
            return []
        return [
            Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=1,
                col=0,
                message="marker present",
                fixable=True,
            )
        ]

    def fix(
        self, filepath: Path, _violations: list[Violation], source: str, _tree: ast.Module, encoding: str = "utf-8"
    ) -> FixResult:
        atomic_write_text(filepath, source.replace(_CLEAN_MARKER, ""), encoding, source)
        return FixResult.for_violations(_violations, FixOutcome.APPLIED)


def test_fix_mode_falls_through_when_an_always_rerun_fix_invalidates_a_cached_cacheable_result(
    tmp_path: Path,
) -> None:
    # ADR-0044: a clean cacheable-group cache hit must not be trusted once
    # an always-rerun sibling's own fix changes the file this run -- that
    # fix can invalidate whatever made the cacheable group clean, so this
    # must fall through to a full recompute rather than blindly returning
    # the stale cached "clean" result merged with the always-rerun group's
    # own fresh one.
    filepath = tmp_path / "module.py"
    filepath.write_text(f"x = 1\n{_CLEAN_MARKER}")

    populate = CheckOrchestrator(checks=[_MarkerFixableCheck(check_id="c")])
    populate.process_files([str(filepath)])  # marker present -> clean, populates the cache

    combined = CheckOrchestrator(
        checks=[_MarkerFixableCheck(check_id="c"), _MarkerRemovingAlwaysRerunCheck()], fix_mode=True
    )
    violations = combined.process_files([str(filepath)])

    # The stale cache hit for "c" must not be returned merged with only the
    # always-rerun check's own result -- that would silently hide the
    # violation the marker-remover's own fix just introduced for "c".
    by_check = {v.check_id: v for v in violations[str(filepath)]}
    assert "c" in by_check

    # Re-running the always-rerun group a second time as part of the
    # recompute must not happen: it would find the file already clean and
    # silently lose its own [FIXED] outcome. The marker-remover's own fix
    # must still be reported.
    assert by_check["marker-remover"].fix_outcome is FixOutcome.APPLIED


def test_always_rerun_probe_check_fix_is_a_no_op(tmp_path: Path) -> None:
    # _AlwaysRerunProbeCheck never marks a violation fixable, so its own
    # fix() is never reached through CheckOrchestrator's own _apply_fixes
    # path -- exercised directly here instead, matching this repo's own
    # convention for a check with no autofix (e.g.
    # RedundantTypeConversionCheck's own test_fix_never_applies_a_fix).
    probe = _AlwaysRerunProbeCheck()
    assert probe.fix(tmp_path / "module.py", [], "x = 1\n", ast.parse("x = 1\n")).outcomes == ()


class _DrainingProbeCheck(_AlwaysRerunProbeCheck):
    """An `_AlwaysRerunProbeCheck` that also reports extra cross-file candidates -- exercises
    `CheckOrchestrator._reconcile_direct_inputs()` (ADR-0041) independent of
    `redundant-type-conversion`'s own real implementation, which needs a live `ty` process.
    """

    __slots__ = ("direct_inputs", "extra_files")

    check_id = "draining-probe"

    def __init__(self, extra_files: list[Path] | None = None, message: str = "probe") -> None:
        super().__init__(message)
        self.direct_inputs: list[Path] = []
        self.extra_files = extra_files or []

    def record_direct_input(self, filepath: Path, _source: str) -> None:
        self.direct_inputs.append(filepath.resolve())

    def reconcile_direct_inputs(self, _already_processed: list[Path]) -> list[Path]:
        return self.extra_files


class _UnavailableDrainingCheck(_DrainingProbeCheck):
    __slots__ = ()

    check_id = "unavailable-draining-probe"

    def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
        raise CheckUnavailableError("simulated: prerequisite missing")


class _OtherDrainingProbeCheck(_DrainingProbeCheck):
    __slots__ = ()

    check_id = "other-draining-probe"


class _CrossFileProbeCheck(_DrainingProbeCheck):
    """A `_DrainingProbeCheck` declaring the direct-input lifecycle that
    `per-file-ignores` must keep feeding even for a file it switched the
    check off for; see ADR-0049.
    """

    __slots__ = ()

    check_id = "cross-file-probe"
    tracks_direct_inputs = True


class _PrefilteredProbeCheck(_AlwaysRerunProbeCheck):
    __slots__ = ()

    check_id = "prefiltered-probe"

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["probe-marker"]


class _PrefilteredCrossFileProbeCheck(_CrossFileProbeCheck):
    __slots__ = ()

    check_id = "prefiltered-cross-file-probe"

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["cross-file-marker"]


class _UnavailableCrossFileProbeCheck(_CrossFileProbeCheck):
    __slots__ = ()

    check_id = "unavailable-cross-file-probe"

    def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
        raise CheckUnavailableError("simulated: prerequisite missing")


class _DrainUnavailableCheck(_AlwaysRerunProbeCheck):
    __slots__ = ()

    check_id = "drain-unavailable-probe"

    def reconcile_direct_inputs(self, _already_processed: list[Path]) -> list[Path]:
        raise CheckUnavailableError("simulated: daemon unavailable")


class _RaisingDrainingCheck(_AlwaysRerunProbeCheck):
    __slots__ = ()

    check_id = "raising-draining-probe"

    def reconcile_direct_inputs(self, _already_processed: list[Path]) -> list[Path]:
        raise LSPError("simulated daemon disconnect")


class _DirectInputUnavailableCheck(_AlwaysRerunProbeCheck):
    __slots__ = ()

    check_id = "direct-input-unavailable-probe"

    def record_direct_input(self, _filepath: Path, _source: str) -> None:
        raise CheckUnavailableError("simulated: daemon unavailable")


class _NeverConvergingDrainingCheck(_AlwaysRerunProbeCheck):
    __slots__ = ("drain_call_count",)

    check_id = "never-converging-probe"

    def __init__(self, message: str = "probe") -> None:
        super().__init__(message)
        self.drain_call_count = 0

    def reconcile_direct_inputs(self, _already_processed: list[Path]) -> list[Path]:
        # Returns a fresh nonexistent path once, isolating reconciliation from direct-input processing.
        self.drain_call_count += 1
        return [Path(f"/nonexistent/never_converges_{self.drain_call_count}.py")]


class _OrderDependentDrainingCheck(_AlwaysRerunProbeCheck):
    """Reports `reported_file` as a cross-file candidate specifically once `trigger_file` has itself been
    examined -- simulates a caller passed to the same run as its own callee, whose later examination is
    what actually reveals the caller needs re-checking (ADR-0041's own processing-order regression).
    """

    __slots__ = ("reported_file", "trigger_file")

    check_id = "order-dependent-draining-probe"

    def __init__(self, *, trigger_file: Path, reported_file: Path, message: str = "probe") -> None:
        super().__init__(message)
        self.trigger_file = trigger_file.resolve()
        self.reported_file = reported_file.resolve()

    def reconcile_direct_inputs(self, already_processed: list[Path]) -> list[Path]:
        if self.trigger_file in already_processed:
            return [self.reported_file]
        return []


class _StaleThenCleanDrainingCheck(_AlwaysRerunProbeCheck):
    """Flags `flagged_file` only the first time it's check()-ed, and reports it as a cross-file
    candidate once `trigger_file` is examined -- proves a re-check that comes back clean replaces (not
    merely fails to add to) that file's own earlier, now-stale violation instead of leaving it in place.
    """

    __slots__ = ("flagged_file", "seen", "trigger_file")

    check_id = "stale-then-clean-draining-probe"

    def __init__(self, *, trigger_file: Path, flagged_file: Path, message: str = "probe") -> None:
        super().__init__(message)
        self.trigger_file = trigger_file.resolve()
        self.flagged_file = flagged_file.resolve()
        self.seen: set[Path] = set()

    def check(self, filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
        self.call_count += 1
        resolved = filepath.resolve()
        already_seen = resolved in self.seen
        self.seen.add(resolved)
        if resolved != self.flagged_file or already_seen:
            return []
        return [
            Violation(
                check_id=self.check_id, error_code=self.error_code, line=1, col=0, message=self.message, fixable=False
            )
        ]

    def reconcile_direct_inputs(self, already_processed: list[Path]) -> list[Path]:
        if self.trigger_file in already_processed:
            return [self.flagged_file]
        return []


def test_order_dependent_draining_checks_do_not_report_before_their_trigger(tmp_path: Path) -> None:
    trigger = tmp_path / "trigger.py"
    reported = tmp_path / "reported.py"
    order_dependent = _OrderDependentDrainingCheck(trigger_file=trigger, reported_file=reported)
    stale_then_clean = _StaleThenCleanDrainingCheck(trigger_file=trigger, flagged_file=reported)

    assert order_dependent.reconcile_direct_inputs([]) == []
    assert stale_then_clean.reconcile_direct_inputs([]) == []


class _SelectivelyViolatingDrainingCheck(_DrainingProbeCheck):
    """Only flags a file named `flagged.py` -- unlike `_AlwaysRerunProbeCheck`, which flags every file
    unconditionally, this lets a test drive an extra file through a real, empty (not unprocessable)
    violations result.
    """

    __slots__ = ()

    check_id = "selective-draining-probe"

    def check(self, filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
        if filepath.name != "flagged.py":
            return []
        return [
            Violation(check_id=self.check_id, error_code=self.error_code, line=1, col=0, message="probe", fixable=False)
        ]


def test_drain_cross_file_candidates_merges_an_extra_files_violations(tmp_path: Path) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")
    extra_file = tmp_path / "extra.py"
    extra_file.write_text("y = 2\n")
    another_extra_file = tmp_path / "another_extra.py"
    another_extra_file.write_text("z = 3\n")

    probe = _DrainingProbeCheck(extra_files=[extra_file, another_extra_file])
    orchestrator = CheckOrchestrator(checks=[probe])

    violations = orchestrator.process_files([str(main_file)])

    assert str(main_file) in violations
    assert str(extra_file.resolve()) in violations
    assert str(another_extra_file.resolve()) in violations
    assert violations[str(extra_file.resolve())][0].message == "probe"


def test_drain_cross_file_candidates_skips_a_check_marked_unavailable_this_run(tmp_path: Path) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")
    extra_file = tmp_path / "extra.py"
    extra_file.write_text("y = 2\n")

    probe = _UnavailableDrainingCheck(extra_files=[extra_file])
    orchestrator = CheckOrchestrator(checks=[probe])

    violations = orchestrator.process_files([str(main_file)])

    assert violations == {}
    assert orchestrator.unavailable_checks == [(probe.check_id, "simulated: prerequisite missing")]


def test_drain_cross_file_candidates_queues_the_same_file_once_per_check(tmp_path: Path) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")
    extra_file = tmp_path / "extra.py"
    extra_file.write_text("y = 2\n")
    first = _DrainingProbeCheck(extra_files=[extra_file])
    second = _OtherDrainingProbeCheck(extra_files=[extra_file])

    violations = CheckOrchestrator(checks=[first, second]).process_files([str(main_file)])

    assert {violation.check_id for violation in violations[str(extra_file.resolve())]} == {
        first.check_id,
        second.check_id,
    }


def test_drain_cross_file_candidates_records_an_unavailable_check(tmp_path: Path) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")
    probe = _DrainUnavailableCheck()
    orchestrator = CheckOrchestrator(checks=[probe])

    orchestrator.process_files([str(main_file)])

    assert orchestrator.rule_failures == []
    assert orchestrator.unavailable_checks == [(probe.check_id, "simulated: daemon unavailable")]


def test_drain_cross_file_candidates_records_rule_failure_when_a_check_raises(tmp_path: Path) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")

    orchestrator = CheckOrchestrator(checks=[_RaisingDrainingCheck()])

    violations = orchestrator.process_files([str(main_file)])

    assert str(main_file) in violations
    assert orchestrator.rule_failures == [(str(main_file), "raising-draining-probe")]


def test_drain_cross_file_candidates_does_not_invent_a_failure_without_direct_inputs() -> None:
    orchestrator = CheckOrchestrator(checks=[_RaisingDrainingCheck()])
    orchestrator._reconcile_direct_inputs([], {}, {})

    assert orchestrator.rule_failures == []


def test_process_files_records_an_unavailable_direct_input_check(tmp_path: Path) -> None:
    filepath = tmp_path / "main.py"
    filepath.write_text("x = 1\n")
    check = _DirectInputUnavailableCheck()
    orchestrator = CheckOrchestrator(checks=[check])

    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].check_id == check.check_id
    assert orchestrator.unavailable_checks == [(check.check_id, "simulated: daemon unavailable")]


def test_drain_cross_file_candidates_skips_an_unresolvable_extra_path(tmp_path: Path) -> None:
    # A path-resolution failure for one drained candidate must be isolated to that candidate, the same
    # way a check's own drain_cross_file_candidates() raising already is above -- not escape and abort
    # the whole run over one bad path from one check.
    class _UnresolvablePath(Path):
        def resolve(self, _strict: bool = False) -> NoReturn:  # noqa: FBT002 -- matches Path.resolve()'s own signature
            msg = "simulated resolve failure"
            raise OSError(msg)

    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")
    probe = _DrainingProbeCheck(extra_files=[_UnresolvablePath(tmp_path / "unresolvable.py")])
    orchestrator = CheckOrchestrator(checks=[probe])

    violations = orchestrator.process_files([str(main_file)])  # must not raise

    assert str(main_file) in violations


def test_drain_cross_file_candidates_skips_an_unresolvable_direct_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")
    other_file = tmp_path / "other.py"
    other_file.write_text("y = 1\n")
    probe = _DrainingProbeCheck(extra_files=[])
    orchestrator = CheckOrchestrator(checks=[probe])
    monkeypatch.setattr(CheckOrchestrator, "_process_single_file", lambda *_args: [])
    real_resolve = Path.resolve

    def resolve(filepath: Path, strict: bool = False) -> Path:  # noqa: FBT002
        if filepath == main_file:
            msg = "simulated resolve failure"
            raise OSError(msg)
        return real_resolve(filepath, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert orchestrator.process_files([str(main_file), str(other_file)]) == {}


def test_drain_cross_file_candidates_reconciles_one_snapshot_without_recursion(tmp_path: Path) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")

    probe = _NeverConvergingDrainingCheck()
    orchestrator = CheckOrchestrator(checks=[probe])

    orchestrator.process_files([str(main_file)])

    assert probe.drain_call_count == 1
    assert len(orchestrator.unprocessable_files) == 1


def test_drain_cross_file_candidates_does_not_record_derived_rechecks_as_direct_inputs(tmp_path: Path) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")
    derived_file = tmp_path / "derived.py"
    derived_file.write_text("y = 2\n")
    probe = _DrainingProbeCheck(extra_files=[derived_file])

    CheckOrchestrator(checks=[probe]).process_files([str(main_file)])

    assert probe.direct_inputs == [main_file.resolve()]


def test_drain_cross_file_candidates_recovers_a_dependent_processed_before_its_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A caller passed to the same run as its own callee, ordered before it, must still be re-examined
    # once the callee's own later examination reveals it dirty -- not silently suppressed by treating
    # every original input file as permanently "already processed" from the very start of the run
    # (ADR-0041's own processing-order regression). Passed as a relative path (the pre-commit/prek norm)
    # so a fix that resolves it before re-checking, but reports it under that resolved path instead of
    # its own original spelling, would also be caught.
    monkeypatch.chdir(tmp_path)
    caller = Path("caller.py")
    caller.write_text("x = 1\n")
    callee = Path("callee.py")
    callee.write_text("y = 2\n")

    probe = _OrderDependentDrainingCheck(trigger_file=callee, reported_file=caller)
    orchestrator = CheckOrchestrator(checks=[probe])

    violations = orchestrator.process_files([str(caller), str(callee)])

    # caller (direct) + callee (direct) + caller again (re-examined once callee's own drain flagged it).
    assert probe.call_count == 3
    # Reported under its own original (relative) key, not a second, separately-resolved one, and with
    # exactly one violation -- the re-check's own result replacing the first, now-stale one rather than
    # being appended alongside it.
    assert str(caller.resolve()) not in violations
    assert len(violations[str(caller)]) == 1


def test_drain_cross_file_candidates_clears_a_dependents_stale_violation_once_the_recheck_is_clean(
    tmp_path: Path,
) -> None:
    # The caller's own first examination flags it; once the callee's later examination marks it dirty,
    # the re-check comes back clean (the underlying issue is now fixed) -- that must replace, not merely
    # fail to add to, the first, now-stale violation, or it would linger in the report forever.
    caller = tmp_path / "caller.py"
    caller.write_text("x = 1\n")
    callee = tmp_path / "callee.py"
    callee.write_text("y = 2\n")

    probe = _StaleThenCleanDrainingCheck(trigger_file=callee, flagged_file=caller)
    orchestrator = CheckOrchestrator(checks=[probe])

    violations = orchestrator.process_files([str(caller), str(callee)])

    assert str(caller) not in violations


def test_drain_cross_file_candidates_reports_nothing_for_an_extra_file_with_no_violations(tmp_path: Path) -> None:
    flagged_file = tmp_path / "flagged.py"
    flagged_file.write_text("x = 1\n")
    clean_extra_file = tmp_path / "clean.py"
    clean_extra_file.write_text("y = 2\n")

    probe = _SelectivelyViolatingDrainingCheck(extra_files=[clean_extra_file])
    orchestrator = CheckOrchestrator(checks=[probe])

    violations = orchestrator.process_files([str(flagged_file)])

    assert str(flagged_file) in violations
    assert str(clean_extra_file.resolve()) not in violations


class _AppendingFixCheck(_AlwaysRerunProbeCheck):
    """A `fix()` that appends its own `marker` line to the file, optionally sleeping between reading
    `source` and writing it back to widen a concurrent-fix race window -- proves `_check_file`'s own
    per-file lock (`_fix_lock_path`) actually serializes two would-be-concurrent writers (e.g. two
    separate `CheckOrchestrator` instances, simulating two separate pre-commit/prek worker processes)
    rather than letting one silently clobber the other's already-applied fix.
    """

    __slots__ = ("marker", "write_delay_seconds")

    check_id = "appending-fix-probe"
    error_code = "ZZZ003"

    def __init__(self, marker: str, *, write_delay_seconds: float = 0.0) -> None:
        super().__init__()
        self.marker = marker
        self.write_delay_seconds = write_delay_seconds

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> list[Violation]:
        if f"# {self.marker}" in source:
            return []
        return [
            Violation(check_id=self.check_id, error_code=self.error_code, line=1, col=0, message="probe", fixable=True)
        ]

    def fix(
        self, filepath: Path, _violations: list[Violation], source: str, _tree: ast.Module, encoding: str = "utf-8"
    ) -> FixResult:
        if self.write_delay_seconds:
            time.sleep(self.write_delay_seconds)
        atomic_write_text(filepath, f"{source}# {self.marker}\n", encoding, source)
        return FixResult.for_violations(_violations, FixOutcome.APPLIED)


def test_check_file_serializes_concurrent_fixes_to_the_same_file_across_orchestrators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulates two separate CheckOrchestrator instances (standing in for two separate pre-commit/prek
    # worker processes, e.g. two overlapping hook configurations, or a future check implementing
    # drain_cross_file_candidates() while also being fixable -- redundant-type-conversion's own drain
    # can't trigger this today, since that check never writes) both fixing the same file at once. Without
    # _check_file's own per-file lock, whichever write lands last would silently discard the other's
    # already-applied fix (a classic lost update), since both would read the same pre-fix content.
    monkeypatch.chdir(tmp_path)
    shared_file = tmp_path / "shared.py"
    shared_file.write_text("x = 1\n")

    slow_check = _AppendingFixCheck("marker_a", write_delay_seconds=0.3)
    fast_check = _AppendingFixCheck("marker_b")
    slow_orchestrator = CheckOrchestrator(checks=[slow_check], fix_mode=True)
    fast_orchestrator = CheckOrchestrator(checks=[fast_check], fix_mode=True)

    thread = threading.Thread(target=slow_orchestrator._check_file, args=(shared_file, [slow_check]))
    thread.start()
    time.sleep(0.1)  # lets the slow orchestrator acquire the lock and start its own (slow) fix first
    fast_orchestrator._check_file(shared_file, [fast_check])
    thread.join(timeout=5)
    assert not thread.is_alive()

    final_content = shared_file.read_text()
    assert "# marker_a" in final_content
    assert "# marker_b" in final_content


class _ExternallyModifiedFixCheck(_AlwaysRerunProbeCheck):
    """A `fix()` that, when `simulate_external_edit` is set, writes an
    out-of-band edit (standing in for an editor, or a concurrent process
    outside this tool's own per-file fix lock) to `filepath` between
    `_apply_fixes`' own read and this check's own write -- proves
    `atomic_write_text()`'s source-identity check actually aborts the write
    instead of silently clobbering that edit, the scenario `_check_file`'s
    own lock (see `_AppendingFixCheck` above) cannot cover because the other
    writer here never goes through this tool at all. With it unset, `fix()`
    behaves like an ordinary, uncontested single-write check.
    """

    __slots__ = ("simulate_external_edit",)

    check_id = "externally-modified-fix-probe"
    error_code = "ZZZ004"

    def __init__(self, *, simulate_external_edit: bool) -> None:
        super().__init__()
        self.simulate_external_edit = simulate_external_edit

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> list[Violation]:
        if "# my fix" in source:
            return []
        return [
            Violation(check_id=self.check_id, error_code=self.error_code, line=1, col=0, message="probe", fixable=True)
        ]

    def fix(
        self, filepath: Path, _violations: list[Violation], source: str, _tree: ast.Module, encoding: str = "utf-8"
    ) -> FixResult:
        if self.simulate_external_edit:
            filepath.write_text(f"{source}# external edit\n", encoding=encoding)
        atomic_write_text(filepath, f"{source}# my fix\n", encoding, source)
        return FixResult.for_violations(_violations, FixOutcome.APPLIED)


def test_apply_fixes_applies_normally_when_nothing_else_touches_the_file(tmp_path: Path) -> None:
    # Companion to the abort case below: the identity check must never
    # reject an ordinary, uncontested fix just because it exists.
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")
    check = _ExternallyModifiedFixCheck(simulate_external_edit=False)

    orchestrator = CheckOrchestrator(checks=[check], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    violation = violations[str(filepath)][0]
    assert violation.fix_outcome is FixOutcome.APPLIED
    assert filepath.read_text() == "x = 1\n# my fix\n"


def test_apply_fixes_aborts_when_file_is_externally_modified_during_fix(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")
    check = _ExternallyModifiedFixCheck(simulate_external_edit=True)

    orchestrator = CheckOrchestrator(checks=[check], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    violation = violations[str(filepath)][0]
    assert violation.fix_outcome is FixOutcome.ABORTED
    assert violation.fix_outcome is not FixOutcome.REJECTED
    assert violation.fix_outcome is not FixOutcome.ERRORED
    assert violation.fix_outcome is not FixOutcome.APPLIED
    # The check's own fix was discarded; the externally written content
    # (what a real concurrent editor/process would have left behind) is what
    # survives on disk, not a silent merge of both writes.
    assert filepath.read_text() == "x = 1\n# external edit\n"


def test_check_file_returns_none_when_the_fix_lock_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_orchestrator, "_FIX_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(_orchestrator, "_FIX_LOCK_POLL_INTERVAL_SECONDS", 0.02)
    target = tmp_path / "target.py"
    target.write_text("x = 1\n")
    check = _AlwaysRerunProbeCheck()
    orchestrator = CheckOrchestrator(checks=[check], fix_mode=True)
    lock_path = _orchestrator._fix_lock_path(target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with locked(lock_path, timeout_seconds=5.0, poll_interval_seconds=0.02):
        lock_result = orchestrator._check_file(target, [check])

    assert lock_result is None


def test_check_file_returns_none_when_the_fix_lock_directory_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.py"
    target.write_text("x = 1\n")
    unavailable_parent = tmp_path / "not-a-directory"
    unavailable_parent.write_text("unavailable\n")
    monkeypatch.setattr(_orchestrator, "_fix_lock_path", lambda _filepath: unavailable_parent / "lock")
    check = _AlwaysRerunProbeCheck()
    orchestrator = CheckOrchestrator(checks=[check], fix_mode=True)

    assert orchestrator._check_file(target, [check]) is None


def test_fix_lock_path_is_independent_of_the_callers_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.py"
    target.write_text("x = 1\n")
    nested_directory = tmp_path / "nested"
    nested_directory.mkdir()

    monkeypatch.chdir(tmp_path)
    root_lock_path = _orchestrator._fix_lock_path(target)
    monkeypatch.chdir(nested_directory)
    nested_lock_path = _orchestrator._fix_lock_path(target)

    assert nested_lock_path == root_lock_path


class _CrashingAlwaysRerunCheck(_AlwaysRerunProbeCheck):
    __slots__ = ()

    check_id = "crashing-always-rerun-probe"

    def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
        raise ValueError("simulated always-rerun check failure")


def test_process_files_a_non_cacheable_checks_own_crash_does_not_block_caching_a_cacheable_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A combined had_rule_failure flag must not gate caching on whether
    # *any* check in this call crashed, cacheable or not -- that would let
    # an always-rerun check's own crash silently disable caching for an
    # unrelated, successfully-completed cacheable check sharing the same
    # file, forcing it to be needlessly recomputed on every future run.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    orchestrator = CheckOrchestrator(
        checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE), _CrashingAlwaysRerunCheck()]
    )
    first = orchestrator.process_files([str(filepath)])
    assert {v.error_code for v in first[str(filepath)]} == {"TR1"}
    assert orchestrator.rule_failures == [(str(filepath), "crashing-always-rerun-probe")]  # pytriage: TR6

    monkeypatch.setattr(
        MeaninglessVarsCheck,
        "check",
        raises(AssertionError, "meaningless-vars must have been cached despite the always-rerun check's own crash"),
    )

    second_orchestrator = CheckOrchestrator(
        checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE), _CrashingAlwaysRerunCheck()]
    )
    second = second_orchestrator.process_files([str(filepath)])
    assert {v.error_code for v in second[str(filepath)]} == {"TR1"}


def test_process_files_non_cacheable_check_never_serves_a_stale_result(tmp_path: Path) -> None:
    # A non-cacheable check's own violations must never come from the
    # cache: its result can depend on state outside the single file it was
    # given (e.g. another file's current type), which the cache has no way
    # to invalidate on.
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    probe = _AlwaysRerunProbeCheck(message="first")
    orchestrator = CheckOrchestrator(checks=[probe])
    first = orchestrator.process_files([str(filepath)])
    assert first[str(filepath)][0].message == "first"
    assert probe.call_count == 1

    probe.message = "second"
    second = orchestrator.process_files([str(filepath)])
    assert second[str(filepath)][0].message == "second"
    assert probe.call_count == 2


def test_process_files_non_cacheable_check_results_are_never_written_to_cache(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    orchestrator = CheckOrchestrator(checks=[_AlwaysRerunProbeCheck()])
    orchestrator.process_files([str(filepath)])

    cached = orchestrator.cache.get_cached_result(filepath, orchestrator.cache.hook_name)
    assert cached is None


def test_process_files_non_cacheable_check_does_not_disturb_a_cacheable_checks_own_caching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ch. issue #108: adding an always-rerun check alongside an already
    # cacheable one must not silently disable the cacheable check's own
    # caching for files they share.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    meaningless_vars_only = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)])
    meaningless_vars_only.process_files([str(filepath)])

    monkeypatch.setattr(
        MeaninglessVarsCheck,
        "check",
        raises(AssertionError, "a cacheable check must not be recomputed once already cached"),
    )

    probe = _AlwaysRerunProbeCheck()
    combined = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE), probe])
    violations = combined.process_files([str(filepath)])

    error_codes = {v.error_code for v in violations[str(filepath)]}
    assert error_codes == {"TR1", "ZZZ001"}
    assert probe.call_count == 1


def test_process_single_file_reports_unprocessable_when_always_rerun_group_fails_after_a_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cache hit for the cacheable group must not silently swallow a
    # read/parse failure in the always-rerun group's own fresh check --
    # e.g. a race where the file becomes unreadable between the cache
    # lookup and the always-rerun group's own _check_file() call.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    meaningless_vars_only = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)])
    meaningless_vars_only.process_files([str(filepath)])  # populates the cache

    combined = CheckOrchestrator(
        checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE), _AlwaysRerunProbeCheck()]
    )

    # MeaninglessVarsCheck's own result for this file is already cached above, so
    # _process_single_file only ever calls _check_file for the always-rerun
    # group (checks == [_AlwaysRerunProbeCheck()]) here -- nothing else calls this stub.
    def unreadable_always_rerun_group(
        _self: CheckOrchestrator, _fp: Path, _checks: list[ASTCheck], **_kwargs: object
    ) -> list[Violation] | None:
        return None

    monkeypatch.setattr(CheckOrchestrator, "_check_file", unreadable_always_rerun_group)

    violations = combined.process_files([str(filepath)])

    assert violations == {}
    assert combined.unprocessable_files == [str(filepath)]  # pytriage: TR6


def test_process_files_enabling_a_non_cacheable_check_does_not_change_cache_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_root = tmp_path / "pre_commit_hooks"
    fake_root.mkdir()
    (fake_root / "module.py").write_text("x = 1\n")
    monkeypatch.setattr(_orchestrator, "_PACKAGE_ROOT", fake_root)

    without_probe = CheckOrchestrator(checks=[MeaninglessVarsCheck()])
    with_probe = CheckOrchestrator(checks=[MeaninglessVarsCheck(), _AlwaysRerunProbeCheck()])
    assert without_probe._generate_cache_version() == with_probe._generate_cache_version()
    assert without_probe.cache.hook_name == with_probe.cache.hook_name


def test_fix_mode_skips_a_file_entirely_on_a_clean_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # See ADR-0044: a fix-mode cache hit reporting zero violations means
    # there is nothing to fix, so the file never needs to be read or
    # parsed at all on a later run.
    filepath = tmp_path / "module.py"
    # "result" only appears in prose, never as an actual assignment target,
    # so meaningless-vars' own content prefilter matches this file (it
    # greps for the literal word) while its real, AST-based check() finds
    # nothing to flag.
    filepath.write_text('"""Doc mentioning a result value."""\n\n\ndef greet() -> str:\n    return "hi"\n')

    first = CheckOrchestrator(checks=[MeaninglessVarsCheck()], fix_mode=True)
    first_violations = first.process_files([str(filepath)])
    assert first_violations == {}
    # Guards against this test passing "for free" because the file never
    # matched meaningless-vars' own prefilter in the first place.
    assert first.cache.get_cached_result(filepath, first.cache.hook_name) is not None

    monkeypatch.setattr(
        CheckOrchestrator, "_check_file", raises(AssertionError, "_check_file should not run on a clean cache hit")
    )
    second = CheckOrchestrator(checks=[MeaninglessVarsCheck()], fix_mode=True)
    second_violations = second.process_files([str(filepath)])
    assert second_violations == {}


def test_fix_mode_clean_cacheable_hit_still_fixes_a_dirty_always_rerun_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # See ADR-0044: a clean cacheable-group cache hit doesn't skip the
    # always-rerun group -- it still runs fresh, and in fix mode still gets
    # fixed if it finds anything, without re-running the (already known
    # clean) cacheable group.
    filepath = tmp_path / "module.py"
    # "result" (prose only, see test_fix_mode_skips_a_file_entirely_on_a_clean_cache_hit)
    # keeps meaningless-vars clean while still matching its own prefilter;
    # the 3 blank lines are what excessive-blank-lines needs to fix.
    filepath.write_text('"""Doc mentioning a result value."""\n\n\n\ndef greet() -> str:\n    return "hi"\n')

    populate = CheckOrchestrator(checks=[MeaninglessVarsCheck()], fix_mode=True)
    populate.process_files([str(filepath)])  # clean for meaningless-vars; populates its cache entry
    assert populate.cache.get_cached_result(filepath, populate.cache.hook_name) is not None

    monkeypatch.setattr(
        MeaninglessVarsCheck, "check", raises(AssertionError, "meaningless-vars must be served from its cache hit")
    )
    monkeypatch.setattr(ExcessiveBlankLinesCheck, "cacheable", False)

    combined = CheckOrchestrator(checks=[MeaninglessVarsCheck(), ExcessiveBlankLinesCheck()], fix_mode=True)
    combined.process_files([str(filepath)])

    assert "def greet" in filepath.read_text()
    assert "\n\n\n\n" not in filepath.read_text()


def test_fix_mode_falls_through_to_a_full_recompute_when_cache_hit_shows_a_violation(tmp_path: Path) -> None:
    # See ADR-0044: cached violations never carry fix_data, so a fix-mode
    # cache hit that reports a violation can't be used to fix it -- it must
    # fall through to a real check()+fix() pass, the same as a cache miss.
    filepath = tmp_path / "module.py"
    filepath.write_text('"""Doc."""\n\n\n\ndef greet() -> str:\n    return "hi"\n')

    check_only = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()])
    populated = check_only.process_files([str(filepath)])
    assert populated[str(filepath)][0].error_code == "TR2"  # pytriage: TR6
    assert check_only.cache.get_cached_result(filepath, check_only.cache.hook_name) is not None

    fixer = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()], fix_mode=True)
    fixer.process_files([str(filepath)])

    assert "\n\n\n\n" not in filepath.read_text()


def test_fix_mode_does_not_cache_a_run_that_actually_changed_the_file(tmp_path: Path) -> None:
    # See ADR-0044 and _apply_fixes' own docstring: a run where a fix
    # changed the file is never cached, even for the check that was
    # fixed -- a check with zero violations elsewhere in the same run is
    # never re-verified against the file's final content once some other
    # check's fix touched it, so nothing in the group is known-accurate
    # this run. Caching a changed file's result anyway would let a sibling
    # check's own stale "clean" entry survive being served after another
    # check's fix invalidated it.
    filepath = tmp_path / "module.py"
    filepath.write_text('"""Doc."""\n\n\n\ndef greet() -> str:\n    return "hi"\n')

    fixer = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()], fix_mode=True)
    fixer.process_files([str(filepath)])
    assert "\n\n\n\n" not in filepath.read_text()
    assert fixer.cache.get_cached_result(filepath, fixer.cache.hook_name) is None


def test_fix_mode_writes_cache_once_the_file_stops_changing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # See ADR-0044: fix mode writes to the cache too, so a file fixed once
    # converges to a clean cache hit on the first run after it, once that
    # run's own fix pass no longer changes anything.
    filepath = tmp_path / "module.py"
    filepath.write_text('"""Doc."""\n\n\n\ndef greet() -> str:\n    return "hi"\n')

    fixer = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()], fix_mode=True)
    fixer.process_files([str(filepath)])
    assert fixer.cache.get_cached_result(filepath, fixer.cache.hook_name) is None

    settled = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()], fix_mode=True)
    settled.process_files([str(filepath)])
    cached = settled.cache.get_cached_result(filepath, settled.cache.hook_name)
    assert cached is not None
    assert cached["violations"] == []

    monkeypatch.setattr(
        CheckOrchestrator, "_check_file", raises(AssertionError, "_check_file should not run on a clean cache hit")
    )
    rerun = CheckOrchestrator(checks=[ExcessiveBlankLinesCheck()], fix_mode=True)
    assert rerun.process_files([str(filepath)]) == {}


def test_fix_mode_does_not_cache_a_check_id_with_a_rejected_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # See ADR-0044: _refresh_stale_positions() leaves a check_id's other
    # still-open violations unrefreshed once any of its violations hits a
    # terminal negative fix outcome this run -- caching that check_id's
    # results anyway could serve a stale position later, so it must be
    # excluded from the write entirely, the same way an incomplete
    # (crashed/unavailable) check_id already is.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "\n\n\nimport requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    def broken_fix(
        _self: MeaninglessVarsCheck, fp: Path, _violations: object, source: str, *_args: object, **_kwargs: object
    ) -> None:
        atomic_write_text(fp, "def broken(:\n", "utf-8", source)

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", broken_fix)

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])
    assert violations[str(filepath)][0].fix_outcome is FixOutcome.REJECTED

    assert orchestrator.cache.get_cached_result(filepath, orchestrator.cache.hook_name) is None


def test_fix_mode_two_configs_do_not_collide_on_the_same_files_cache_entry(tmp_path: Path) -> None:
    # See ADR-0044: before the hook_name/cache_version split, two
    # orchestrators sharing one hardcoded hook_name but a different
    # cache_version (from differing check config, e.g. this repo's own
    # default vs. permissive --meaningless-vars-level hooks) would wipe
    # each other's entire per-file cache blob on write -- every run of one
    # config would evict whatever the other config's own last run wrote,
    # forcing both into a permanent cache miss against each other forever.
    filepath = tmp_path / "module.py"
    # "result" (prose only) matches both instances' own prefilter (the name
    # set doesn't vary by level -- only the reporting threshold does) while
    # staying clean for both, so neither run performs a fix that would
    # change the file's content hash between the two writes below.
    filepath.write_text('"""Doc mentioning a result value."""\n\n\ndef greet() -> str:\n    return "hi"\n')

    default = CheckOrchestrator(checks=[MeaninglessVarsCheck()], fix_mode=True)
    default.process_files([str(filepath)])

    permissive = CheckOrchestrator(checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE)], fix_mode=True)
    permissive.process_files([str(filepath)])

    assert default.cache.get_cached_result(filepath, default.cache.hook_name) is not None
    assert permissive.cache.get_cached_result(filepath, permissive.cache.hook_name) is not None


def test_check_unavailable_error_is_recorded_once_and_disables_that_check(tmp_path: Path) -> None:
    class _UnavailableCheck(_AlwaysRerunProbeCheck):
        __slots__ = ("attempts",)

        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
            self.attempts += 1
            raise CheckUnavailableError("some prerequisite is missing")

    filepath_1 = tmp_path / "module1.py"
    filepath_1.write_text("x = 1\n")
    filepath_2 = tmp_path / "module2.py"
    filepath_2.write_text("y = 2\n")

    check = _UnavailableCheck()
    orchestrator = CheckOrchestrator(checks=[check])
    all_violations = orchestrator.process_files([str(filepath_1), str(filepath_2)])

    # Deliberately not recorded as an ordinary rule_failures entry — see
    # CheckUnavailableError's own docstring.
    assert orchestrator.rule_failures == []
    assert orchestrator.unavailable_checks == [("always-rerun-probe", "some prerequisite is missing")]
    assert all_violations == {}
    # Attempted on the first file only -- the second file's own call is
    # skipped entirely rather than failing (and recording) all over again.
    assert check.attempts == 1


def test_check_unavailable_error_does_not_discard_other_checks_results(tmp_path: Path) -> None:
    class _UnavailableCheck(_AlwaysRerunProbeCheck):
        def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
            raise CheckUnavailableError("some prerequisite is missing")

    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    orchestrator = CheckOrchestrator(
        checks=[MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE), _UnavailableCheck()]
    )
    all_violations = orchestrator.process_files([str(filepath)])

    assert {v.check_id for v in all_violations[str(filepath)]} == {"meaningless-vars"}
    assert orchestrator.unavailable_checks == [("always-rerun-probe", "some prerequisite is missing")]


def test_refresh_stale_positions_skips_a_check_already_known_unavailable(tmp_path: Path) -> None:
    # A check that raised CheckUnavailableError during this same file's own
    # _check_file pass is already recorded in _unavailable_check_ids by the
    # time a different check's fix (here, MeaninglessVarsCheck's own rename)
    # triggers _refresh_stale_positions for that file -- it must not be
    # attempted (and fail) all over again there.
    class _UnavailableCheck(_AlwaysRerunProbeCheck):
        __slots__ = ("attempts",)

        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
            self.attempts += 1
            raise CheckUnavailableError("some prerequisite is missing")

    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    check = _UnavailableCheck()
    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck(), check], fix_mode=True)
    orchestrator.process_files([str(filepath)])

    assert filepath.read_text() == (
        "import requests\n\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n"
    )
    assert check.attempts == 1
    assert orchestrator.unavailable_checks == [("always-rerun-probe", "some prerequisite is missing")]


def test_main_reports_check_unavailable_error_once_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _UnavailableCheck(_AlwaysRerunProbeCheck):
        check_id = "unavailable-probe"
        error_code = "ZZZ002"

        def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
            raise CheckUnavailableError("some prerequisite is missing; install it and retry")

    monkeypatch.setattr(_cli, "ALL_CHECKS", [*ALL_CHECKS, _UnavailableCheck])
    monkeypatch.setattr(_orchestrator, "ALL_CHECKS", [*ALL_CHECKS, _UnavailableCheck])

    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    exit_code = main([str(filepath), "--select", "unavailable-probe"])

    assert exit_code == 1
    assert capsys.readouterr().err.count("some prerequisite is missing") == 1


def test_apply_fixes_skips_check_with_no_fixable_violations(tmp_path: Path) -> None:
    # redundant-super-init never marks violations fixable; when mixed with
    # a fixable meaningless-vars violation in the same file, its check must be
    # skipped in the fix loop rather than attempting (and no-op'ing) a fix.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\n"
        "def request():\n"
        "    data = requests.get(url)\n"
        "    return data.status_code\n"
        "\n\n"
        "class Base:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n\n"
        "class Child(Base):\n"
        "    def __init__(self, **kwargs):\n"
        "        super().__init__(**kwargs)\n"
    )

    checks = load_checks(select={"meaningless-vars", "redundant-super-init"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_check = {v.check_id: v for v in violations[str(filepath)]}
    assert by_check["meaningless-vars"].fix_outcome is FixOutcome.APPLIED
    assert by_check["redundant-super-init"].fixable is False


def test_apply_fixes_does_not_mark_fixed_a_violation_fix_left_untouched(tmp_path: Path) -> None:
    # validate-function-name's fix() loops over violations and
    # can skip some (should_autofix refuses to rename methods) while fixing
    # others in the same call. _apply_fixes must not mark every violation
    # of a check as fixed just because fix() returned True at all,
    # regardless of whether that specific violation was actually touched —
    # a rename that should_autofix skips must not be reported [FIXED]
    # while the file still has the old name.
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

    assert by_func_name["get_config"].fix_outcome is FixOutcome.APPLIED
    assert by_func_name["get_data"].fix_outcome is not FixOutcome.APPLIED
    fixed_content = filepath.read_text()
    assert "def get_config" not in fixed_content
    assert "def get_data(self):" in fixed_content


def test_apply_fixes_distinguishes_violations_with_identical_messages(tmp_path: Path) -> None:
    # a free function and an unrelated method can produce
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

    assert by_line[1].fix_outcome is FixOutcome.APPLIED
    assert by_line[7].fix_outcome is not FixOutcome.APPLIED
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
    filepath.write_text(
        "\n\n\nimport requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    def broken_fix(
        _self: MeaninglessVarsCheck, fp: Path, _violations: object, source: str, *_args: object, **_kwargs: object
    ) -> None:
        atomic_write_text(fp, "def broken(:\n", "utf-8", source)

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", broken_fix)

    checks: list[ASTCheck] = [MeaninglessVarsCheck(), ExcessiveBlankLinesCheck()]
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_check = {v.check_id: v for v in violations[str(filepath)]}
    meaningless_vars_violation = by_check["meaningless-vars"]
    blank_lines_violation = by_check["excessive-blank-lines"]

    assert meaningless_vars_violation.fix_outcome is FixOutcome.REJECTED
    assert meaningless_vars_violation.fix_outcome is not FixOutcome.APPLIED
    assert blank_lines_violation.fix_outcome is not FixOutcome.REJECTED
    assert blank_lines_violation.fix_outcome is FixOutcome.APPLIED
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
    filepath.write_text(
        "\n\n\nimport requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    def broken_fix(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated fix bug")

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", broken_fix)

    checks: list[ASTCheck] = [MeaninglessVarsCheck(), ExcessiveBlankLinesCheck()]
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_check = {v.check_id: v for v in violations[str(filepath)]}
    meaningless_vars_violation = by_check["meaningless-vars"]
    blank_lines_violation = by_check["excessive-blank-lines"]

    assert meaningless_vars_violation.fix_outcome is FixOutcome.ERRORED
    assert meaningless_vars_violation.fix_outcome is not FixOutcome.REJECTED
    assert meaningless_vars_violation.fix_outcome is not FixOutcome.APPLIED
    assert blank_lines_violation.fix_outcome is not FixOutcome.ERRORED
    assert blank_lines_violation.fix_outcome is FixOutcome.APPLIED
    assert "data = requests.get(url)" in filepath.read_text()


def test_apply_fixes_marks_already_resolved_violation_fixed_not_errored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a check that writes more than once per fix() call
    # (looping over violations individually, like validate_function_name)
    # can commit some violations before a later write raises. Marking every
    # violation in the batch [FIX ERRORED] regardless would misreport an
    # already-applied fix as "not applied", leaving the user unable to tell
    # which change actually landed on disk. Only the violation(s) still
    # present after the crash must be marked errored.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\n"
        "def first():\n"
        "    data = requests.get(url)\n"
        "    return data.status_code\n\n"
        "def second():\n"
        "    result = requests.get(url)\n"
        "    return result.status_code\n"
    )

    def partial_then_raise(
        _self: MeaninglessVarsCheck, fp: Path, _violations: object, source: str, *_args: object, **_kwargs: object
    ) -> None:
        # Simulates a multi-write check that already committed the fix for
        # "data" before crashing while attempting "result".
        atomic_write_text(
            fp,
            "import requests\n\n"
            "def first():\n"
            "    response = requests.get(url)\n"
            "    return response.status_code\n\n"
            "def second():\n"
            "    result = requests.get(url)\n"
            "    return result.status_code\n",
            "utf-8",
            source,
        )
        raise RuntimeError("simulated fix bug partway through")

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", partial_then_raise)

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    by_line = {v.line: v for v in violations[str(filepath)]}
    data_violation = by_line[4]
    result_violation = by_line[8]

    assert data_violation.fix_outcome is FixOutcome.APPLIED
    assert data_violation.fix_outcome is not FixOutcome.ERRORED

    assert result_violation.fix_outcome is FixOutcome.ERRORED
    assert result_violation.fix_outcome is not FixOutcome.APPLIED

    fixed_content = filepath.read_text()
    assert "response = requests.get(url)" in fixed_content
    assert "result = requests.get(url)" in fixed_content


def test_apply_fixes_records_rule_failure_when_fix_raises_after_resolving_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # if fix() commits every requested edit successfully and
    # then raises afterwards (e.g. during unrelated cleanup), every
    # violation ends up correctly marked [FIXED] and none are left to mark
    # [FIX ERRORED] — but the exception itself must still be recorded
    # somewhere, or a genuine internal failure would be completely
    # invisible behind what looks like a fully successful fix.
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    def fix_then_raise(
        _self: MeaninglessVarsCheck, fp: Path, _violations: object, source: str, *_args: object, **_kwargs: object
    ) -> None:
        atomic_write_text(
            fp,
            "import requests\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n",
            "utf-8",
            source,
        )
        raise RuntimeError("simulated cleanup bug after a successful fix")

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", fix_then_raise)

    orchestrator = CheckOrchestrator(checks=[MeaninglessVarsCheck()], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    violation = violations[str(filepath)][0]
    assert violation.fix_outcome is FixOutcome.APPLIED
    assert violation.fix_outcome is not FixOutcome.ERRORED
    assert orchestrator.rule_failures == [(str(filepath), "meaningless-vars")]  # pytriage: TR6
    assert filepath.read_text() == (
        "import requests\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n"
    )


def _write_get_config_and_get_active_module(tmp_path: Path) -> Path:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "def get_config():\n"
        '    with open("config.json") as f:\n'
        "        return f.read()\n"
        "\n\n"
        "def get_active(user: dict) -> bool:\n"
        '    return user.get("status") == "active"\n'
    )
    return filepath


def _run_vfn_fix_with_patched_apply_fix(
    filepath: Path,
    monkeypatch: pytest.MonkeyPatch,
    flaky_apply_fix: Callable[[Path, Suggestion], FixOutcome],
) -> dict[str, Violation]:
    monkeypatch.setattr(vfn_module, "apply_fix", flaky_apply_fix)

    checks = load_checks(select={"validate-function-name"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    return {v.fix_data["suggestion"].func_name: v for v in violations[str(filepath)] if v.fix_data}


def test_apply_fixes_marks_only_the_rejected_violation_of_a_multi_write_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # validate-function-name's fix() writes once per violation rather than
    # once per check. When one of those writes is rejected, the orchestrator
    # must attribute the rejection to that specific violation, not the
    # whole check — an earlier rename that already committed must still be
    # reported [FIXED], not swept into [FIX REJECTED] alongside it.
    filepath = _write_get_config_and_get_active_module(tmp_path)
    original_apply_fix = vfn_module.apply_fix

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> FixOutcome:
        if suggestion.func_name == "get_active":
            atomic_write_text(fp, "def broken(:\n", "utf-8", fp.read_text())
        return original_apply_fix(fp, suggestion)

    by_func_name = _run_vfn_fix_with_patched_apply_fix(filepath, monkeypatch, flaky_apply_fix)
    get_config_violation = by_func_name["get_config"]
    get_active_violation = by_func_name["get_active"]
    assert get_config_violation.fix_outcome is FixOutcome.APPLIED
    assert get_config_violation.fix_outcome is not FixOutcome.REJECTED
    assert get_active_violation.fix_outcome is FixOutcome.REJECTED
    assert get_active_violation.fix_outcome is not FixOutcome.APPLIED

    fixed_content = filepath.read_text()
    assert "def get_config" not in fixed_content
    assert 'def get_active(user: dict) -> bool:\n    return user.get("status") == "active"\n' in fixed_content


def test_apply_fixes_marks_only_the_aborted_violation_of_a_multi_write_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same per-violation scoping as the [FIX REJECTED] case above, but for a
    # concurrent on-disk modification: an earlier rename in this loop that
    # already committed must still be reported [FIXED], not swept into
    # [FIX ABORTED] alongside a later violation whose own write collided
    # with an on-disk change.
    filepath = _write_get_config_and_get_active_module(tmp_path)
    original_apply_fix = vfn_module.apply_fix

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> FixOutcome:
        if suggestion.func_name == "get_active":
            atomic_write_text(fp, "def get_active(user):\n    pass\n", "utf-8", "not the real current content")
        return original_apply_fix(fp, suggestion)

    by_func_name = _run_vfn_fix_with_patched_apply_fix(filepath, monkeypatch, flaky_apply_fix)
    get_config_violation = by_func_name["get_config"]
    get_active_violation = by_func_name["get_active"]
    assert get_config_violation.fix_outcome is FixOutcome.APPLIED
    assert get_config_violation.fix_outcome is not FixOutcome.ABORTED
    assert get_active_violation.fix_outcome is FixOutcome.ABORTED
    assert get_active_violation.fix_outcome is not FixOutcome.APPLIED

    fixed_content = filepath.read_text()
    assert "def get_config" not in fixed_content
    assert 'def get_active(user: dict) -> bool:\n    return user.get("status") == "active"\n' in fixed_content


def test_apply_fixes_keeps_aborted_violation_aborted_even_when_the_external_edit_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The post-fix recheck (_mark_resolved_and_get_still_present) must never
    # report [FIXED] just because a violation is no longer detected -- the
    # very external edit that caused the abort can easily be the reason it's
    # gone (e.g. someone else already renamed the function), but this run's
    # own write never landed and must not be credited for that. get_config
    # is an unrelated, unaffected violation in the same batch, proving the
    # fix above doesn't regress an ordinary successful multi-write fix.
    filepath = _write_get_config_and_get_active_module(tmp_path)
    original_apply_fix = vfn_module.apply_fix

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> FixOutcome:
        if suggestion.func_name == "get_active":
            stale_source = fp.read_text()
            # Simulate a concurrent process independently renaming the
            # function to a compliant name between should_autofix()'s own
            # read and this apply_fix() call.
            fp.write_text(stale_source.replace("def get_active(", "def is_active("))
            atomic_write_text(fp, "def get_active(user):\n    pass\n", "utf-8", stale_source)
        return original_apply_fix(fp, suggestion)

    by_func_name = _run_vfn_fix_with_patched_apply_fix(filepath, monkeypatch, flaky_apply_fix)
    get_active_violation = by_func_name["get_active"]
    assert get_active_violation.fix_outcome is FixOutcome.ABORTED
    assert get_active_violation.fix_outcome is not FixOutcome.APPLIED

    get_config_violation = by_func_name["get_config"]
    assert get_config_violation.fix_outcome is FixOutcome.APPLIED

    assert 'def is_active(user: dict) -> bool:\n    return user.get("status") == "active"\n' in filepath.read_text()


def test_apply_fixes_keeps_aborted_violation_aborted_when_the_external_edit_leaves_invalid_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The concurrent edit that aborts get_active's own fix also happens to
    # overwrite the whole file with invalid Python (a half-written save, a
    # merge conflict marker) -- destroying get_config's own already-applied
    # rename too. The post-fix recheck (_mark_resolved_and_get_still_present)
    # must not let that SyntaxError escape into _apply_fixes' outer except
    # Exception, which would blanket-mark every fixable violation for this
    # check [FIX ERRORED] and record a spurious rule failure. get_active's
    # own [FIX ABORTED] (decided before the file was clobbered) must survive
    # untouched; get_config -- genuinely unconfirmable now that the file no
    # longer parses -- must fall back to ordinary [FIXABLE], not a false
    # [FIX ERRORED] for a check that never actually raised.
    filepath = _write_get_config_and_get_active_module(tmp_path)
    original_apply_fix = vfn_module.apply_fix

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> FixOutcome:
        if suggestion.func_name == "get_active":
            fp.write_text("this is not valid python (((\n")
            atomic_write_text(fp, "def get_active(user):\n    pass\n", "utf-8", "stale content that won't match")
        return original_apply_fix(fp, suggestion)

    by_func_name = _run_vfn_fix_with_patched_apply_fix(filepath, monkeypatch, flaky_apply_fix)
    get_active_violation = by_func_name["get_active"]
    assert get_active_violation.fix_outcome is FixOutcome.ABORTED
    assert get_active_violation.fix_outcome is not FixOutcome.ERRORED
    assert get_active_violation.fix_outcome is not FixOutcome.APPLIED

    get_config_violation = by_func_name["get_config"]
    assert get_config_violation.fix_outcome is not FixOutcome.ERRORED
    assert get_config_violation.fix_outcome is not FixOutcome.APPLIED

    assert filepath.read_text() == "this is not valid python (((\n"


def test_apply_fixes_marks_errored_violation_of_a_multi_write_check_when_apply_fix_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # validate-function-name's own fix() already catches any
    # exception apply_fix() raises other than FixValidationError internally
    # (logging it and moving on to the next violation), so it never
    # propagates to CheckOrchestrator._apply_fixes' own [FIX ERRORED]
    # handling at all — that handling is only reachable for single-write
    # checks. The check itself must mark the specific violation it failed
    # to fix, the same way it already does for a rejected fix.
    filepath = _write_get_config_and_get_active_module(tmp_path)
    original_apply_fix = vfn_module.apply_fix

    def flaky_apply_fix(fp: Path, suggestion: Suggestion) -> FixOutcome:
        if suggestion.func_name == "get_active":
            raise RuntimeError("simulated apply_fix bug")
        return original_apply_fix(fp, suggestion)

    by_func_name = _run_vfn_fix_with_patched_apply_fix(filepath, monkeypatch, flaky_apply_fix)
    get_config_violation = by_func_name["get_config"]
    get_active_violation = by_func_name["get_active"]
    assert get_config_violation.fix_outcome is FixOutcome.APPLIED
    assert get_config_violation.fix_outcome is not FixOutcome.ERRORED
    assert get_active_violation.fix_outcome is FixOutcome.ERRORED
    assert get_active_violation.fix_outcome is not FixOutcome.REJECTED
    assert get_active_violation.fix_outcome is not FixOutcome.APPLIED

    fixed_content = filepath.read_text()
    assert "def get_config" not in fixed_content
    assert "def get_active(user: dict) -> bool:" in fixed_content


def _disappear_before_refetch(
    _orchestrator: CheckOrchestrator, _meaningless_vars: MeaninglessVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the file can't be re-read inside _apply_fixes (e.g. deleted by a
    # concurrent process), that check's fix is skipped rather than crashing.
    original_read = CheckOrchestrator._read_source
    calls = {"n": 0}

    def flaky_read(self: CheckOrchestrator, fp: Path) -> tuple[str, str] | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_read(self, fp)
        return None

    monkeypatch.setattr(CheckOrchestrator, "_read_source", flaky_read)


def _disappear_after_fix(
    _orchestrator: CheckOrchestrator, _meaningless_vars: MeaninglessVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the file can't be re-read for the post-fix verification (e.g.
    # deleted right after the fix wrote it), the fix isn't reported as
    # fixed even though it actually happened — this run just can't confirm
    # it, so it stays conservative rather than guessing.
    original_read = CheckOrchestrator._read_source
    calls = {"n": 0}

    def flaky_read(self: CheckOrchestrator, fp: Path) -> tuple[str, str] | None:
        calls["n"] += 1
        # Call 1 is _check_file's own initial read, call 2 is _apply_fixes'
        # pre-fix read — both must succeed so the real fix actually runs.
        # Call 3 is the new post-fix verification read.
        if calls["n"] <= 2:
            return original_read(self, fp)
        return None

    monkeypatch.setattr(CheckOrchestrator, "_read_source", flaky_read)


def _recompute_finds_no_fixable_violations(
    _orchestrator: CheckOrchestrator, meaningless_vars: MeaninglessVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_check = meaningless_vars.check
    calls = {"n": 0}

    def flaky_check(_self: MeaninglessVarsCheck, fp: Path, tree: ast.Module, source: str) -> list[Violation]:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_check(fp, tree, source)
        return []

    monkeypatch.setattr(MeaninglessVarsCheck, "check", flaky_check)


def _recompute_raises(
    _orchestrator: CheckOrchestrator, meaningless_vars: MeaninglessVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A crash in _apply_fixes' own pre-fix recompute call is distinct from
    # fix() itself raising (which is caught separately and marked [FIX
    # ERRORED], see test_apply_fixes_marks_violation_errored_when_fix_raises_unexpectedly)
    # — fix() is never even reached here. The original, already-reported
    # violation must stay exactly as it was rather than silently vanishing.
    original_check = meaningless_vars.check
    calls = {"n": 0}

    def flaky_check(_self: MeaninglessVarsCheck, fp: Path, tree: ast.Module, source: str) -> list[Violation]:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_check(fp, tree, source)
        raise ValueError("simulated recompute failure")

    monkeypatch.setattr(MeaninglessVarsCheck, "check", flaky_check)


def _fix_declines(
    _orchestrator: CheckOrchestrator, _meaningless_vars: MeaninglessVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    def decline_fix(_self: object, _filepath: Path, violations: list[Violation], *_args: object) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.DECLINED)

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", decline_fix)


def _fix_raises(
    _orchestrator: CheckOrchestrator, _meaningless_vars: MeaninglessVarsCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MeaninglessVarsCheck, "fix", raises(RuntimeError, "simulated fix failure"))


@pytest.mark.parametrize(
    "configure",
    [
        _disappear_before_refetch,
        _disappear_after_fix,
        _recompute_finds_no_fixable_violations,
        _recompute_raises,
        _fix_declines,
        _fix_raises,
    ],
    ids=[
        "file-disappears-before-refetch",
        "file-disappears-after-fix",
        "recompute-finds-no-fixable-violations",
        "recompute-raises",
        "fix-declines",
        "fix-raises",
    ],
)
def test_apply_fixes_marks_nothing_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configure: Callable[[CheckOrchestrator, MeaninglessVarsCheck, pytest.MonkeyPatch], None],
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    meaningless_vars = MeaninglessVarsCheck()
    orchestrator = CheckOrchestrator(checks=[meaningless_vars], fix_mode=True)
    configure(orchestrator, meaningless_vars, monkeypatch)

    violations = orchestrator.process_files([str(filepath)])
    v = violations[str(filepath)][0]
    assert v.fix_outcome is not FixOutcome.APPLIED


class _LineRemovingFixCheck(_AlwaysRerunProbeCheck):
    """Removes a *different* check's violation on demand, so the outcomes
    below can be exercised without depending on which shipped checks happen
    to interact today (see `docs/adr/0053-indirect-resolution-outcome.md`).
    """

    __slots__ = ("target",)

    check_id = "line-remover"
    error_code = "ZZZ005"
    cacheable = True

    def __init__(self, target: str = _FLAGGED_LINE) -> None:
        super().__init__()
        self.target = target

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> list[Violation]:
        if self.target not in source:
            return []
        return [
            Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=1,
                col=0,
                message=f"{self.target.strip()} present",
                fixable=True,
            )
        ]

    def fix(
        self, filepath: Path, _violations: list[Violation], source: str, _tree: ast.Module, encoding: str = "utf-8"
    ) -> FixResult:
        atomic_write_text(filepath, source.replace(self.target, ""), encoding, source)
        return FixResult.for_violations(_violations, FixOutcome.APPLIED)


class _LineFlaggingCheck(_AlwaysRerunProbeCheck):
    """Never resolves anything itself, so any outcome its violations end up
    with came from another check's fix.
    """

    __slots__ = ()

    check_id = "line-flagger"
    error_code = "ZZZ006"
    cacheable = True

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> list[Violation]:
        return [
            Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=number,
                col=0,
                message=f"{text} flagged",
                fixable=True,
            )
            for number, text in enumerate(source.splitlines(), start=1)
            if "flagged" in text
        ]

    def fix(
        self, _filepath: Path, _violations: list[Violation], _source: str, _tree: ast.Module, _encoding: str = "utf-8"
    ) -> FixResult:
        return FixResult.for_violations(_violations, FixOutcome.DECLINED)


class _UnfixableLineFlaggingCheck(_LineFlaggingCheck):
    """A flagger with no autofix of its own at all, like
    redundant-super-init.
    """

    __slots__ = ()

    check_id = "unfixable-line-flagger"

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        violations = super().check(filepath, tree, source)
        for violation in violations:
            violation.fixable = False
        return violations


class _PairFlaggingFixCheck(_AlwaysRerunProbeCheck):
    """Reports a fixable and a non-fixable violation for the same line, and
    deletes that line to fix the first one -- so the second goes away
    without any fix ever having been applied for it.
    """

    __slots__ = ()

    check_id = "pair-flagger"
    error_code = "ZZZ007"
    cacheable = True

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> list[Violation]:
        if _FLAGGED_LINE not in source:
            return []
        return [
            Violation(
                check_id=self.check_id,
                error_code=self.error_code,
                line=1,
                col=0,
                message=f"{half} half",
                fixable=fixable,
            )
            for half, fixable in (("fixable", True), ("unfixable", False))
        ]

    def fix(
        self, filepath: Path, _violations: list[Violation], source: str, _tree: ast.Module, encoding: str = "utf-8"
    ) -> FixResult:
        atomic_write_text(filepath, source.replace(_FLAGGED_LINE, ""), encoding, source)
        return FixResult.for_violations(_violations, FixOutcome.APPLIED)


def _run_line_probes(tmp_path: Path, checks: list[ASTCheck], source: str = "") -> dict[str, list[Violation]]:
    filepath = tmp_path / "module.py"
    filepath.write_text(source or f"{_UNRELATED_LINE}{_FLAGGED_LINE}")

    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True, cache_dir=tmp_path / "cache")
    violations = orchestrator.process_files([str(filepath)])

    return _group_by_check_id(violations[str(filepath)])


@pytest.mark.parametrize(
    "remover_runs_first",
    [True, False],
    ids=["remover-first", "flagger-first"],
)
def test_apply_fixes_marks_a_violation_another_checks_fix_removed_as_resolved_indirectly(
    tmp_path: Path, remover_runs_first: bool
) -> None:
    # ch. 1: "MUST distinguish between a diagnostic that was fixed and a
    # diagnostic that disappeared as a side effect of another fix". Whichever
    # way round the two run, the flagger's own fix() is what never resolves
    # anything, so the outcome must not depend on that ordering.
    remover = _LineRemovingFixCheck()
    flagger = _LineFlaggingCheck()
    checks: list[ASTCheck] = [remover, flagger] if remover_runs_first else [flagger, remover]

    by_check = _run_line_probes(tmp_path, checks)
    (flagged,) = by_check["line-flagger"]
    (removed,) = by_check["line-remover"]

    assert flagged.fix_outcome is FixOutcome.RESOLVED_INDIRECTLY
    assert flagged.fix_outcome is not FixOutcome.APPLIED
    assert removed.fix_outcome is FixOutcome.APPLIED
    assert removed.fix_outcome is not FixOutcome.RESOLVED_INDIRECTLY


def test_apply_fixes_leaves_a_surviving_violation_open_when_another_check_fixes_an_unrelated_line(
    tmp_path: Path,
) -> None:
    # The file changed this run, but the flagged line is still there.
    by_check = _run_line_probes(tmp_path, [_LineRemovingFixCheck(target=_UNRELATED_LINE), _LineFlaggingCheck()])
    (flagged,) = by_check["line-flagger"]

    assert flagged.fix_outcome is not FixOutcome.RESOLVED_INDIRECTLY
    assert flagged.fix_outcome is not FixOutcome.APPLIED
    assert flagged.fix_data is None
    assert by_check["line-remover"][0].fix_outcome is FixOutcome.APPLIED


def test_apply_fixes_marks_only_the_violation_another_checks_fix_actually_removed(tmp_path: Path) -> None:
    # Removing the first flagged line also shifts the second one up, so the
    # survivor is reported by a different object than the one snapshotted
    # before the fix -- it must not be swept up with the violation that
    # genuinely went away.
    by_check = _run_line_probes(
        tmp_path,
        [_LineRemovingFixCheck(), _LineFlaggingCheck()],
        source=f"{_FLAGGED_LINE}{_UNRELATED_LINE}{_SECOND_FLAGGED_LINE}",
    )
    by_message = {v.message: v for v in by_check["line-flagger"]}
    removed = by_message[f"{_FLAGGED_LINE.strip()} flagged"]
    survivor = by_message[f"{_SECOND_FLAGGED_LINE.strip()} flagged"]

    assert removed.fix_outcome is FixOutcome.RESOLVED_INDIRECTLY
    assert survivor.fix_outcome is not FixOutcome.RESOLVED_INDIRECTLY
    assert survivor.fix_data is None
    assert survivor.line == 2


def test_apply_fixes_keeps_a_failed_fix_distinguishable_from_an_indirect_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flagger's own fix() failed to write the file, and another check's
    # fix then removed the violation anyway. [FIX FAILED] reports something
    # the user may need to act on (a full disk, a read-only file), which
    # relabeling it as resolved would hide.
    def failing_fix(
        _self: object, _fp: Path, violations: list[Violation], *_args: object, **_kwargs: object
    ) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.FAILED)

    monkeypatch.setattr(_LineFlaggingCheck, "fix", failing_fix)

    (flagged,) = _run_line_probes(tmp_path, [_LineFlaggingCheck(), _LineRemovingFixCheck()])["line-flagger"]

    assert flagged.fix_outcome is FixOutcome.FAILED
    assert flagged.fix_outcome is not FixOutcome.RESOLVED_INDIRECTLY
    assert flagged.fix_outcome is not FixOutcome.APPLIED


def test_apply_fixes_marks_a_non_fixable_violation_another_checks_fix_removed_as_resolved_indirectly(
    tmp_path: Path,
) -> None:
    # ch. 1 draws no line at fixable diagnostics.
    (flagged,) = _run_line_probes(tmp_path, [_LineRemovingFixCheck(), _UnfixableLineFlaggingCheck()])[
        "unfixable-line-flagger"
    ]

    assert flagged.fix_outcome is FixOutcome.RESOLVED_INDIRECTLY


def test_apply_fixes_does_not_attribute_a_disappearance_to_a_fix_when_the_file_was_edited_externally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The remover's own write aborts because something outside this run
    # edited the file (ADR-0042), and that same edit is what removed the
    # flagger's violation -- so there is no fix of this run's own to
    # attribute it to (ADR-0053).
    def racing_fix(
        _self: object, fp: Path, _violations: object, source: str, *_args: object, **_kwargs: object
    ) -> None:
        fp.write_text(_UNRELATED_LINE)
        atomic_write_text(fp, source.replace(_FLAGGED_LINE, ""), "utf-8", source)

    monkeypatch.setattr(_LineRemovingFixCheck, "fix", racing_fix)

    by_check = _run_line_probes(tmp_path, [_LineRemovingFixCheck(), _LineFlaggingCheck()])

    assert by_check["line-remover"][0].fix_outcome is FixOutcome.ABORTED
    assert "line-flagger" not in by_check


def test_apply_fixes_marks_a_violation_its_own_checks_fix_took_with_it(tmp_path: Path) -> None:
    # No other check is even enabled here: the violation went away because
    # the same check's fix deleted the line, having only ever been asked to
    # fix the other half. Nothing was applied for it either way, which is
    # what this outcome means -- so it must not be dropped, and the report
    # must not name a different check (ADR-0053).
    by_message = {v.message: v for v in _run_line_probes(tmp_path, [_PairFlaggingFixCheck()])["pair-flagger"]}

    assert by_message["fixable half"].fix_outcome is FixOutcome.APPLIED
    assert by_message["unfixable half"].fix_outcome is FixOutcome.RESOLVED_INDIRECTLY
    assert by_message["unfixable half"].fix_outcome is not FixOutcome.APPLIED


def _run_shipped_fix_pair(tmp_path: Path, source: str) -> dict[str, list[Violation]]:
    filepath = tmp_path / "module.py"
    filepath.write_text(source)
    # ALL_CHECKS order, which is what decides who fixes first.
    checks: list[ASTCheck] = [
        MeaninglessVarsCheck(level=MeaninglessVarsLevel.PERMISSIVE),
        RedundantAssignmentCheck(level=AggressivenessLevel.PERMISSIVE),
    ]
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True, cache_dir=tmp_path / "cache")

    return _group_by_check_id(orchestrator.process_files([str(filepath)])[str(filepath)])


def test_apply_fixes_marks_a_shipped_checks_violation_another_shipped_fix_removed(tmp_path: Path) -> None:
    # Two real checks, in their real order: meaningless-vars reports `data`
    # but has no autofix for this shape, and redundant-assignment then
    # inlines the assignment away, taking the reported violation with it.
    by_check = _run_shipped_fix_pair(
        tmp_path, "import requests\n\n\ndef request():\n    data = requests.get(url)\n    return data\n"
    )
    (meaningless,) = by_check["meaningless-vars"]

    assert meaningless.fix_outcome is FixOutcome.RESOLVED_INDIRECTLY
    assert meaningless.fix_outcome is not FixOutcome.APPLIED
    assert by_check["redundant-assignment"][0].fix_outcome is FixOutcome.APPLIED


def test_apply_fixes_does_not_double_report_a_violation_whose_message_another_fix_rewrote(tmp_path: Path) -> None:
    # meaningless-vars renames `data` to `response` before
    # redundant-assignment runs, so redundant-assignment's own message names a
    # different variable before and after -- for one and the same violation,
    # which it then fixes itself. Deciding what went missing by message alone
    # would report that violation twice, [FIXED] under the new wording and
    # [RESOLVED INDIRECTLY] under the old (ADR-0053).
    by_check = _run_shipped_fix_pair(
        tmp_path, "import requests\n\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    outcomes = [v.fix_outcome for violations in by_check.values() for v in violations]
    assert len(outcomes) == 2
    assert all(outcome is FixOutcome.APPLIED for outcome in outcomes)
    assert not any(
        v.fix_outcome is FixOutcome.RESOLVED_INDIRECTLY for violations in by_check.values() for v in violations
    )


def test_report_prints_an_indirectly_resolved_violation_distinctly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Nothing is left for the user to run --fix for, but the report must
    # still account for the violation it originally reported.
    filepath = tmp_path / "module.py"
    filepath.write_text(f"{_UNRELATED_LINE}{_FLAGGED_LINE}")
    orchestrator = CheckOrchestrator(
        checks=[_LineRemovingFixCheck(), _LineFlaggingCheck()], fix_mode=True, cache_dir=tmp_path / "cache"
    )

    assert report(orchestrator, orchestrator.process_files([str(filepath)])) == 1

    err = capsys.readouterr().err
    assert "[RESOLVED INDIRECTLY]" in err
    assert "it disappeared as a side effect of another fix in this run" in err
    indirect_line = next(line for line in err.splitlines() if "[RESOLVED INDIRECTLY]" in line)
    assert "Run with --fix" not in indirect_line
    assert "please report it" not in err


def test_load_checks_explicit_check_args_none_default() -> None:
    # Passing check_args explicitly (not relying on the None default)
    # takes the same path as leaving it unset.
    checks = load_checks(select={"meaningless-vars"}, check_args={})
    assert len(checks) == 1
    assert checks[0].check_id == "meaningless-vars"


def test_load_checks_ignore_set_skips_matching_check() -> None:
    checks = load_checks(ignore={"meaningless-vars"})
    check_ids = {c.check_id for c in checks}
    assert "meaningless-vars" not in check_ids
    assert len(check_ids) == len(ALL_CHECKS) - 1


def test_load_checks_ignore_composes_with_select() -> None:
    # `select` must not make `ignore` a no-op entirely (an
    # `elif` instead of two independent checks) -- `--select`+`--ignore`
    # must be composable, the way `ruff check --select`/`--ignore` can be.
    checks = load_checks(select={"meaningless-vars", "redundant-super-init"}, ignore={"meaningless-vars"})
    assert {c.check_id for c in checks} == {"redundant-super-init"}


def test_all_checks_have_unique_check_ids_and_error_codes() -> None:
    # ALL_CHECKS is a fixed, hand-maintained list with no dynamic/plugin
    # loading (ch. 18: "MUST NOT silently resolve conflicting rule
    # identifiers in an ambiguous way") — a collision would only come from
    # a direct edit to this list, which docs/adding-a-check.md's "next
    # unused number" convention is meant to prevent. This locks that
    # invariant in so a future duplicate fails loudly here instead of
    # silently letting `--select`/`--ignore` and the cache key treat two
    # different checks as the same one.
    instances = [cls() for cls in ALL_CHECKS]
    check_ids = [c.check_id for c in instances]
    error_codes = [c.error_code for c in instances]
    assert len(check_ids) == len(set(check_ids))
    assert len(error_codes) == len(set(error_codes))


def test_load_checks_check_specific_args_are_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercises the generic re-instantiate-with-kwargs branch in
    # `load_checks` against a synthetic check with a second, unrelated
    # configurable `__init__`, rather than meaningless-vars' own.
    class ConfigurableCheck:
        check_id = "configurable"

        def __init__(self, custom: str = "default") -> None:
            self.custom = custom

    monkeypatch.setattr(_orchestrator, "ALL_CHECKS", [*ALL_CHECKS, ConfigurableCheck])

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

    monkeypatch.setattr(_orchestrator, "ALL_CHECKS", [*ALL_CHECKS, BrokenCheck])

    assert len(load_checks()) == len(ALL_CHECKS)


def test_load_checks_skips_check_when_custom_args_raise() -> None:
    checks = load_checks(
        select={"meaningless-vars"},
        check_args={"meaningless-vars": {"not_a_real_kwarg": 1}},
    )
    assert checks == []


def test_main_list_checks(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list-checks"]) == 0

    out = capsys.readouterr().out
    assert "Available checks:" in out
    assert "meaningless-vars: TR1" in out


@pytest.mark.parametrize(
    ("entrypoint", "expected_check", "unexpected_check"),
    [
        (ruff_extra_rules.main, "meaningless-vars: TR1", "redundant-type-conversion: TR6"),
        (ruff_extra_rules_ty.main, "redundant-type-conversion: TR6", "meaningless-vars: TR1"),
    ],
    ids=["default", "ty"],
)
def test_fixed_hook_entrypoints_list_only_their_own_checks(
    capsys: pytest.CaptureFixture[str],
    entrypoint: Callable[[list[str] | None], int],
    expected_check: str,
    unexpected_check: str,
) -> None:
    assert entrypoint(["--list-checks"]) == 0

    out = capsys.readouterr().out
    assert expected_check in out
    assert unexpected_check not in out


@pytest.mark.parametrize(
    ("entrypoint", "selected"),
    [
        (ruff_extra_rules.main, "redundant-type-conversion"),
        (ruff_extra_rules_ty.main, "meaningless-vars"),
    ],
    ids=["default-drops-ty-check", "ty-drops-default-check"],
)
def test_fixed_hook_entrypoint_selecting_only_a_check_it_cannot_run_exits_zero(
    tmp_path: Path, entrypoint: Callable[[list[str] | None], int], selected: str
) -> None:
    # One project-wide selection is shared by both hooks, so a check this
    # entry point can't run is dropped rather than rejected -- and dropping
    # everything exits 0, the way `ruff format` does with a lint-only
    # config. `--isolated` keeps this repo's own pyproject.toml out of it.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    assert entrypoint(["--isolated", "--select", selected, str(filepath)]) == 0


def test_fixed_hook_entrypoint_runs_a_selected_check_it_does_own(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    exit_code = ruff_extra_rules.main(
        ["--isolated", "--select", "meaningless-vars", "--meaningless-vars-level", "permissive", str(filepath)]
    )

    assert exit_code == 1


def test_main_no_filenames_returns_zero() -> None:
    assert main([]) == 0


def test_main_no_violations_returns_zero(tmp_path: Path) -> None:
    filepath = tmp_path / "clean.py"
    filepath.write_text("x = 1\n")

    assert main([str(filepath)]) == 0


def test_main_verbose_flag_does_not_change_a_clean_runs_exit_code(tmp_path: Path) -> None:
    # Exercises the --verbose branch in-process for coverage; the actual
    # observable stderr/exit-code behavior it enables is proven end-to-end
    # via a real subprocess in test_main.py (pytest's own logging-capture
    # handler makes an in-process assertion about default log output
    # unreliable -- see test_real_invocation_does_not_leak_a_traceback_onto_stderr).
    filepath = tmp_path / "clean.py"
    filepath.write_text("x = 1\n")

    assert main(["--verbose", str(filepath)]) == 0


def test_main_unparseable_file_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # An unparseable file must not be silently dropped with exit
    # code 0 — that would be indistinguishable from a clean run with
    # nothing to report. Content includes "data" so the file clears every
    # check's prefilter pattern and actually reaches parsing rather than
    # being dropped as a non-candidate beforehand.
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
    # Inside a real git repo, the prefilter's `git grep`
    # subprocess (not the Python-only fallback the other tmp_path-based
    # tests exercise, since they aren't git repos) reports a
    # permission-denied file only via a "failed to stat" line on its own
    # stderr, without changing its exit code for the files it *could*
    # read. main() must inspect that stderr — otherwise the file would be
    # silently dropped before ever reaching _check_file, and the whole run
    # would report exit code 0, as if the file never existed.
    # MeaninglessVarsCheck has a prefilter pattern ("data" is one of its
    # meaningless names), so this only exercises the check that actually
    # calls into git grep.
    git = shutil.which("git")
    assert git is not None

    with contextlib.chdir(tmp_path):
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603

        filepath = tmp_path / "module.py"
        filepath.write_text("data = 1\n")
        subprocess.run([git, "add", "module.py"], check=True, cwd=tmp_path)  # noqa: S603

        with restricted_permissions(filepath, 0o000, restore=0o644), caplog.at_level("DEBUG"):
            exit_code = main(["module.py", "--select", "meaningless-vars"])

        assert exit_code == 1
        assert "module.py: error: could not be read or parsed; file skipped" in capsys.readouterr().err
        # The clean diagnostic above already reports this; a raw traceback
        # alongside it on stderr would just be redundant noise (ch. 7: "MUST
        # NOT emit uncontrolled human-oriented text into a machine-readable
        # output stream").
        assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_check_crash_returns_one_and_reports_check_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A check that crashes on every file it sees must not make the whole
    # run look clean (exit code 0, nothing printed) just because no other
    # check reported a violation for the same files.
    monkeypatch.setattr(MeaninglessVarsCheck, "check", raises(ValueError, "simulated check failure"))

    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    exit_code = main([str(filepath), "--select", "meaningless-vars"])
    assert exit_code == 1

    assert f"{filepath}: error: check 'meaningless-vars' raised an unexpected exception" in capsys.readouterr().err


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
    assert "TR3" in err
    assert "[FIXABLE]" not in err
    assert "[FIXED]" not in err


def test_main_reports_column_alongside_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Violation.col is computed accurately (meaningless-vars reports the assigned
    # name's own ast.col_offset, converted to a character offset), and
    # main()'s printed line must include it, not just the line -- ch. 7:
    # "MUST report line and column information accurately when available".
    # "data" starts at the 0-indexed character offset 4; main() reports
    # the conventional 1-based column, 5.
    filepath = tmp_path / "module.py"
    filepath.write_text("def process():\n    data = requests.get(url)\n    return data\n")

    exit_code = main([str(filepath), "--select", "meaningless-vars", "--meaningless-vars-level", "permissive"])
    assert exit_code == 1

    assert f"{filepath}:2:5: TR1:" in capsys.readouterr().err


def test_main_reports_fixable_violation_without_fix_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    exit_code = main([str(filepath), "--select", "meaningless-vars"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIXABLE]" in err
    assert "Run with --fix to inline automatically." in err


def test_main_fix_flag_marks_violation_fixed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    exit_code = main([str(filepath), "--select", "meaningless-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIXED]" in err
    fixed_line = next(line for line in err.splitlines() if "[FIXED]" in line)
    assert "Run with --fix" not in fixed_line
    # ch. 32: "MUST verify that the reported result matches the actual final
    # file state" -- the rejected/errored/failed variants of this test
    # already assert the file stays untouched; only the success path was
    # never checked against the real on-disk content, just the printed
    # [FIXED] line.
    assert filepath.read_text() == (
        "import requests\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n"
    )


def test_main_fix_flag_reports_rejected_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A fix rejected for invalid syntax must be reported distinctly from
    # both [FIXED] and the ordinary [FIXABLE]/"Run with --fix" hint, since
    # re-running --fix would just fail identically again.
    def broken_fix(_self: object, fp: Path, *_args: object, **_kwargs: object) -> None:
        atomic_write_text(fp, "def broken(:\n", "utf-8", fp.read_text())

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", broken_fix)

    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "meaningless-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIX REJECTED]" in err
    assert "please report it" in err
    assert "https://github.com/alessio-locatelli/ruff-extra-rules/issues" in err
    assert "[FIXED]" not in err
    assert "Run with --fix" not in err
    assert "data = requests.get(url)" in filepath.read_text()
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

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", broken_fix)

    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "meaningless-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIX ERRORED]" in err
    assert "please report it" in err
    assert "https://github.com/alessio-locatelli/ruff-extra-rules/issues" in err
    assert "[FIXED]" not in err
    assert "[FIX REJECTED]" not in err
    assert "Run with --fix" not in err
    assert "data = requests.get(url)" in filepath.read_text()
    # [FIX ERRORED] above already reports this; a raw traceback alongside it
    # on stderr would just be redundant noise (ch. 7: "MUST NOT emit
    # uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_fix_flag_reports_failed_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # A fix() that catches its own OSError (disk full, permission denied)
    # and returns a failure outcome must
    # be reported distinctly from [FIXED] and the ordinary [FIXABLE]/"Run
    # with --fix" hint -- re-running --fix would just fail identically
    # again, and unlike [FIX ERRORED] this isn't a bug in the check's own
    # logic, so the hint must not claim it is.
    subdir = tmp_path / "readonly"
    subdir.mkdir()
    filepath = subdir / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )
    with restricted_permissions(subdir, 0o555, restore=0o755), caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "meaningless-vars", "--fix"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "[FIX FAILED]" in err
    assert "bug" not in err
    assert "[FIXED]" not in err
    assert "[FIX ERRORED]" not in err
    assert "[FIX REJECTED]" not in err
    assert "Run with --fix" not in err
    assert "could not write the file" in err
    assert "data = requests.get(url)" in filepath.read_text()
    # [FIX FAILED] above already reports this; a raw traceback alongside it
    # on stderr would just be redundant noise (ch. 7: "MUST NOT emit
    # uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_fix_flag_reports_declined_fix_without_operational_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def declined_fix(
        _self: object, _fp: Path, violations: list[Violation], *_args: object, **_kwargs: object
    ) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.DECLINED)

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", declined_fix)
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    assert main([str(filepath), "--select", "meaningless-vars", "--fix"]) == 1

    err = capsys.readouterr().err
    assert "[FIX DECLINED]" in err
    assert "not safe to apply automatically" in err
    assert "could not write the file" not in err
    assert "file permissions" not in err


def test_main_fix_flag_reports_aborted_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A file changed on disk after _apply_fixes read it but before this
    # check's own write landed -- an editor, or a process outside this run's
    # own per-file fix lock. Must be reported distinctly from
    # [FIXED]/[FIX REJECTED]/[FIX ERRORED]/[FIX FAILED]: re-running --fix is
    # the right recovery here, not a bug report or an environmental failure.
    def racing_fix(
        _self: object, fp: Path, _violations: object, source: str, *_args: object, **_kwargs: object
    ) -> None:
        fp.write_text(f"{source}# edited elsewhere\n")
        atomic_write_text(
            fp,
            "import requests\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n",
            "utf-8",
            source,
        )

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", racing_fix)

    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "meaningless-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIX ABORTED]" in err
    assert "run with --fix again" in err
    assert "[FIXED]" not in err
    assert "[FIX ERRORED]" not in err
    assert "[FIX REJECTED]" not in err
    assert "[FIX FAILED]" not in err
    assert "please report it" not in err
    # ConcurrentModificationError has its own except clause in _apply_fixes,
    # distinct from the generic except Exception branch that records a rule
    # failure -- an abort is an expected, cleanly-reported outcome, not an
    # internal bug, so it must never also surface as one.
    assert f"{filepath}: error: check 'meaningless-vars' raised an unexpected exception" not in err
    # The externally written content survives untouched -- this check's own
    # attempted fix must never land on top of it.
    assert filepath.read_text() == (
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
        "# edited elsewhere\n"
    )
    # [FIX ABORTED] above already reports this; a raw traceback alongside it
    # on stderr would just be redundant noise (ch. 7: "MUST NOT emit
    # uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_main_reports_rule_failure_when_reread_fails_mid_fix_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _apply_fixes() re-reads the file before recomputing each
    # check's fresh violations (a previous check's fix in the same loop may
    # have already modified it). If that re-read itself fails, the loop
    # must not just `continue` with zero signal anywhere -- otherwise the
    # stale violation would be left unmarked and reported as an ordinary
    # [FIXABLE], as if --fix had never even been attempted for it.
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
        "import requests\n\n"
        "def request():\n"
        "    data = requests.get(url)\n"
        "    return data.status_code\n\n\n"
        "class Base:\n    def __init__(self):\n        pass\n\n\n"
        "class Child(Base):\n    def __init__(self, **kwargs):\n        super().__init__(**kwargs)\n"
    )

    # A second, never-fixable check alongside meaningless-vars: its own
    # violation must be left alone by the marking loop below (only
    # meaningless-vars' violation matches check.check_id), exercising that the
    # loop's `if` condition can also be False for a violation from an
    # unrelated check_id, not just True for a matching, fixable one.
    checks = load_checks(select={"meaningless-vars", "redundant-super-init"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert (str(filepath), "meaningless-vars") in orchestrator.rule_failures  # pytriage: TR6
    meaningless_vars_violation = next(v for v in violations[str(filepath)] if v.check_id == "meaningless-vars")
    super_init_violation = next(v for v in violations[str(filepath)] if v.check_id == "redundant-super-init")
    assert meaningless_vars_violation.fix_outcome is FixOutcome.ERRORED
    assert super_init_violation.fix_outcome is not FixOutcome.ERRORED
    # The re-read failure means nothing was ever written back.
    assert "data = requests.get(url)" in filepath.read_text()


def test_main_reports_rule_failure_when_recompute_raises_mid_fix_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # _apply_fixes() recomputes fresh violations via check.check()
    # against the current file state before calling check.fix(). If that
    # recompute itself raises, it must not be left to only the broad outer
    # `except Exception` to catch -- that would log the exception but
    # never record a rule_failure or mark any violation, so the run could
    # look completely clean depending on what else was reported for the
    # file.
    original_check = MeaninglessVarsCheck.check
    calls = 0

    def check_raises_on_second_call(
        self: MeaninglessVarsCheck, filepath: Path, tree: ast.Module, source: str
    ) -> list[Violation]:
        nonlocal calls
        calls += 1
        if calls == 2:
            msg = "simulated recompute failure"
            raise RuntimeError(msg)
        return original_check(self, filepath, tree, source)

    monkeypatch.setattr(MeaninglessVarsCheck, "check", check_raises_on_second_call)

    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\n"
        "def request():\n"
        "    data = requests.get(url)\n"
        "    return data.status_code\n\n\n"
        "class Base:\n    def __init__(self):\n        pass\n\n\n"
        "class Child(Base):\n    def __init__(self, **kwargs):\n        super().__init__(**kwargs)\n"
    )

    # A second, never-fixable check alongside meaningless-vars: its own
    # violation must be left alone by the marking loop below (only
    # meaningless-vars' violation matches check.check_id), exercising that the
    # loop's `if` condition can also be False for a violation from an
    # unrelated check_id, not just True for a matching, fixable one.
    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "meaningless-vars,redundant-super-init", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert f"{filepath}: error: check 'meaningless-vars' raised an unexpected exception" in err
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
    # fix() can commit every requested edit and then raise
    # afterwards (e.g. during unrelated cleanup) — every violation ends up
    # correctly reported [FIXED], but the exception itself must still
    # surface in the run's own output, or a genuine internal failure would
    # be completely invisible behind what reads as full success.
    def fix_then_raise(_self: object, filepath: Path, *_args: object, **_kwargs: object) -> None:
        atomic_write_text(
            filepath,
            "import requests\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n",
            "utf-8",
            filepath.read_text(),
        )
        raise RuntimeError("simulated cleanup bug after a successful fix")

    monkeypatch.setattr(MeaninglessVarsCheck, "fix", fix_then_raise)

    filepath = tmp_path / "module.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n"
    )

    with caplog.at_level("DEBUG"):
        exit_code = main([str(filepath), "--select", "meaningless-vars", "--fix"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "[FIXED]" in err
    assert "[FIX ERRORED]" not in err
    assert f"{filepath}: error: check 'meaningless-vars' raised an unexpected exception" in err
    assert filepath.read_text() == (
        "import requests\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n"
    )
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
    # redundant-assignment's own --redundant-assignment-level flag (see
    # test_main_level_flag_switches_between_conservative_and_permissive_reporting
    # below) already exercises this wiring against a shipped check; this
    # synthetic one keeps that coverage independent of TR5's own reporting
    # rules.
    # Exercises main()'s OPTIONS -> parse_args -> resolved-config ->
    # check_args wiring end-to-end against a synthetic check.
    class _Marker(Enum):
        DEFAULT = auto()
        CUSTOM = auto()

    class ConfigurableCheck:
        check_id = "configurable"
        error_code = "CFG001"
        cacheable = True
        tracks_direct_inputs = False
        OPTIONS: ClassVar[tuple[CheckOption, ...]] = (
            EnumOption(name="marker", values=_Marker, default=_Marker.DEFAULT, help="synthetic"),
        )

        def __init__(self, marker: _Marker = _Marker.DEFAULT) -> None:
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
                    message=self.marker.name,
                    fixable=False,
                )
            ]

    # main() and load_checks() each hold their own `ALL_CHECKS` name binding
    # (imported via `from . import ALL_CHECKS` into `_cli.py` and
    # `_orchestrator.py` respectively) -- both need patching for the
    # synthetic check to reach both main()'s own CLI-arg wiring and
    # load_checks()'s instantiation.
    monkeypatch.setattr(_cli, "ALL_CHECKS", [*ALL_CHECKS, ConfigurableCheck])
    monkeypatch.setattr(_orchestrator, "ALL_CHECKS", [*ALL_CHECKS, ConfigurableCheck])

    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    exit_code = main(
        [
            str(filepath),
            "--isolated",
            "--select",
            "configurable",
            "--configurable-marker",
            "custom",
        ]
    )
    assert exit_code == 1

    assert "CUSTOM" in capsys.readouterr().err


class _LevelFlagCase(NamedTuple):
    check_id: str
    level_flag: str
    source: str
    permissive_needle: str


@pytest.mark.parametrize(
    "case",
    [
        _LevelFlagCase(
            "redundant-assignment",
            "--redundant-assignment-level",
            # A type-annotated single-use assignment scores just above the
            # conservative report ceiling but under the permissive one (see
            # tests/redundant_assignment/test_check.py::test_annotated_assignment_tracked).
            'def example():\n    x: str = "foo"\n    func(x)\n',
            "'x'",
        ),
        _LevelFlagCase(
            "meaningless-vars",
            "--meaningless-vars-level",
            # "result = 42" has no suggested rename.
            "def other():\n    result = 42\n    return result\n",
            "'result'",
        ),
    ],
    ids=["redundant-assignment", "meaningless-vars"],
)
def test_main_level_flag_switches_between_conservative_and_permissive_reporting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: _LevelFlagCase
) -> None:
    # Issue #76 (redundant-assignment) / docs/adr/0031 (meaningless-vars):
    # both checks share one conservative/permissive vocabulary for "how
    # eagerly does this check speak up" -- the default must agree with an
    # explicit "conservative", and only "permissive" surfaces this
    # borderline violation.
    filepath = tmp_path / "module.py"
    filepath.write_text(case.source)

    default_exit_code = main([str(filepath), "--select", case.check_id])
    assert default_exit_code == 0

    conservative_exit_code = main([str(filepath), "--select", case.check_id, case.level_flag, "conservative"])
    assert conservative_exit_code == 0

    permissive_exit_code = main([str(filepath), "--select", case.check_id, case.level_flag, "permissive"])
    assert permissive_exit_code == 1
    assert case.permissive_needle in capsys.readouterr().err


@pytest.mark.parametrize(
    "level_flag",
    ["--redundant-assignment-level", "--meaningless-vars-level"],
    ids=["redundant-assignment", "meaningless-vars"],
)
def test_main_level_flag_rejects_unknown_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], level_flag: str
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    with pytest.raises(SystemExit) as exc_info:
        main([str(filepath), level_flag, "bogus"])

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--select", "--ignore"], ids=["select", "ignore"])
def test_main_unknown_check_name_returns_two(tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    exit_code = main([str(filepath), flag, "not-a-real-check"])
    assert exit_code == 2

    # Both the offending flag and where the value came from are named —
    # otherwise --select and --ignore errors are indistinguishable, and a
    # config-file typo reads as if it came from the command line.
    err = capsys.readouterr().err
    assert f"Unknown check `not-a-real-check` in `{flag}` from the CLI" in err


def test_main_ignoring_all_checks_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Enabling no checks is a legitimate configuration, not an error:
    # `ruff check` with `select = []` likewise reports nothing and exits 0.
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    all_ids = ",".join(sorted(cls().check_id for cls in ALL_CHECKS))
    exit_code = main([str(filepath), "--isolated", "--ignore", all_ids])
    assert exit_code == 0

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("flag", "meaningless_vars_runs"),
    [("--select", True), ("--ignore", False)],
    ids=["select", "ignore"],
)
def test_main_trailing_comma_does_not_report_blank_unknown_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, meaningless_vars_runs: bool
) -> None:
    # A trailing comma must not leave an empty string in the parsed set,
    # reported as a confusing blank "Unknown checks: " instead of being
    # tolerated like --exclude's own comma list already is. Asserting on
    # the TR1 violation itself (not just the exit code) is what actually
    # proves meaningless-vars was recognized: a bogus "Unknown checks"
    # error would also exit 1 for --select (masking a real violation), so
    # the exit code alone can't tell the two apart.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    exit_code = main([str(filepath), flag, "meaningless-vars,", "--meaningless-vars-level", "permissive"])
    err = capsys.readouterr().err

    assert "Unknown checks" not in err
    assert ("TR1" in err) is meaningless_vars_runs
    assert exit_code == (1 if meaningless_vars_runs else 0)


def test_main_select_only_commas_is_a_configuration_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A --select naming no check at all (once blanks are filtered) is
    # malformed input, not a request to run nothing — `ruff` likewise
    # rejects `--select=` as an unknown rule selector rather than treating
    # it like `select = []`. The blank value must not be echoed back as a
    # nameless "Unknown check ``".
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    exit_code = main([str(filepath), "--select", ",,"])
    assert exit_code == 2

    err = capsys.readouterr().err
    assert "Unknown check ``" not in err
    assert "expected at least one check name" in err


def test_main_select_and_ignore_compose(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # `--select`+`--ignore` together must not behave like
    # `--select` alone, silently dropping `--ignore` (see
    # `test_load_checks_ignore_composes_with_select`).
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")

    exit_code = main(
        [
            str(filepath),
            "--select",
            "meaningless-vars,redundant-super-init",
            "--ignore",
            "meaningless-vars",
        ]
    )
    assert exit_code == 0

    assert "TR1" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "expected_codes"),
    [
        ("--select", {"TR1", "TR3"}),
        ("--ignore", set()),
    ],
    ids=["select", "ignore"],
)
def test_main_accumulates_repeated_check_selection_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, expected_codes: set[str]
) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "data = 1\n\n"
        "class Base:\n"
        "    def __init__(self):\n"
        "        pass\n\n"
        "class Child(Base):\n"
        "    def __init__(self, **kwargs):\n"
        "        super().__init__(**kwargs)\n"
    )

    exit_code = main(
        [
            str(filepath),
            flag,
            "meaningless-vars",
            flag,
            "redundant-super-init",
            "--meaningless-vars-level=permissive",
        ]
    )

    err = capsys.readouterr().err
    assert {code for code in ("TR1", "TR3") if code in err} == expected_codes
    assert exit_code == (1 if expected_codes else 0)


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


_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_BAD_FIXTURE_PATHS = sorted(_FIXTURES_DIR.glob("**/bad/*.py"))


@pytest.fixture
def isolated_tri006_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    original_session = tri006_session_module._session
    original_probe_failed = tri006_session_module._daemon_probe_failed
    original_probe_next_retry_at = tri006_session_module._daemon_probe_next_retry_at
    tri006_session_module._session = None
    tri006_session_module._daemon_probe_failed = False
    tri006_session_module._daemon_probe_next_retry_at = 0.0
    try:
        yield tmp_path
    finally:
        leftover_session = tri006_session_module.peek_session()
        if leftover_session is not None:
            leftover_session.close()
        for _ in range(3):
            tri006_daemon.shutdown_if_running(tmp_path)
            time.sleep(0.2)
        tri006_session_module._session = original_session
        tri006_session_module._daemon_probe_failed = original_probe_failed
        tri006_session_module._daemon_probe_next_retry_at = original_probe_next_retry_at


@pytest.mark.parametrize(
    "fixture_path",
    _BAD_FIXTURE_PATHS,
    ids=[str(p.relative_to(_FIXTURES_DIR)) for p in _BAD_FIXTURE_PATHS],
)
def test_fix_converges_after_one_pass_across_all_checks(fixture_path: Path, isolated_tri006_daemon: Path) -> None:
    # ch. 10: "MUST ensure that applying the same fix repeatedly converges
    # to a stable result"; "MUST test fix idempotence explicitly". Runs
    # --fix (every check together, matching real pre-commit/prek usage)
    # twice over every existing bad/*.py fixture and asserts the second
    # pass leaves the file exactly as the first pass did.
    target = isolated_tri006_daemon / fixture_path.name
    target.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")

    CheckOrchestrator(checks=load_checks(), fix_mode=True).process_files([str(target)])
    first_pass = target.read_text(encoding="utf-8")

    CheckOrchestrator(checks=load_checks(), fix_mode=True).process_files([str(target)])
    second_pass = target.read_text(encoding="utf-8")

    assert second_pass == first_pass


def test_main_handles_path_containing_spaces_and_unicode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ch. 31: "MUST test paths containing spaces and Unicode." meaningless-vars
    # declares a prefilter pattern, so a normal run first passes this path to
    # git_grep_filter()'s real `git grep` subprocess call as a plain argument,
    # never through a shell (0006/0015's file-discovery audit) -- but only
    # inside a real git repo; a bare tmp_path (outside any repo, as every
    # other test in this file uses it) makes `git grep` error out and
    # silently fall back to the Python-only path instead, which has no
    # subprocess argument-passing to prove safe in the first place, and would
    # still produce the same passing result -- silently masking exactly
    # the bug this test exists to catch. Spies on the real subprocess.run
    # (still executed for real, never mocked out) to confirm git grep
    # itself actually received and matched this path rather than silently
    # falling back.
    git = shutil.which("git")
    assert git is not None

    directory = tmp_path / "my proj café 日本語"
    directory.mkdir()
    filepath = directory / "module café.py"
    filepath.write_text(
        "import requests\n\ndef request():\n    data = requests.get(url)\n    return data.status_code\n",
        encoding="utf-8",
    )

    real_run = subprocess.run
    # Every subprocess.run call made once the spy is installed below is a
    # git-grep call: meaningless-vars' own two prefilter patterns ("data",
    # "result") are the only thing that invokes git_grep_filter() for this
    # single-file, single-check run (a lone file argument never triggers
    # expand_directories()'s own git ls-files call).
    grep_commands: list[list[str]] = []
    grep_results: list[subprocess.CompletedProcess[str]] = []

    def _spy_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        completed_process = real_run(*args, **kwargs)  # type: ignore[call-overload]
        grep_commands.append(args[0])  # type: ignore[arg-type]
        grep_results.append(completed_process)
        return completed_process

    with contextlib.chdir(tmp_path):
        # Args are hardcoded test setup, not untrusted input.
        subprocess.run([git, "init", "-q"], check=True)  # noqa: S603
        subprocess.run([git, "add", filepath], check=True)  # noqa: S603

        monkeypatch.setattr(subprocess, "run", _spy_run)
        exit_code = main([str(filepath), "--select", "meaningless-vars", "--fix"])

    assert grep_results, "git grep was never invoked -- fell back to the Python-only path"
    assert all(result.stderr == "" for result in grep_results)
    assert any(result.returncode == 0 for result in grep_results)
    # Prove the path itself reached the command as a real, intact argument --
    # a command that dropped the pathspec (searching the whole repo instead
    # of this one file) would still find the sole match and pass every
    # assertion above, silently losing path scoping without this check.
    assert all(str(filepath) in command for command in grep_commands)  # pytriage: TR6

    assert exit_code == 1
    assert "[FIXED]" in capsys.readouterr().err
    assert filepath.read_text(encoding="utf-8") == (
        "import requests\n\ndef request():\n    response = requests.get(url)\n    return response.status_code\n"
    )


def test_process_files_handles_a_large_file(tmp_path: Path) -> None:
    # ch. 31: "MUST test large files." This is a correctness check, not a
    # performance benchmark (ch. 24/30's own concern) -- it only proves
    # every violation in a large file is still found and fixed correctly,
    # with nothing dropped, corrupted, or left unprocessed.
    function_count = 300
    source = "import requests\n\n" + "\n\n".join(
        f"def func_{i}():\n    data = requests.get(url)\n    return data.status_code" for i in range(function_count)
    )
    filepath = tmp_path / "large_module.py"
    filepath.write_text(source + "\n", encoding="utf-8")

    orchestrator = CheckOrchestrator(checks=load_checks(select={"meaningless-vars"}), fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert orchestrator.unprocessable_files == []
    assert orchestrator.rule_failures == []
    fixed = [v for v in violations[str(filepath)] if v.fix_outcome is FixOutcome.APPLIED]
    assert len(fixed) == function_count
    assert "data = requests.get" not in filepath.read_text(encoding="utf-8")


def _ignoring(check_id: str, pattern: str, anchor: Path) -> PerFileIgnoreList:
    return PerFileIgnoreList(
        (PerFileIgnore(pattern=pattern, anchor=anchor, negated=False, check_ids=frozenset({check_id})),)
    )


def test_per_file_ignores_runs_a_check_on_the_files_it_does_not_match(tmp_path: Path) -> None:
    ignored = tmp_path / "skip_me.py"
    ignored.write_text("x = 1\n")
    checked = tmp_path / "check_me.py"
    checked.write_text("y = 2\n")
    probe = _AlwaysRerunProbeCheck()

    orchestrator = CheckOrchestrator(checks=[probe], per_file_ignores=_ignoring(probe.check_id, "skip_me.py", tmp_path))
    violations = orchestrator.process_files([str(ignored), str(checked)])

    assert violations.keys() == {str(checked)}
    assert probe.call_count == 1


def test_a_per_file_ignored_check_is_still_handed_the_files_content(tmp_path: Path) -> None:
    # Suppressing a report in one file must not withhold that file from the
    # cross-file analysis another file's report depends on; see ADR-0049.
    ignored = tmp_path / "dependency.py"
    ignored.write_text("x = 1\n")
    checked = tmp_path / "importer.py"
    checked.write_text("y = 2\n")
    probe = _CrossFileProbeCheck()

    orchestrator = CheckOrchestrator(
        checks=[probe], per_file_ignores=_ignoring(probe.check_id, "dependency.py", tmp_path)
    )
    violations = orchestrator.process_files([str(ignored), str(checked)])

    assert violations.keys() == {str(checked)}
    assert probe.direct_inputs == [ignored.resolve(), checked.resolve()]


def test_an_unreadable_file_is_reported_even_when_every_check_is_ignored(tmp_path: Path) -> None:
    ignored = tmp_path / "dependency.py"
    ignored.write_text("x = 1\n")
    probe = _CrossFileProbeCheck()

    orchestrator = CheckOrchestrator(
        checks=[probe], per_file_ignores=_ignoring(probe.check_id, "dependency.py", tmp_path)
    )
    with restricted_permissions(ignored, 0o000, restore=0o644):
        violations = orchestrator.process_files([str(ignored)])

    assert violations == {}
    assert orchestrator.unprocessable_files == [str(ignored)]


def test_an_unparseable_ignored_file_never_reaches_a_cross_file_session(tmp_path: Path) -> None:
    ignored = tmp_path / "dependency.py"
    ignored.write_text("def broken(\n")
    probe = _CrossFileProbeCheck()

    orchestrator = CheckOrchestrator(
        checks=[probe], per_file_ignores=_ignoring(probe.check_id, "dependency.py", tmp_path)
    )
    violations = orchestrator.process_files([str(ignored)])

    assert violations == {}
    assert orchestrator.unprocessable_files == [str(ignored)]
    assert probe.direct_inputs == []


def test_a_check_without_the_direct_input_lifecycle_never_sees_an_ignored_file(tmp_path: Path) -> None:
    ignored = tmp_path / "skip_me.py"
    ignored.write_text("x = 1\n")
    probe = _DrainingProbeCheck()

    orchestrator = CheckOrchestrator(checks=[probe], per_file_ignores=_ignoring(probe.check_id, "skip_me.py", tmp_path))
    orchestrator.process_files([str(ignored)])

    assert probe.direct_inputs == []


def test_a_cross_file_candidate_is_skipped_where_its_check_is_ignored(tmp_path: Path) -> None:
    main_file = tmp_path / "main.py"
    main_file.write_text("x = 1\n")
    extra_file = tmp_path / "extra.py"
    extra_file.write_text("y = 2\n")
    probe = _DrainingProbeCheck(extra_files=[extra_file])

    orchestrator = CheckOrchestrator(checks=[probe], per_file_ignores=_ignoring(probe.check_id, "extra.py", tmp_path))
    violations = orchestrator.process_files([str(main_file)])

    assert violations.keys() == {str(main_file)}


def test_an_ignored_check_leaves_the_remaining_ones_their_own_cache_identity(tmp_path: Path) -> None:
    # The cached result of a run that skipped a check must never be served to
    # a run that did not, which is what deciding hook_name per file buys;
    # see ADR-0049.
    filepath = tmp_path / "module.py"
    filepath.write_text("data = 1\n")
    cache_dir = tmp_path / ".cache"

    def orchestrate(per_file_ignores: PerFileIgnoreList) -> dict[str, list[Violation]]:
        return CheckOrchestrator(
            checks=[_MarkerFixableCheck(check_id="probe-a"), MeaninglessVarsCheck(MeaninglessVarsLevel.PERMISSIVE)],
            cache_dir=cache_dir,
            per_file_ignores=per_file_ignores,
        ).process_files([str(filepath)])

    while_ignored = orchestrate(_ignoring("meaningless-vars", "module.py", tmp_path))
    assert {v.check_id for v in while_ignored[str(filepath)]} == {"probe-a"}

    assert {v.check_id for v in orchestrate(PerFileIgnoreList())[str(filepath)]} == {"probe-a", "meaningless-vars"}


def test_an_ignored_file_is_not_fed_to_a_check_already_found_unavailable(tmp_path: Path) -> None:
    # A missing prerequisite does not get better on the next file, so the
    # check is not worth feeding again this run either.
    checked = tmp_path / "importer.py"
    checked.write_text("x = 1\n")
    ignored = tmp_path / "dependency.py"
    ignored.write_text("y = 2\n")
    probe = _UnavailableCrossFileProbeCheck()

    orchestrator = CheckOrchestrator(
        checks=[probe], per_file_ignores=_ignoring(probe.check_id, "dependency.py", tmp_path)
    )
    orchestrator.process_files([str(checked), str(ignored)])

    assert orchestrator.unavailable_checks == [(probe.check_id, "simulated: prerequisite missing")]
    assert probe.direct_inputs == []


def test_a_prefilter_skips_the_files_its_own_check_is_ignored_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scanning for a check that cannot run is the expensive half of deciding
    # something already decided; see ADR-0049.
    ignored = tmp_path / "skip_me.py"
    ignored.write_text("x = 1\n")
    checked = tmp_path / "check_me.py"
    checked.write_text("y = 2\n")
    ignored_path, checked_path = str(ignored), str(checked)
    plain = _PrefilteredProbeCheck()
    cross_file = _PrefilteredCrossFileProbeCheck()

    scanned: dict[str, list[str]] = {}

    def spy(filepaths: list[str], patterns: list[str]) -> list[str]:
        scanned[patterns[0]] = sorted(filepaths)
        return filepaths

    monkeypatch.setattr(_orchestrator, "batch_filter_files", spy)

    per_file_ignores = PerFileIgnoreList(
        (
            PerFileIgnore(
                pattern="skip_me.py",
                anchor=tmp_path,
                negated=False,
                check_ids=frozenset({plain.check_id, cross_file.check_id}),
            ),
        )
    )
    CheckOrchestrator(checks=[plain, cross_file], per_file_ignores=per_file_ignores).process_files(
        [ignored_path, checked_path]
    )

    assert scanned["probe-marker"] == [checked_path]
    # A check that has to be handed the file regardless is still asked.
    assert scanned["cross-file-marker"] == sorted([checked_path, ignored_path])


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [("../{anchor}/vendor/**", []), ("{absolute}/vendor/**", []), ("../elsewhere/**", ["vendor/v.py"])],
    ids=["leads-back-in", "absolute-pattern", "leads-out-and-stays-out"],
)
def test_an_exclude_pattern_is_resolved_against_its_anchor(tmp_path: Path, pattern: str, expected: list[str]) -> None:
    anchor = tmp_path / "project"
    spelled = pattern.format(anchor=anchor.name, absolute=anchor)
    absolute = [str(anchor / "vendor" / "v.py")]

    filtered = filter_excluded_files(absolute, [ExcludePattern(spelled, anchor)])

    assert filtered == [str(anchor / name) for name in expected]


def _cross_file_probe_orchestrator(tmp_path: Path, probe: _CrossFileProbeCheck) -> CheckOrchestrator:
    """A cacheable check that sees every file, plus a cross-file check the
    named file switches off -- so a second run hits the cache with nothing
    left to re-run.
    """
    return CheckOrchestrator(
        checks=[_MarkerFixableCheck(check_id="probe-a"), probe],
        cache_dir=tmp_path / ".cache",
        per_file_ignores=_ignoring(probe.check_id, "module.py", tmp_path),
    )


def test_a_clean_cache_hit_still_hands_an_ignored_file_to_a_cross_file_check(tmp_path: Path) -> None:
    # Nothing else opens the file on this path, so the direct-input lifecycle
    # has to; see ADR-0049.
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    _cross_file_probe_orchestrator(tmp_path, _CrossFileProbeCheck()).process_files([str(filepath)])
    probe = _CrossFileProbeCheck()
    orchestrator = _cross_file_probe_orchestrator(tmp_path, probe)

    assert {v.check_id for v in orchestrator.process_files([str(filepath)])[str(filepath)]} == {"probe-a"}
    assert probe.direct_inputs == [filepath.resolve()]


def test_an_unreadable_file_is_reported_even_on_a_clean_cache_hit(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text("x = 1\n")

    _cross_file_probe_orchestrator(tmp_path, _CrossFileProbeCheck()).process_files([str(filepath)])
    probe = _CrossFileProbeCheck()
    orchestrator = _cross_file_probe_orchestrator(tmp_path, probe)

    with restricted_permissions(filepath, 0o000, restore=0o644):
        violations = orchestrator.process_files([str(filepath)])

    assert violations == {}
    assert orchestrator.unprocessable_files == [str(filepath)]
    assert probe.direct_inputs == []


def test_an_entry_for_a_check_this_run_cannot_run_changes_nothing(tmp_path: Path) -> None:
    # One table serves both published hooks (ADR-0045), so an entry naming
    # only the other one's check must not make this run read a file its own
    # prefilter already dropped.
    filepath = tmp_path / "module.py"
    filepath.write_text("def broken(\n")
    orchestrator = CheckOrchestrator(
        checks=[_PrefilteredProbeCheck()],
        per_file_ignores=_ignoring("some-other-hooks-check", "module.py", tmp_path),
    )

    assert orchestrator.process_files([str(filepath)]) == {}
    assert orchestrator.unprocessable_files == []
