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


@pytest.mark.parametrize(
    ("source", "diagnostics_by_content", "hover_by_position", "level"),
    [
        (
            "y = str(x)\n",
            {
                "y = str(x)\n": frozenset(),
                "y = x\n": {("invalid-argument-type", "boom", 0, 0, 0, 5)},
            },
            {(0, 8): "str"},
            ConfidenceLevel.CONSERVATIVE,
        ),
        (
            "len(set(op_ids))\n",
            {"len(set(op_ids))\n": frozenset()},
            {(0, 13): "list[int]"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "len(dict(m))\n",
            {"len(dict(m))\n": frozenset()},
            {(0, 9): "Mapping[str, int]"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "y = matches == [str(ignored)]\n",
            {"y = matches == [str(ignored)]\n": frozenset()},
            {(0, 26): "Path"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "assert expected <= set(PERFORMANCE_INDEXES)\n",
            {"assert expected <= set(PERFORMANCE_INDEXES)\n": frozenset()},
            {(0, 41): "tuple[tuple[str, tuple[str, ...]], ...]"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "assert set(manager._executions) == {'race:0', 'race:1'}\n",
            {"assert set(manager._executions) == {'race:0', 'race:1'}\n": frozenset()},
            {(0, 29): "dict[str, Execution]"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "class Path:\n    pass\n\n\nassert set(manager._executions) == {'race:0', 'race:1'}\n",
            {"class Path:\n    pass\n\n\nassert set(manager._executions) == {'race:0', 'race:1'}\n": frozenset()},
            {(4, 29): "dict[str, Execution]"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "y = bytes(data) is data\n",
            {"y = bytes(data) is data\n": frozenset()},
            {(0, 13): "bytearray"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "y = list(data) is data\n",
            {"y = list(data) is data\n": frozenset(), "y = data is data\n": frozenset()},
            {(0, 14): "list[int]"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "y = bytes(data) in container\n",
            {"y = bytes(data) in container\n": frozenset(), "y = data in container\n": frozenset()},
            {(0, 15): "bytearray"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "z = float(x) == other\n",
            {"z = float(x) == other\n": frozenset()},
            {(0, 10): "int"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "y = tuple(x)\nsql = f'{y}'\n",
            {"y = tuple(x)\nsql = f'{y}'\n": frozenset()},
            {(0, 10): "frozenset[int]"},
            ConfidenceLevel.AGGRESSIVE,
        ),
        (
            "y = dict(x)\ny['k'] = 1\n",
            {"y = dict(x)\ny['k'] = 1\n": frozenset(), "y = x\ny['k'] = 1\n": frozenset()},
            {(0, 9): "dict[str, int]"},
            ConfidenceLevel.AGGRESSIVE,
        ),
    ],
    ids=[
        "recheck-finds-a-new-diagnostic",
        "len-wrapped-set-not-an-exact-match",
        "len-wrapped-dict-not-an-exact-match",
        "path-conversion-in-an-equality-comparison",
        "tuple-conversion-in-a-subset-comparison",
        "dict-conversion-in-an-equality-comparison",
        "dict-conversion-with-an-unrelated-path-name-shadowed",
        "non-exact-family-member-in-an-identity-comparison",
        "mutable-constructor-in-an-identity-comparison",
        "bytearray-conversion-in-a-membership-test",
        "int-conversion-in-a-float-comparison",
        "non-exact-conversion-reachable-from-a-string-interpolation",
        "mutable-constructor-later-mutated",
    ],
)
def test_decide_candidates_skips(
    source: str,
    diagnostics_by_content: dict[str, frozenset[tuple[object, ...]]],
    hover_by_position: dict[tuple[int, int], str | None],
    level: ConfidenceLevel,
) -> None:
    redundant, _session = _decide(
        source, diagnostics_by_content=diagnostics_by_content, hover_by_position=hover_by_position, level=level
    )

    assert redundant == []


@pytest.mark.parametrize(
    ("source", "diagnostics_by_content", "hover_by_position", "level", "expected_constructor"),
    [
        (
            "y = matches == [str(ignored)]\n",
            {"y = matches == [str(ignored)]\n": frozenset(), "y = matches == [ignored]\n": frozenset()},
            {(0, 26): "LiteralString"},
            ConfidenceLevel.AGGRESSIVE,
            "str",
        ),
        (
            "class Path:\n    pass\n\n\ny = matches == [str(ignored)]\n",
            {
                "class Path:\n    pass\n\n\ny = matches == [str(ignored)]\n": frozenset(),
                "class Path:\n    pass\n\n\ny = matches == [ignored]\n": frozenset(),
            },
            {(4, 26): "Path"},
            ConfidenceLevel.AGGRESSIVE,
            "str",
        ),
        (
            "assert expected <= set(a_frozenset)\n",
            {"assert expected <= set(a_frozenset)\n": frozenset(), "assert expected <= a_frozenset\n": frozenset()},
            {(0, 33): "frozenset[str]"},
            ConfidenceLevel.AGGRESSIVE,
            "set",
        ),
        (
            "y = str(x) is matches\n",
            {"y = str(x) is matches\n": frozenset(), "y = x is matches\n": frozenset()},
            {(0, 8): "str"},
            ConfidenceLevel.CONSERVATIVE,
            "str",
        ),
        (
            "y = tuple(x)\nsql = f'{y}'\n",
            {"y = tuple(x)\nsql = f'{y}'\n": frozenset(), "y = x\nsql = f'{y}'\n": frozenset()},
            {(0, 10): "tuple[int]"},
            ConfidenceLevel.AGGRESSIVE,
            "tuple",
        ),
        (
            "y = dict(x)\nprint(y)\n",
            {"y = dict(x)\nprint(y)\n": frozenset(), "y = x\nprint(y)\n": frozenset()},
            {(0, 9): "dict[str, int]"},
            ConfidenceLevel.AGGRESSIVE,
            "dict",
        ),
        (
            "len(set(op_ids))\n",
            {"len(set(op_ids))\n": frozenset(), "len(op_ids)\n": frozenset()},
            {(0, 13): "set[int]"},
            ConfidenceLevel.AGGRESSIVE,
            "set",
        ),
        (
            "takes_list(list(bar))\n",
            {"takes_list(list(bar))\n": frozenset(), "takes_list(bar)\n": frozenset()},
            {(0, 18): "list[int]"},
            ConfidenceLevel.AGGRESSIVE,
            "list",
        ),
    ],
    ids=[
        "ordinary-conversion-in-an-equality-comparison",
        "path-hover-when-purepath-is-locally-ambiguous",
        "frozenset-conversion-in-a-subset-comparison",
        "exact-match-in-an-identity-comparison",
        "exact-match-reachable-from-a-string-interpolation",
        "mutable-constructor-never-mutated",
        "len-wrapped-candidate-that-is-an-exact-match",
        "aggressive-includes-mutable-constructors",
    ],
)
def test_decide_candidates_still_flags(
    source: str,
    diagnostics_by_content: dict[str, frozenset[tuple[object, ...]]],
    hover_by_position: dict[tuple[int, int], str | None],
    level: ConfidenceLevel,
    expected_constructor: str,
) -> None:
    redundant, _session = _decide(
        source, diagnostics_by_content=diagnostics_by_content, hover_by_position=hover_by_position, level=level
    )

    assert len(redundant) == 1
    assert redundant[0].candidate.constructor == expected_constructor


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
    assert "even for two obviously-invalid probe statements of unrelated diagnostic rules" in caplog.text


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
