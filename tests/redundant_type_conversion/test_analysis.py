from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks._lsp import LSPError
from pre_commit_hooks.ast_checks._base import CheckUnavailableError
from pre_commit_hooks.ast_checks.redundant_type_conversion.analysis import (
    _DIAGNOSTICS_PROBE,
    _build_modified_text,
    decide_candidates,
)
from pre_commit_hooks.ast_checks.redundant_type_conversion.candidates import find_candidates
from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import (
    ALL_CONSTRUCTORS,
    ConfidenceLevel,
    eligible_constructors,
)

from ._helpers import FakeSession

if TYPE_CHECKING:
    from pre_commit_hooks.ast_checks.redundant_type_conversion.analysis import RedundantConversion


def _decide(
    source: str,
    *,
    diagnostics_by_content: dict[str, frozenset[tuple[object, ...]]],
    hover_by_position: dict[tuple[int, int], str | None],
    level: ConfidenceLevel = ConfidenceLevel.CONSERVATIVE,
    ignored_lines: set[int] | None = None,
) -> tuple[list[RedundantConversion], FakeSession]:
    session = FakeSession(diagnostics_by_content=diagnostics_by_content, hover_by_position=hover_by_position)
    candidates = find_candidates(ast.parse(source), eligible_constructors(level))
    redundant = decide_candidates(
        session, Path("test.py"), candidates, source, level=level, ignored_lines=ignored_lines or set()
    )
    return redundant, session


@pytest.mark.parametrize(
    ("source", "candidate_index", "expected"),
    [
        ("takes_list(list(bar))\n", 0, "takes_list(bar)\n"),
        ("x = 1; y = str(a); z = 2\n", 0, "x = 1; y = a; z = 2\n"),
        ("a = str(x)\nb = int(y)\n", 1, "a = str(x)\nb = y\n"),
    ],
    ids=["splices-out-just-the-wrapping-call", "preserves-surrounding-text-on-the-same-line", "only-touches-own-line"],
)
def test_build_modified_text(source: str, candidate_index: int, expected: str) -> None:
    candidates = find_candidates(ast.parse(source), ALL_CONSTRUCTORS)
    lines = source.splitlines(keepends=True)

    modified = _build_modified_text(lines, candidates[candidate_index])

    assert modified == expected


def test_decide_candidates_flags_a_redundant_conservative_case() -> None:
    source = "y = str(x)\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 8): "str"},
    )

    assert len(redundant) == 1
    assert redundant[0].line == 1
    assert redundant[0].argument_type == "str"
    assert session.closed_files == [Path("test.py")]


def test_decide_candidates_does_not_flag_when_recheck_finds_a_new_diagnostic() -> None:
    source = "y = str(x)\n"
    new_diagnostic = frozenset({("invalid-argument-type", "boom", 0, 0, 0, 5)})
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "y = x\n": new_diagnostic},
        hover_by_position={(0, 8): "str"},
    )

    assert redundant == []


def test_decide_candidates_skips_the_recheck_entirely_when_hover_gate_fails() -> None:
    source = "y = str(x)\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 8): "Any"},
    )

    assert redundant == []
    assert session.opened_content == [source, source + _DIAGNOSTICS_PROBE, source]


def test_decide_candidates_skips_a_file_outside_the_sessions_root(caplog: pytest.LogCaptureFixture) -> None:
    source = "y = str(x)\n"
    session = FakeSession(diagnostics_by_content={}, hover_by_position={}, within_root=False)
    candidates = find_candidates(ast.parse(source), eligible_constructors(ConfidenceLevel.CONSERVATIVE))

    redundant = decide_candidates(
        session, Path("test.py"), candidates, source, level=ConfidenceLevel.CONSERVATIVE, ignored_lines=set()
    )

    assert redundant == []
    assert session.opened_content == []
    assert "outside the `ty` session's own project root" in caplog.text


def test_decide_candidates_skips_a_file_ty_reports_no_diagnostics_for_at_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = "y = str(x)\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), source + _DIAGNOSTICS_PROBE: frozenset()},
        hover_by_position={(0, 8): "str"},
    )

    assert redundant == []
    assert session.opened_content == [source, source + _DIAGNOSTICS_PROBE, source]
    assert session.hover_calls == []
    assert "even for an obviously-invalid probe statement" in caplog.text


def test_decide_candidates_honors_ignored_lines_without_ever_opening_a_session() -> None:
    redundant, session = _decide(
        "y = str(x)\n",
        diagnostics_by_content={},
        hover_by_position={},
        ignored_lines={1},
    )

    assert redundant == []
    assert session.opened_content == []
    assert session.closed_files == []


def test_decide_candidates_hovers_the_arguments_own_last_character() -> None:
    source = "y = str(x)\n"
    _redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "y = x\n": frozenset()},
        hover_by_position={(0, 8): "str"},
    )

    assert session.hover_calls == [(0, 8)]


def test_decide_candidates_handles_a_multibyte_final_character_in_the_argument() -> None:
    source = "y = str(é)\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "y = é\n": frozenset()},
        hover_by_position={(0, 8): "str"},
    )

    assert len(redundant) == 1
    assert session.hover_calls == [(0, 8)]


def test_decide_candidates_conservative_excludes_mutable_constructors() -> None:
    source = "takes_list(list(bar))\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "takes_list(bar)\n": frozenset()},
        hover_by_position={(0, 18): "list[int]"},
    )

    assert redundant == []
    assert session.opened_content == []


def test_decide_candidates_opens_one_baseline_before_all_hovers() -> None:
    source = "a = str(x)\nb = int(y)\n"
    modified_1 = "a = x\nb = int(y)\n"
    modified_2 = "a = str(x)\nb = y\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), modified_1: frozenset(), modified_2: frozenset()},
        hover_by_position={(0, 8): "str", (1, 8): "int"},
    )

    assert len(redundant) == 2
    assert session.opened_content == [source, source + _DIAGNOSTICS_PROBE, source, modified_1, modified_2]


def test_decide_candidates_skips_a_len_wrapped_candidate_that_is_not_an_exact_match() -> None:
    source = "len(set(op_ids))\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 13): "list[int]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert redundant == []
    assert session.opened_content == [source, source + _DIAGNOSTICS_PROBE, source]


def test_decide_candidates_still_flags_a_len_wrapped_candidate_that_is_an_exact_match() -> None:
    source = "len(set(op_ids))\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "len(op_ids)\n": frozenset()},
        hover_by_position={(0, 13): "set[int]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert len(redundant) == 1
    assert redundant[0].candidate.constructor == "set"


def test_decide_candidates_skips_a_path_conversion_used_in_an_equality_comparison() -> None:
    source = "y = matches == [str(ignored)]\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 26): "Path"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert redundant == []
    assert session.opened_content == [source, source + _DIAGNOSTICS_PROBE, source]


def test_decide_candidates_still_flags_an_ordinary_conversion_used_in_an_equality_comparison() -> None:
    source = "y = matches == [str(ignored)]\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "y = matches == [ignored]\n": frozenset()},
        hover_by_position={(0, 26): "LiteralString"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert len(redundant) == 1


def test_decide_candidates_still_flags_a_path_hover_when_purepath_is_locally_ambiguous() -> None:
    source = "class Path:\n    pass\n\n\ny = matches == [str(ignored)]\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={
            source: frozenset(),
            "class Path:\n    pass\n\n\ny = matches == [ignored]\n": frozenset(),
        },
        hover_by_position={(4, 26): "Path"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert len(redundant) == 1


def test_decide_candidates_skips_a_tuple_conversion_used_in_a_subset_comparison() -> None:
    source = "assert expected <= set(PERFORMANCE_INDEXES)\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 41): "tuple[tuple[str, tuple[str, ...]], ...]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert redundant == []
    assert session.opened_content == [source, source + _DIAGNOSTICS_PROBE, source]


def test_decide_candidates_skips_a_dict_conversion_used_in_an_equality_comparison() -> None:
    source = "assert set(manager._executions) == {'race:0', 'race:1'}\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 29): "dict[str, Execution]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert redundant == []
    assert session.opened_content == [source, source + _DIAGNOSTICS_PROBE, source]


def test_decide_candidates_still_skips_a_dict_conversion_when_an_unrelated_path_name_is_shadowed() -> None:
    source = "class Path:\n    pass\n\n\nassert set(manager._executions) == {'race:0', 'race:1'}\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(4, 29): "dict[str, Execution]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert redundant == []


def test_decide_candidates_still_flags_a_frozenset_conversion_used_in_a_subset_comparison() -> None:
    source = "assert expected <= set(a_frozenset)\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "assert expected <= a_frozenset\n": frozenset()},
        hover_by_position={(0, 33): "frozenset[str]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert len(redundant) == 1


def test_decide_candidates_skips_a_non_exact_family_member_used_in_an_identity_comparison() -> None:
    source = "y = bytes(data) is data\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 13): "bytearray"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert redundant == []


def test_decide_candidates_still_flags_an_exact_match_used_in_an_identity_comparison() -> None:
    source = "y = str(x) is matches\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "y = x is matches\n": frozenset()},
        hover_by_position={(0, 8): "str"},
    )

    assert len(redundant) == 1


def test_decide_candidates_skips_an_int_conversion_used_in_a_float_comparison() -> None:
    source = "z = float(x) == other\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 10): "int"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert redundant == []


def test_decide_candidates_skips_a_non_exact_conversion_reachable_from_a_string_interpolation() -> None:
    source = "y = tuple(x)\nsql = f'{y}'\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},
        hover_by_position={(0, 10): "frozenset[int]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert redundant == []
    assert session.opened_content == [source, source + _DIAGNOSTICS_PROBE, source]


def test_decide_candidates_still_flags_an_exact_match_reachable_from_a_string_interpolation() -> None:
    source = "y = tuple(x)\nsql = f'{y}'\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "y = x\nsql = f'{y}'\n": frozenset()},
        hover_by_position={(0, 10): "tuple[int]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert len(redundant) == 1


def test_decide_candidates_aggressive_includes_mutable_constructors() -> None:
    source = "takes_list(list(bar))\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "takes_list(bar)\n": frozenset()},
        hover_by_position={(0, 18): "list[int]"},
        level=ConfidenceLevel.AGGRESSIVE,
    )

    assert len(redundant) == 1
    assert redundant[0].candidate.constructor == "list"


class _SessionRaisingFromIsWithinRoot(FakeSession):
    __slots__ = ()

    def is_within_root(self, _filepath: Path, /) -> bool:
        raise LSPError("simulated ty crash")


def test_decide_candidates_converts_a_lost_daemon_to_check_unavailable_error_during_the_root_check() -> None:
    source = "y = str(x)\n"
    session = _SessionRaisingFromIsWithinRoot(diagnostics_by_content={}, hover_by_position={})
    candidates = find_candidates(ast.parse(source), eligible_constructors(ConfidenceLevel.CONSERVATIVE))

    with pytest.raises(CheckUnavailableError, match="lost its connection to `ty`"):
        decide_candidates(
            session, Path("test.py"), candidates, source, level=ConfidenceLevel.CONSERVATIVE, ignored_lines=set()
        )


class _SessionRaisingFromOpenOrUpdate(FakeSession):
    __slots__ = ("_call_count", "_raise_on_call")

    def __init__(
        self,
        *,
        diagnostics_by_content: dict[str, frozenset[tuple[object, ...]]],
        hover_by_position: dict[tuple[int, int], str | None],
        raise_on_call: int,
    ) -> None:
        super().__init__(diagnostics_by_content=diagnostics_by_content, hover_by_position=hover_by_position)
        self._raise_on_call = raise_on_call
        self._call_count = 0

    def open_or_update(self, filepath: Path, content: str) -> frozenset[tuple[object, ...]]:
        self._call_count += 1
        if self._call_count == self._raise_on_call:
            raise LSPError("simulated ty crash")
        return super().open_or_update(filepath, content)


@pytest.mark.parametrize("raise_on_call", [1, 2], ids=["baseline-open-fails", "recheck-open-fails"])
def test_decide_candidates_converts_a_lost_session_to_check_unavailable_error(raise_on_call: int) -> None:
    source = "y = str(x)\n"
    session = _SessionRaisingFromOpenOrUpdate(
        diagnostics_by_content={source: frozenset(), "y = x\n": frozenset()},
        hover_by_position={(0, 8): "str"},
        raise_on_call=raise_on_call,
    )

    candidates = find_candidates(ast.parse(source), eligible_constructors(ConfidenceLevel.CONSERVATIVE))
    with pytest.raises(CheckUnavailableError, match="lost its connection to `ty`"):
        decide_candidates(
            session, Path("test.py"), candidates, source, level=ConfidenceLevel.CONSERVATIVE, ignored_lines=set()
        )

    assert session.closed_files == [Path("test.py")]


class _SessionRaisingFromHover(FakeSession):
    __slots__ = ()

    def hover(self, _filepath: Path, _line0: int, _char_utf16: int) -> str | None:
        raise RuntimeError("unexpected failure")


def test_decide_candidates_still_closes_the_file_when_a_candidate_raises_unexpectedly() -> None:
    source = "y = str(x)\n"
    session = _SessionRaisingFromHover(diagnostics_by_content={source: frozenset()}, hover_by_position={})
    candidates = find_candidates(ast.parse(source), eligible_constructors(ConfidenceLevel.CONSERVATIVE))

    with pytest.raises(RuntimeError, match="unexpected failure"):
        decide_candidates(
            session, Path("test.py"), candidates, source, level=ConfidenceLevel.CONSERVATIVE, ignored_lines=set()
        )

    assert session.closed_files == [Path("test.py")]
