from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._base import FixOutcome
from pre_commit_hooks.ast_checks.excessive_blank_lines import (
    ExcessiveBlankLinesCheck,
    fix_file_content,
)
from tests.factories import ViolationFactory

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "excessive_blank_lines"


def _check(source: str) -> list[str]:
    violations = ExcessiveBlankLinesCheck().check(Path("test.py"), ast.parse(source), source)
    return [v.message for v in violations]


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "bad").glob("*.py")),
    ids=lambda p: p.name,
)
def test_bad_fixtures_are_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text())


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "good").glob("*.py")),
    ids=lambda p: p.name,
)
def test_good_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text()) == []


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "ignore").glob("*.py")),
    ids=lambda p: p.name,
)
def test_ignore_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text()) == []


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        # Raw/byte-prefixed docstrings must be detected via the
        # AST — a raw-text quote-prefix scan misses the r/b prefix entirely
        # and would treat the whole file as one giant docstring.
        ('r"""Raw docstring."""\n\n\n\nimport os\n', True),
        # A file with only comments (no code at all) has no first code
        # line, so the header-end scan runs off the end of the file.
        ("# just a comment\n\n# another comment\n", False),
        ("", False),
        # The blank run's own line is blank, so the ignore comment goes on
        # the first code line after it instead.
        ('"""Docstring."""\n\n\n\ndef foo():  # pytriage: TR2\n    pass\n', False),
        # A trailing # fmt: skip on the anchor line suppresses the violation
        # the same way the project's own inline ignore comment does — see
        # docs/adr/0050-format-suppression-pragmas.md. (A standalone
        # `# fmt: off` line here would instead get swallowed into the
        # module header by find_module_header_end's own comment handling,
        # eliminating the violation before ignored_lines is ever
        # consulted — not what this case means to exercise.)
        ('"""Docstring."""\n\n\n\ndef foo():  # fmt: skip\n    pass\n', False),
    ],
    ids=["raw-prefixed-docstring", "comment-only-file", "empty-file", "inline-ignore", "fmt-skip-anchor-line"],
)
def test_check_edge_cases(source: str, *, flagged: bool) -> None:
    assert bool(_check(source)) is flagged


def test_leading_blank_lines_before_first_code_with_no_header() -> None:
    # No docstring/comment header, just leading blank lines before the
    # first code line, so the whole leading run is treated as the gap
    # before the first code line.
    assert _check("\n\n\nimport os\n") == [
        (
            "Excessive blank lines (3) should be collapsed to 1. Add "
            "'# pytriage: TR2' to the line following the blank run "
            "to suppress."
        )
    ]


@pytest.mark.parametrize(
    "source",
    [
        '"""Docstring."""\n\n\n\ndef foo():  # pytriage: TR2\n    pass\n',
        '"""Docstring."""\n\ndef foo():\n    pass\n',
        '"""Docstring."""\n\n\n\ndef foo():  # fmt: skip\n    pass\n',
    ],
    ids=["ignore-comment-respected", "no-current-violation", "fmt-skip-respected"],
)
def test_fix_ignores_stale_violation(source: str, tmp_path: Path) -> None:
    test_file = tmp_path / "module.py"
    test_file.write_text(source)
    check = ExcessiveBlankLinesCheck()

    # A caller-supplied violations list can be stale — e.g. an ignore
    # comment was added since, or a previous fix in the same run already
    # collapsed the blank run — so fix() must recheck the current source
    # rather than trusting it.
    stale_violation = ViolationFactory.build(check_id=check.check_id, error_code=check.error_code)

    assert FixOutcome.APPLIED not in check.fix(test_file, [stale_violation], source, ast.parse(source)).outcomes
    assert test_file.read_text() == source


def test_fix_file_content_empty_source_returns_unchanged() -> None:
    assert fix_file_content("", ast.parse("")) == ""


def test_fix_with_no_violations_declines(tmp_path: Path) -> None:
    source = "x = 1\n"
    test_file = tmp_path / "module.py"
    test_file.write_text(source)

    check = ExcessiveBlankLinesCheck()
    assert FixOutcome.APPLIED not in check.fix(test_file, [], source, ast.parse(source)).outcomes


def test_fix_leading_blank_lines_before_first_code_with_no_header(
    tmp_path: Path,
) -> None:
    source = "\n\n\nimport os\n"
    tree = ast.parse(source)
    test_file = tmp_path / "module.py"
    test_file.write_text(source)

    check = ExcessiveBlankLinesCheck()
    violations = check.check(test_file, tree, source)
    assert FixOutcome.APPLIED in check.fix(test_file, violations, source, tree).outcomes
    assert test_file.read_text() == "\nimport os\n"


def test_fix_write_failure_reports_failed_outcome(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    bad_source = (FIXTURES_DIR / "bad" / "header_spacing.py").read_text()

    # Point at a path inside a directory that doesn't exist so write_text()
    # raises OSError.
    test_file = tmp_path / "missing_dir" / "module.py"

    tree = ast.parse(bad_source)
    check = ExcessiveBlankLinesCheck()
    violations = check.check(test_file, tree, bad_source)
    with caplog.at_level("DEBUG"):
        fix_result = check.fix(test_file, violations, bad_source, tree)
    assert FixOutcome.APPLIED not in fix_result.outcomes
    # The write failure must be attributed to the violations it
    # actually affected, not left indistinguishable from "never attempted"
    # — the orchestrator's own report otherwise misleadingly suggests
    # re-running --fix, which would just fail identically again.
    assert fix_result.outcomes == (FixOutcome.FAILED,) * len(violations)
    assert all(record.levelname == "DEBUG" for record in caplog.records)


def test_fix_collapses_header_blank_lines(tmp_path: Path) -> None:
    bad_source = (FIXTURES_DIR / "bad" / "header_spacing.py").read_text()
    good_source = (FIXTURES_DIR / "good" / "header_spacing.py").read_text()

    test_file = tmp_path / "module.py"
    test_file.write_text(bad_source)

    tree = ast.parse(bad_source)
    check = ExcessiveBlankLinesCheck()
    violations = check.check(test_file, tree, bad_source)
    assert FixOutcome.APPLIED in check.fix(test_file, violations, bad_source, tree).outcomes

    assert test_file.read_text() == good_source
