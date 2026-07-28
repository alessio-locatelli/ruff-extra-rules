"""Integration layer for TR6: runs the real `ty` binary (pinned as a dev
dependency, see pyproject.toml), sharing one warm `ty server` session
across this whole module -- mirroring the check's own production design
(one session per hook invocation, not one per file/query).

The bulk of this check's own detection logic is unit-tested against
recorded/fake session responses in `tests/redundant_type_conversion/`,
for speed and determinism; this file exists specifically so a real `ty`
version bump is automatically checked for staleness against those
recordings on every CI run, per issue #108's own testing decisions.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import pre_commit_hooks.ast_checks.redundant_type_conversion as tri006_module
from pre_commit_hooks.ast_checks.redundant_type_conversion import RedundantTypeConversionCheck
from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import ConfidenceLevel
from pre_commit_hooks.ast_checks.redundant_type_conversion.session import TySession
from pre_commit_hooks.ast_checks.redundant_type_conversion.session import get_session as real_get_session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pre_commit_hooks.ast_checks._base import Violation

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "redundant_type_conversion"
BAD_ROOT = FIXTURES_ROOT / "bad"


@pytest.fixture(scope="module")
def ty_session() -> Iterator[TySession]:
    session = TySession(root=FIXTURES_ROOT)
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _use_shared_session(monkeypatch: pytest.MonkeyPatch, ty_session: TySession) -> None:
    monkeypatch.setattr(tri006_module, "get_session", lambda: ty_session)


def _check(filepath: Path, *, level: ConfidenceLevel = ConfidenceLevel.CONSERVATIVE) -> list[Violation]:
    source = filepath.read_text()
    return RedundantTypeConversionCheck(level=level).check(filepath, ast.parse(source), source)


def test_same_scope_scalar_conversion_is_flagged() -> None:
    violations = _check(BAD_ROOT / "same_scope.py")

    assert len(violations) == 1
    assert violations[0].line == 2
    assert violations[0].error_code == "TR6"


def test_cross_file_call_site_conversion_is_flagged_at_permissive() -> None:
    # The headline gap issue #108 exists to close: a redundant conversion
    # passed as a call argument, where the parameter's own type lives in a
    # different file -- pyrefly's own `unnecessary-type-conversion` rule
    # (and every other existing tool) misses this shape entirely.
    violations = _check(BAD_ROOT / "cross_file_call_site.py", level=ConfidenceLevel.PERMISSIVE)

    assert {v.line for v in violations} == {5, 6}
    assert {v.error_code for v in violations} == {"TR6"}


def test_cross_file_call_site_conversion_is_not_flagged_at_conservative() -> None:
    # list/dict/set/bytearray are copy-producing -- excluded by default.
    violations = _check(BAD_ROOT / "cross_file_call_site.py")

    assert violations == []


def test_redundant_conversion_is_still_flagged_despite_an_unrelated_error_shifting_on_the_same_line() -> None:
    # Regression: an unrelated, pre-existing diagnostic later on the same
    # line (here, a genuinely wrong second call argument) keeps its exact
    # code/message after the conversion is spliced out, but its own
    # column position shifts left by however many characters were
    # removed. Treating that shift as "a new diagnostic" used to make
    # this candidate's own genuinely redundant conversion look unsafe to
    # flag.
    violations = _check(BAD_ROOT / "unrelated_diagnostic_shifts_on_same_line.py")

    assert len(violations) == 1
    assert violations[0].line == 6


def test_necessary_conversions_are_never_flagged() -> None:
    violations = _check(FIXTURES_ROOT / "good" / "necessary_conversions.py", level=ConfidenceLevel.PERMISSIVE)

    assert violations == []


def test_suppressed_conversions_are_never_flagged() -> None:
    violations = _check(FIXTURES_ROOT / "ignore" / "suppressed.py")

    assert violations == []


def test_the_real_installed_ty_still_passes_this_checks_own_self_test() -> None:
    # Deliberately unmocked: a failed self-test raises CheckUnavailableError, failing this test on its own.
    session = real_get_session()
    assert isinstance(session, TySession)
