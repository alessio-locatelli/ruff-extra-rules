from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from pre_commit_hooks.ast_checks.redundant_assignment.semantic import AggressivenessLevel
from tests.redundant_assignment._helpers import _check

CASSETTE_SOURCE = """def to_vcr_cassette_dict(responses):
    return list(responses)


def to_vcr_cassette(cache, path):
    responses = cache.responses.values()
    write_cassette(to_vcr_cassette_dict(responses), path)
"""


@pytest.mark.parametrize("level", tuple(AggressivenessLevel), ids=["conservative", "aggressive"])
@pytest.mark.parametrize(
    ("rhs", "use", "conservative_reported"),
    [
        ("load()", "return consume(value)", True),
        ("load()", "return outer(inner(value))", False),
        ("load(inner())", "return consume(value)", False),
        ("load(inner())", "return value", True),
        ("obj.first()", "return value.second()", True),
        ("obj.first()", "return value.second().third()", False),
        ("load()", "return outer(inner(value=value))", False),
        ("1", "return outer(inner(value=value))", True),
        ("1", "return outer(middle(inner(value)))", False),
        ("1", "return consume(other(), value=value)", True),
        ("1", "return consume(value=value, other=outer(inner(1)))", False),
        ("load()", "return consume(value=value, other=inner(1))", True),
        ("load()", "return consume(\n        value=value,\n    )", True),
        ("[\n        1,\n    ]", "return consume(value=value)", False),
        ("[\n        1,\n    ]", "return value", False),
        ("load()", "return value[inner()](1)", True),
        ("load()", "return value[inner()](1).method()", False),
    ],
    ids=[
        "two-call-layers",
        "three-call-layers",
        "nested-rhs-three-layers",
        "nested-rhs-two-layers",
        "two-method-layers",
        "three-method-layers",
        "keyword-echo-three-layers",
        "keyword-echo-two-layers",
        "existing-three-call-layers",
        "sibling-calls",
        "complex-sibling-expression",
        "nested-sibling-keeps-two-layers",
        "multiline-use",
        "multiline-rhs-keyword-echo",
        "multiline-rhs-return",
        "subscript-callee-two-layers",
        "subscript-callee-three-layers",
    ],
)
def test_call_nesting_and_multiline_readability(
    rhs: str, use: str, *, conservative_reported: bool, level: AggressivenessLevel
) -> None:
    source = f"def example():\n    value = {rhs}\n    {use}\n"
    violations = _check(source, level=level)

    assert bool(violations) is (conservative_reported or level is AggressivenessLevel.AGGRESSIVE)


@pytest.mark.parametrize("level", tuple(AggressivenessLevel), ids=["conservative", "aggressive"])
def test_cassette_argument_echo_readability(level: AggressivenessLevel) -> None:
    violations = _check(CASSETTE_SOURCE, level=level)

    assert len(violations) == (level is AggressivenessLevel.AGGRESSIVE)
    if violations:
        assert "parameter also named 'responses'" in violations[0].message


@pytest.mark.parametrize("level", tuple(AggressivenessLevel), ids=["conservative", "aggressive"])
@pytest.mark.parametrize("character", ["a", "é"], ids=["ascii", "unicode"])
@pytest.mark.parametrize("indent", [4, 8], ids=["function", "conditional"])
@pytest.mark.parametrize(
    ("rhs", "use_length", "conservative_reported"),
    [("load()", 78, True), ("load()", 79, False), ("1", 79, True), ("1", 80, False)],
    ids=["projected-79", "projected-80", "existing-79", "existing-80-shrinks"],
)
def test_use_line_length_readability(
    rhs: str,
    use_length: int,
    *,
    conservative_reported: bool,
    character: str,
    indent: int,
    level: AggressivenessLevel,
) -> None:
    indentation = " " * indent
    use_line = f"{indentation}return consume(value=value, label='')"
    label = character * (use_length - len(use_line))
    use_line = f"{indentation}return consume(value=value, label='{label}')"
    header = "def example():\n" if indent == 4 else "def example():\n    if ready:\n"
    source = f"{header}{indentation}value = {rhs}\n{use_line}\n"
    violations = _check(source, level=level)

    assert bool(violations) is (conservative_reported or level is AggressivenessLevel.AGGRESSIVE)


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("level", tuple(AggressivenessLevel), ids=["conservative", "aggressive"])
def test_multiline_rhs_readability_with_newline_styles(newline: str, level: AggressivenessLevel) -> None:
    source = "def example():\n    value = [\n        1,\n    ]\n    return value\n".replace("\n", newline)

    assert bool(_check(source, level=level)) is (level is AggressivenessLevel.AGGRESSIVE)


@pytest.mark.parametrize("level", tuple(AggressivenessLevel), ids=["conservative", "aggressive"])
@pytest.mark.parametrize(
    ("rhs", "use"),
    [
        ("load()", "return outer(inner(value))"),
        ("1", "return consume(value=value, label='" + "a" * 50 + "')"),
        ("[\n        1,\n    ]", "return consume(value=value)"),
    ],
    ids=["call-depth", "line-length", "multiline-rhs"],
)
def test_readability_exemptions_do_not_consume_suppressions(rhs: str, use: str, level: AggressivenessLevel) -> None:
    source = f"def example():\n    value = {rhs}\n    {use}  # pytriage: TR5\n"
    check = RedundantAssignmentCheck(level=level)
    check_result = check.check(Path("example.py"), ast.parse(source), source)

    assert check_result == []
    assert len(check_result.suppression_usages) == (level is AggressivenessLevel.AGGRESSIVE)


@pytest.fixture
def readability_file(tmp_path: Path, source: str) -> Path:
    filepath = tmp_path / "example.py"
    filepath.write_text(source)
    return filepath


@pytest.mark.parametrize("level", tuple(AggressivenessLevel), ids=["conservative", "aggressive"])
@pytest.mark.parametrize(
    ("source", "conservative_reported"),
    [
        (CASSETTE_SOURCE, False),
        ("def example():\n    value = load()\n    return consume(value)\n", True),
        ("def example():\n    value = load()\n    return outer(inner(value))\n", False),
    ],
    ids=["cassette", "simple-call", "nested-call"],
)
def test_readability_fix_behavior(
    source: str, readability_file: Path, *, conservative_reported: bool, level: AggressivenessLevel
) -> None:
    check = RedundantAssignmentCheck(level=level)
    tree = ast.parse(source)
    violations = check.check(readability_file, tree, source)
    expected_reported = conservative_reported or level is AggressivenessLevel.AGGRESSIVE

    assert bool(violations) is expected_reported
    assert all(violation.fixable for violation in violations)

    check.fix(readability_file, violations, source, tree)
    fixed_source = readability_file.read_text()

    assert (fixed_source != source) is expected_reported
    assert check.check(readability_file, ast.parse(fixed_source), fixed_source) == []
