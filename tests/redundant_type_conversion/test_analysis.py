from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks.redundant_type_conversion.analysis import _build_modified_text, decide_candidates
from pre_commit_hooks.ast_checks.redundant_type_conversion.candidates import find_candidates
from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import ALL_CONSTRUCTORS, ConfidenceLevel

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
    redundant = decide_candidates(
        session, Path("test.py"), ast.parse(source), source, level=level, ignored_lines=ignored_lines or set()
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
        diagnostics_by_content={source: frozenset(), "y = x\n": frozenset()},
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
        diagnostics_by_content={source: frozenset()},  # "y = x\n" deliberately not recorded
        hover_by_position={(0, 8): "Any"},
    )

    assert redundant == []
    # Only the baseline open -- the expensive synthetic-rewrite-and-recheck
    # must never run for a candidate the cheap hover gate already rejected.
    assert session.opened_content == [source]


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
    # Regression: the hover position used to be computed by subtracting 1
    # directly in UTF-8 *byte* space from the argument's own end offset.
    # That offset is only a valid boundary when the argument's own last
    # character is single-byte in UTF-8 -- for a multi-byte final
    # character (e.g. 'é', 2 bytes), it landed mid-character and raised
    # UnicodeDecodeError on otherwise ordinary, valid Python source.
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
    assert session.opened_content == []  # never even worth opening the baseline


def test_decide_candidates_restores_pristine_source_before_each_candidates_hover() -> None:
    # Regression: hover used to run against whatever the *previous*
    # candidate's own synthetic rewrite left the in-memory document as,
    # rather than the file's real, unmodified content -- with two
    # candidates, the second one's hover would see the first candidate's
    # rewrite still in place instead of the original source. The recorded
    # open_or_update() sequence is the direct evidence: `source` must be
    # reopened before candidate 2's own hover, not left at `modified_1`.
    source = "a = str(x)\nb = int(y)\n"
    modified_1 = "a = x\nb = int(y)\n"
    modified_2 = "a = str(x)\nb = y\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), modified_1: frozenset(), modified_2: frozenset()},
        hover_by_position={(0, 8): "str", (1, 8): "int"},
    )

    assert len(redundant) == 2
    assert session.opened_content == [source, modified_1, source, modified_2]


def test_decide_candidates_skips_a_len_wrapped_candidate_that_is_not_an_exact_match() -> None:
    # See ADR-0035's `len()` sink exclusion.
    source = "len(set(op_ids))\n"
    redundant, session = _decide(
        source,
        diagnostics_by_content={source: frozenset()},  # "len(op_ids)\n" deliberately not recorded
        hover_by_position={(0, 13): "list[int]"},
        level=ConfidenceLevel.PERMISSIVE,
    )

    assert redundant == []
    # The expensive synthetic-rewrite-and-recheck must never run once the
    # len()-wrap exclusion alone already decided this candidate.
    assert session.opened_content == [source]


def test_decide_candidates_still_flags_a_len_wrapped_candidate_that_is_an_exact_match() -> None:
    source = "len(set(op_ids))\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "len(op_ids)\n": frozenset()},
        hover_by_position={(0, 13): "set[int]"},
        level=ConfidenceLevel.PERMISSIVE,
    )

    assert len(redundant) == 1
    assert redundant[0].candidate.constructor == "set"


def test_decide_candidates_permissive_includes_mutable_constructors() -> None:
    source = "takes_list(list(bar))\n"
    redundant, _session = _decide(
        source,
        diagnostics_by_content={source: frozenset(), "takes_list(bar)\n": frozenset()},
        hover_by_position={(0, 18): "list[int]"},
        level=ConfidenceLevel.PERMISSIVE,
    )

    assert len(redundant) == 1
    assert redundant[0].candidate.constructor == "list"
