from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._base import FixOutcome, Violation
from pre_commit_hooks.ast_checks.redundant_dict_get import RedundantDictGetCheck


def _check(source: str) -> list[Violation]:
    return RedundantDictGetCheck().check(Path("test.py"), ast.parse(source), source)


def test_identity_and_opt_in_properties() -> None:
    check = RedundantDictGetCheck()

    assert check.check_id == "redundant-dict-get"
    assert check.error_code == "TR9"
    assert check.default_enabled is True
    assert check.cacheable is True
    assert check.tracks_direct_inputs is False
    assert check.get_prefilter_pattern() == [".get("]


def test_check_reports_a_literal_dict_proof() -> None:
    source = "config = {'port': 5432}\nvalue = config.get('port')\n"

    violations = _check(source)

    assert len(violations) == 1
    assert violations[0].line == 2
    assert violations[0].col == 8
    assert violations[0].fixable is False
    assert "dict literal" in violations[0].message
    assert "config['port']" in violations[0].message
    assert "pytriage: TR9" in violations[0].message


@pytest.mark.parametrize(
    "source",
    [
        "value = config.get('port')\n",
        "config = {'port': 5432}\nvalue = config.get('port')  # pytriage: TR9\n",
        "value = config['port']\n",
    ],
)
def test_check_leaves_non_proofs_and_suppressed_calls_unreported(source: str) -> None:
    assert _check(source) == []


def test_check_skips_a_suppression_when_another_proof_is_active() -> None:
    source = (
        "first = {'port': 5432}\n"
        "second = {'host': 'localhost'}\n"
        "first_value = first.get('port')\n"
        "second_value = second.get('host')  # pytriage: TR9\n"
    )

    assert [violation.line for violation in _check(source)] == [3]


def test_tracking_records_a_suppressed_real_proof() -> None:
    source = "config = {'port': 5432}\nvalue = config.get('port')  # pytriage: TR9\n"

    check_result = RedundantDictGetCheck().check_with_suppression_tracking(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [(usage.check_id, usage.error_code, usage.line) for usage in check_result.suppression_usages] == [
        ("redundant-dict-get", "TR9", 2)
    ]


def test_fix_always_declines() -> None:
    violation = Violation(check_id="redundant-dict-get", error_code="TR9", line=1, col=0, message="x", fixable=False)

    fix_result = RedundantDictGetCheck().fix(Path("test.py"), [violation], "x = 1\n", ast.parse("x = 1\n"))

    assert fix_result.outcomes == (FixOutcome.DECLINED,)
