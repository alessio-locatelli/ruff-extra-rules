from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import NoReturn

import pytest

import pre_commit_hooks.ast_checks.redundant_type_conversion as tri006_module
import pre_commit_hooks.ast_checks.redundant_type_conversion.daemon as daemon_module
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks.redundant_type_conversion import RedundantTypeConversionCheck
from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import ConfidenceLevel

from ._helpers import FakeSession


def _fail_if_called() -> NoReturn:
    raise AssertionError("get_session() must not be called")


def test_check_id_and_error_code() -> None:
    check = RedundantTypeConversionCheck()
    assert check.check_id == "redundant-type-conversion"
    assert check.error_code == "TR6"


def test_is_not_cacheable() -> None:
    # See ADR-0034: this check's result for one file can depend on another
    # file's current content (a cross-file import's parameter type), so it
    # must always re-run fresh rather than ever reading/writing the shared
    # per-file cache.
    assert RedundantTypeConversionCheck().cacheable is False


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (
            ConfidenceLevel.CONSERVATIVE,
            {"str(", "int(", "float(", "bool(", "bytes(", "frozenset(", "tuple("},
        ),
        (
            ConfidenceLevel.PERMISSIVE,
            {
                "str(",
                "int(",
                "float(",
                "bool(",
                "bytes(",
                "frozenset(",
                "tuple(",
                "list(",
                "dict(",
                "set(",
                "bytearray(",
            },
        ),
    ],
    ids=["conservative", "permissive"],
)
def test_prefilter_pattern_matches_the_configured_levels_eligible_constructors(
    level: ConfidenceLevel, expected: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Narrowed to eligible_constructors(level), not every constructor TR6 knows about -- not the widened
    # "check everything" None a real daemon for this exact repository would otherwise trigger (ADR-0041).
    monkeypatch.setattr(daemon_module, "socket_exists_for", lambda _root: False)
    pattern = RedundantTypeConversionCheck(level=level).get_prefilter_pattern()
    assert pattern is not None
    assert set(pattern) == expected  # pytriage: TR6


def test_fix_never_applies_a_fix() -> None:
    check = RedundantTypeConversionCheck()
    assert check.fix(Path("test.py"), [], "x = 1\n", ast.parse("x = 1\n")) is False


def test_check_never_calls_get_session_when_the_file_has_no_real_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    # "print(1)" matches the "int(" prefilter substring pattern at the file
    # level, but find_candidates() correctly rejects it at the AST level
    # (print isn't an eligible constructor) -- get_session(), which starts
    # `ty` on this process's first call, must never be reached for a file
    # like this.
    source = "print(1)\n"
    monkeypatch.setattr(tri006_module, "get_session", _fail_if_called)

    violations = RedundantTypeConversionCheck().check(Path("test.py"), ast.parse(source), source)

    assert violations == []


def test_check_never_calls_get_session_when_every_candidate_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)  # pytriage: TR6\n"
    monkeypatch.setattr(tri006_module, "get_session", _fail_if_called)

    violations = RedundantTypeConversionCheck().check(Path("test.py"), ast.parse(source), source)

    assert violations == []


@pytest.mark.parametrize(
    ("cli_value", "expected"),
    [("conservative", ConfidenceLevel.CONSERVATIVE), ("permissive", ConfidenceLevel.PERMISSIVE)],
)
def test_cli_kwargs_from_args_round_trip(cli_value: str, expected: ConfidenceLevel) -> None:

    parser = argparse.ArgumentParser()
    RedundantTypeConversionCheck.add_cli_arguments(parser)
    args = parser.parse_args(["--redundant-type-conversion-level", cli_value])
    assert RedundantTypeConversionCheck.cli_kwargs_from_args(args) == {"level": expected}


def test_cli_level_flag_defaults_to_conservative() -> None:

    parser = argparse.ArgumentParser()
    RedundantTypeConversionCheck.add_cli_arguments(parser)
    assert RedundantTypeConversionCheck.cli_kwargs_from_args(parser.parse_args([])) == {
        "level": ConfidenceLevel.CONSERVATIVE
    }


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    monkeypatch.setattr(tri006_module, "get_session", lambda: session)


def test_check_flags_a_redundant_conservative_case(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)\n"
    session = FakeSession(
        diagnostics_by_content={source: frozenset(), "y = x\n": frozenset()},
        hover_by_position={(0, 8): "str"},
    )
    _patch_session(monkeypatch, session)

    violations = RedundantTypeConversionCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 1
    assert violations[0].check_id == "redundant-type-conversion"
    assert violations[0].error_code == "TR6"
    assert violations[0].line == 1
    assert violations[0].fixable is False
    assert "str" in violations[0].message
    assert "pytriage: TR6" in violations[0].message


def test_check_hedges_the_message_for_a_non_exact_permissive_match(monkeypatch: pytest.MonkeyPatch) -> None:
    # See ADR-0035's permissive-gate limitation.
    source = "y = str({'a': [1]}) == 1\n"
    session = FakeSession(
        diagnostics_by_content={source: frozenset(), "y = {'a': [1]} == 1\n": frozenset()},
        hover_by_position={(0, 17): "dict[str, list[int]]"},
    )
    _patch_session(monkeypatch, session)

    violations = RedundantTypeConversionCheck(level=ConfidenceLevel.PERMISSIVE).check(
        Path("test.py"), ast.parse(source), source
    )

    assert len(violations) == 1
    message = violations[0].message
    assert "already `str`" not in message
    assert "dict[str, list[int]]" in message
    assert "not `str`" in message
    assert "pytriage: TR6" in message


def test_check_honors_pytriage_inline_ignore(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)  # pytriage: TR6\n"
    session = FakeSession(diagnostics_by_content={}, hover_by_position={})
    _patch_session(monkeypatch, session)

    violations = RedundantTypeConversionCheck().check(Path("test.py"), ast.parse(source), source)

    assert violations == []
    assert session.opened_content == []


@pytest.mark.parametrize(
    "comment",
    ["# type: ignore", "# pyright: ignore", "# pyright: ignore[reportArgumentType]", "# ty: ignore", "# TY: IGNORE"],
    ids=["type-ignore", "pyright-ignore", "pyright-ignore-code", "ty-ignore", "case-insensitive"],
)
def test_check_skips_a_line_with_a_third_party_suppression_comment(
    monkeypatch: pytest.MonkeyPatch, comment: str
) -> None:
    source = f"y = str(x)  {comment}\n"
    # No recorded responses at all: if the check tried to reach the
    # session for this candidate, FakeSession.open_or_update/hover would
    # raise a KeyError-free but semantically wrong empty/None result --
    # asserting opened_content stays empty is the real proof it never got
    # that far.
    session = FakeSession(diagnostics_by_content={}, hover_by_position={})
    _patch_session(monkeypatch, session)

    violations = RedundantTypeConversionCheck().check(Path("test.py"), ast.parse(source), source)

    assert violations == []
    assert session.opened_content == []


def test_check_does_not_flag_a_line_with_an_unrelated_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)  # not a suppression comment\n"
    session = FakeSession(
        diagnostics_by_content={source: frozenset(), "y = x  # not a suppression comment\n": frozenset()},
        hover_by_position={(0, 8): "str"},
    )
    _patch_session(monkeypatch, session)

    violations = RedundantTypeConversionCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 1


def test_end_to_end_through_main_reports_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = "y = str(x)\n"
    filepath = tmp_path / "module.py"
    filepath.write_text(source)

    session = FakeSession(
        diagnostics_by_content={source: frozenset(), "y = x\n": frozenset()},
        hover_by_position={(0, 8): "str"},
    )
    _patch_session(monkeypatch, session)

    exit_code = main([str(filepath), "--select=redundant-type-conversion"])

    assert exit_code == 1
    assert "TR6" in capsys.readouterr().err
