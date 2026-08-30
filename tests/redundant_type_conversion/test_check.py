from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

import pre_commit_hooks.ast_checks.redundant_type_conversion as tri006_module
import pre_commit_hooks.ast_checks.redundant_type_conversion.daemon as daemon_module
from pre_commit_hooks.ast_checks._base import FixOutcome, Violation
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
    assert RedundantTypeConversionCheck().cacheable is False


def test_tracks_direct_inputs() -> None:
    assert RedundantTypeConversionCheck().tracks_direct_inputs is True


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
    monkeypatch.setattr(daemon_module, "socket_exists_for", lambda _root: False)
    pattern = RedundantTypeConversionCheck(level=level).get_prefilter_pattern()
    assert pattern is not None
    assert set(pattern) == expected  # pytriage: TR6


def test_fix_always_declines() -> None:
    check = RedundantTypeConversionCheck()
    violation = Violation(
        check_id="redundant-type-conversion", error_code="TR6", line=1, col=0, message="x", fixable=False
    )
    assert check.fix(Path("test.py"), [violation], "x = 1\n", ast.parse("x = 1\n")).outcomes == (FixOutcome.DECLINED,)


def test_check_never_calls_get_session_when_the_file_has_no_real_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "print(1)\n"
    monkeypatch.setattr(tri006_module, "get_session", _fail_if_called)

    violations = RedundantTypeConversionCheck().check(Path("test.py"), ast.parse(source), source)

    assert violations == []


def test_check_never_calls_get_session_when_every_candidate_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)  # pytriage: TR6\n"
    monkeypatch.setattr(tri006_module, "get_session", _fail_if_called)

    violations = RedundantTypeConversionCheck().check(Path("test.py"), ast.parse(source), source)

    assert violations == []


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


def test_check_reuses_a_cached_result_for_identical_source(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)\n"
    session = FakeSession(
        diagnostics_by_content={source: frozenset(), "y = x\n": frozenset()},
        hover_by_position={(0, 8): "str"},
    )
    _patch_session(monkeypatch, session)
    check = RedundantTypeConversionCheck()

    first = check.check(Path("test.py"), ast.parse(source), source)
    opened_after_first = session.opened_content.copy()
    second = check.check(Path("test.py"), ast.parse(source), source)

    assert second == first
    assert session.opened_content == opened_after_first


def test_check_hedges_the_message_for_a_non_exact_permissive_match(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_tracking_check_records_a_pytriage_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)  # pytriage: TR6\n"
    session = FakeSession(
        diagnostics_by_content={source: frozenset(), "y = x  # pytriage: TR6\n": frozenset()},
        hover_by_position={(0, 8): "str"},
    )
    _patch_session(monkeypatch, session)

    check_result = RedundantTypeConversionCheck().check_with_suppression_tracking(
        Path("test.py"), ast.parse(source), source
    )

    assert check_result == []
    assert [(usage.check_id, usage.error_code, usage.line) for usage in check_result.suppression_usages] == [
        ("redundant-type-conversion", "TR6", 1)
    ]


def test_tracking_check_does_not_reuse_normal_analysis_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "y = str(x)  # pytriage: TR6\nz = int(value)\n"
    session = FakeSession(diagnostics_by_content={}, hover_by_position={})
    _patch_session(monkeypatch, session)
    ignored_lines_seen: list[set[int]] = []

    def decide(
        _session: object,
        _filepath: Path,
        _candidates: object,
        _source: str,
        *,
        level: object,  # noqa: ARG001
        ignored_lines: set[int],
    ) -> list[SimpleNamespace]:
        ignored_lines_seen.append(ignored_lines)
        return [
            SimpleNamespace(candidate=SimpleNamespace(constructor="str"), line=1, col=4, argument_type="str"),
            SimpleNamespace(candidate=SimpleNamespace(constructor="int"), line=2, col=4, argument_type="int"),
        ]

    monkeypatch.setattr(tri006_module, "decide_candidates", decide)
    check = RedundantTypeConversionCheck()

    assert check.check(Path("test.py"), ast.parse(source), source)[0].line == 2
    tracked = check.check_with_suppression_tracking(Path("test.py"), ast.parse(source), source)

    assert ignored_lines_seen == [{1}, set()]
    assert [usage.line for usage in tracked.suppression_usages] == [1]
    assert [violation.line for violation in tracked] == [2]


def test_tracking_check_records_each_pytriage_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)  # pytriage: TR6\nz = int(a)  # pytriage: TR6\n"
    session = FakeSession(
        diagnostics_by_content={
            source: frozenset(),
            "y = x  # pytriage: TR6\nz = int(a)  # pytriage: TR6\n": frozenset(),
            "y = str(x)  # pytriage: TR6\nz = a  # pytriage: TR6\n": frozenset(),
        },
        hover_by_position={(0, 8): "str", (1, 8): "int"},
    )
    _patch_session(monkeypatch, session)

    check_result = RedundantTypeConversionCheck().check_with_suppression_tracking(
        Path("test.py"), ast.parse(source), source
    )

    assert check_result == []
    assert [usage.line for usage in check_result.suppression_usages] == [1, 2]


def test_tracking_check_handles_cached_format_suppressed_redundancies(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "y = str(x)  # pytriage: TR6\n# fmt: off\nz = int(a)\n# fmt: on\nw = bool(value)\n"
    session = FakeSession(diagnostics_by_content={}, hover_by_position={})
    redundancies = [
        ("str", 1, 8, "str"),
        ("int", 3, 8, "int"),
        ("bool", 5, 8, "bool"),
    ]
    filepath = Path("test.py")
    session.cache_redundancies(
        filepath,
        source,
        tri006_module._cache_key(ConfidenceLevel.CONSERVATIVE, collect_suppression_usage=False),
        redundancies,
    )
    session.cache_redundancies(
        filepath,
        source,
        tri006_module._cache_key(ConfidenceLevel.CONSERVATIVE, collect_suppression_usage=True),
        redundancies,
    )
    _patch_session(monkeypatch, session)
    check = RedundantTypeConversionCheck()

    tracked = check.check_with_suppression_tracking(filepath, ast.parse(source), source)
    direct = check.check(filepath, ast.parse(source), source)

    assert [usage.line for usage in tracked.suppression_usages] == [1]
    assert [violation.line for violation in direct] == [5]


@pytest.mark.parametrize(
    "comment",
    ["# type: ignore", "# pyright: ignore", "# pyright: ignore[reportArgumentType]", "# ty: ignore", "# TY: IGNORE"],
    ids=["type-ignore", "pyright-ignore", "pyright-ignore-code", "ty-ignore", "case-insensitive"],
)
def test_check_skips_a_line_with_a_third_party_suppression_comment(
    monkeypatch: pytest.MonkeyPatch, comment: str
) -> None:
    source = f"y = str(x)  {comment}\n"
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
