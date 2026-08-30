from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks import ruff_extra_rules
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks._per_file_ignores import PerFileIgnore, PerFileIgnoreList

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.uses_project_config

_FLAGGED = "data = 1\n"
_ARGV = ["--select", "meaningless-vars", "--meaningless-vars-level", "permissive"]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_config(directory: Path, per_file_ignores: str) -> None:
    (directory / "pyproject.toml").write_text(f"[tool.ruff-extra-rules]\nper-file-ignores = {{ {per_file_ignores} }}\n")


def _flagged_file(project: Path, name: str) -> Path:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_FLAGGED)
    return path


def _entry(pattern: str, anchor: Path, *, negated: bool = False) -> PerFileIgnore:
    return PerFileIgnore(pattern=pattern, anchor=anchor, negated=negated, check_ids=frozenset({"a-check"}))


@pytest.mark.parametrize(
    ("pattern", "negated", "relative", "expected"),
    [
        ("__init__.py", False, "src/pkg/__init__.py", True),
        ("__init__.py", False, "src/pkg/mod.py", False),
        ("src/**", False, "src/pkg/mod.py", True),
        ("src/**", False, "tests/t.py", False),
        ("src/**", True, "tests/t.py", True),
        ("src/**", True, "src/pkg/mod.py", False),
        ("__init__.py", True, "src/pkg/mod.py", True),
        ("./tests/**", False, "tests/t.py", True),
        ("tests//**", False, "tests/deep/t.py", True),
        ("tests/../src/**", False, "src/mod.py", True),
        ("tests/../src/**", False, "tests/t.py", False),
        ("./mod.py", False, "mod.py", True),
        ("./mod.py", False, "src/mod.py", False),
    ],
    ids=[
        "basename-matches-at-any-depth",
        "basename-must-match-the-file-name",
        "path-pattern-is-anchored",
        "path-pattern-does-not-match-elsewhere",
        "negated-pattern-applies-to-what-it-misses",
        "negated-pattern-spares-what-it-matches",
        "negation-applies-to-the-basename-matcher-too",
        "a-leading-dot-component-is-resolved",
        "a-repeated-separator-is-resolved",
        "a-parent-component-is-resolved",
        "a-resolved-pattern-stops-matching-what-it-left",
        "a-dot-prefixed-name-anchors-at-the-root",
        "a-dot-prefixed-name-is-not-matched-unanchored",
    ],
)
def test_which_checks_a_file_ignores(
    tmp_path: Path, pattern: str, negated: bool, relative: str, expected: bool
) -> None:
    ignores = PerFileIgnoreList((_entry(pattern, tmp_path, negated=negated),))

    assert ("a-check" in ignores.ignored_check_ids(tmp_path / relative)) is expected


def test_a_path_pattern_does_not_reach_outside_its_anchor(tmp_path: Path) -> None:
    anchor = tmp_path / "project"
    ignores = PerFileIgnoreList((_entry("src/**", anchor),))

    assert ignores.ignored_check_ids(tmp_path / "elsewhere" / "src" / "mod.py") == frozenset()


@pytest.mark.parametrize(
    ("pattern", "negated", "expected"),
    [("mod.py", False, True), ("src/**", True, True), ("src/**", False, False)],
    ids=["a-name-pattern-still-matches-it", "a-negated-pattern-still-applies-to-it", "a-path-pattern-does-not"],
)
def test_a_file_outside_the_anchor_is_matched_on_its_name_alone(
    tmp_path: Path, pattern: str, negated: bool, expected: bool
) -> None:
    anchor = tmp_path / "project"
    ignores = PerFileIgnoreList((_entry(pattern, anchor, negated=negated),))

    assert ("a-check" in ignores.ignored_check_ids(tmp_path / "elsewhere" / "mod.py")) is expected


def test_every_matching_entry_contributes_its_own_checks(tmp_path: Path) -> None:
    ignores = PerFileIgnoreList(
        (
            PerFileIgnore(pattern="src/**", anchor=tmp_path, negated=False, check_ids=frozenset({"a-check"})),
            PerFileIgnore(pattern="*.py", anchor=tmp_path, negated=False, check_ids=frozenset({"b-check"})),
            PerFileIgnore(pattern="tests/**", anchor=tmp_path, negated=False, check_ids=frozenset({"c-check"})),
        )
    )

    assert ignores.ignored_check_ids(tmp_path / "src" / "mod.py") == frozenset({"a-check", "b-check"})


def test_no_entries_ignores_nothing(tmp_path: Path) -> None:
    assert PerFileIgnoreList().ignored_check_ids(tmp_path / "mod.py") == frozenset()


def test_a_configured_pattern_switches_the_check_off_for_matching_files_only(project: Path) -> None:
    _write_config(project, '"tests/**" = ["meaningless-vars"]')
    ignored = _flagged_file(project, "tests/t.py")
    checked = _flagged_file(project, "src/mod.py")

    assert main([str(ignored), *_ARGV]) == 0
    assert main([str(checked), *_ARGV]) == 1


def test_a_configured_pattern_is_anchored_at_the_project_root(project: Path) -> None:
    _write_config(project, '"tests/**" = ["meaningless-vars"]')
    lookalike = _flagged_file(project, "src/tests/t.py")

    assert main([str(lookalike), *_ARGV]) == 1


def test_an_unlisted_check_still_reports_in_an_ignored_file(project: Path) -> None:
    _write_config(project, '"tests/**" = ["misplaced-comment"]')
    ignored = _flagged_file(project, "tests/t.py")

    assert main([str(ignored), *_ARGV]) == 1


def test_the_command_line_replaces_the_configured_patterns(project: Path) -> None:
    _write_config(project, '"tests/**" = ["meaningless-vars"]')
    previously_ignored = _flagged_file(project, "tests/t.py")
    newly_ignored = _flagged_file(project, "src/mod.py")

    argv = ["--per-file-ignores", "src/**:meaningless-vars", *_ARGV]

    assert main([str(previously_ignored), *argv]) == 1
    assert main([str(newly_ignored), *argv]) == 0


def test_a_command_line_pattern_is_anchored_at_the_working_directory(project: Path) -> None:
    nested = project / "services"
    ignored = _flagged_file(project, "services/src/mod.py")

    with pytest.MonkeyPatch.context() as inner:
        inner.chdir(nested)
        assert main([str(ignored), "--per-file-ignores", "src/**:meaningless-vars", *_ARGV]) == 0
        assert main([str(ignored), "--per-file-ignores", "services/**:meaningless-vars", *_ARGV]) == 1


def test_repeating_the_flag_accumulates_patterns(project: Path) -> None:
    first = _flagged_file(project, "tests/t.py")
    second = _flagged_file(project, "src/mod.py")

    argv = [
        "--per-file-ignores",
        "tests/**:meaningless-vars",
        "--per-file-ignores",
        "src/**:meaningless-vars",
        *_ARGV,
    ]

    assert main([str(first), str(second), *argv]) == 0


def test_an_ignored_check_never_fixes_the_file(project: Path) -> None:
    _write_config(project, '"vendor/**" = ["redundant-assignment"]')
    vendored = project / "vendor" / "mod.py"
    vendored.parent.mkdir()
    original = "def f():\n    value = compute()\n    return value\n"
    vendored.write_text(original)

    assert main([str(vendored), "--select", "redundant-assignment", "--fix"]) == 0
    assert vendored.read_text() == original


def test_ignoring_every_check_for_a_file_reports_nothing(project: Path) -> None:
    _write_config(project, '"tests/**" = ["meaningless-vars", "misplaced-comment"]')
    ignored = _flagged_file(project, "tests/t.py")

    assert main([str(ignored), "--select", "meaningless-vars,misplaced-comment"]) == 0


def test_a_pattern_naming_a_check_this_hook_cannot_run_is_accepted(project: Path) -> None:
    _write_config(project, '"tests/**" = ["redundant-type-conversion"]')
    checked = _flagged_file(project, "tests/t.py")

    assert ruff_extra_rules.main([str(checked), *_ARGV]) == 1


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("[tool.ruff-extra-rules]\nper-file-ignores = 3\n", "must be a table"),
        ('[tool.ruff-extra-rules]\nper-file-ignores = { "a.py" = "meaningless-vars" }\n', "expected a list of strings"),
        ('[tool.ruff-extra-rules]\nper-file-ignores = { "a.py" = [1] }\n', "expected a list of strings"),
        ('[tool.ruff-extra-rules]\nper-file-ignores = { "a.py" = ["nope"] }\n', "Unknown check `nope`"),
        ('[tool.ruff-extra-rules]\nper-file-ignores = { "[a.py" = ["meaningless-vars"] }\n', "Invalid file pattern"),
    ],
    ids=["non-table", "non-list-value", "non-string-entry", "unknown-check", "uncompilable-pattern"],
)
def test_an_invalid_configured_table_exits_two_and_names_its_source(
    project: Path, capsys: pytest.CaptureFixture[str], body: str, needle: str
) -> None:
    path = project / "pyproject.toml"
    path.write_text(body)
    filepath = _flagged_file(project, "mod.py")

    assert main([str(filepath)]) == 2

    err = capsys.readouterr().err
    assert needle in err
    assert str(path) in err


def test_the_configured_table_is_validated_even_when_the_command_line_replaces_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An override must not launder an invalid file into an accepted one; see
    # ADR-0045.
    _write_config(project, '"a.py" = ["nope"]')
    filepath = _flagged_file(project, "mod.py")

    assert main([str(filepath), "--per-file-ignores", "b.py:meaningless-vars"]) == 2
    assert "Unknown check `nope`" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("tests/**", "expected `<file pattern>:<check>`"),
        ("tests/**:meaningless-vars:extra", "expected `<file pattern>:<check>`"),
        (":meaningless-vars", "expected `<file pattern>:<check>`"),
        ("tests/**:", "expected `<file pattern>:<check>`"),
        ("tests/**:nope", "Unknown check `nope`"),
        (",,", "expected at least one"),
        ("[a.py:meaningless-vars", "Invalid file pattern"),
    ],
    ids=["no-separator", "too-many-separators", "no-pattern", "no-check", "unknown-check", "empty", "uncompilable"],
)
def test_an_invalid_flag_value_exits_two(
    project: Path, capsys: pytest.CaptureFixture[str], value: str, needle: str
) -> None:
    filepath = _flagged_file(project, "mod.py")

    assert main([str(filepath), "--per-file-ignores", value]) == 2
    assert needle in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [None, "data = (\n"],
    ids=["missing-file", "unparseable-file"],
)
def test_an_unusable_file_is_still_reported_when_every_check_is_ignored(
    project: Path, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    # Switching every check off for a file says nothing about whether the
    # file itself is usable, and an input the user named must never be
    # skipped in silence (behavioral contract chapter 13).
    _write_config(project, '"**" = ["meaningless-vars"]')
    filepath = project / "module.py"
    if content is not None:
        filepath.write_text(content)

    assert main([str(filepath), "--select", "meaningless-vars"]) == 1
    assert "could not be read or parsed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("../{anchor}/src/**", True),
        ("{absolute}/src/**", True),
        ("../{{{anchor},elsewhere}}/src/**", True),
        ("../elsewhere/**", False),
        ("../../../../../../../../../../../../elsewhere/**", False),
    ],
    ids=[
        "a-parent-component-can-lead-back-in",
        "an-absolute-pattern-is-honoured",
        "an-alternative-that-leads-back-in-is-resolved",
        "a-pattern-leading-out-matches-nothing",
        "a-pattern-climbing-past-the-root-matches-nothing",
    ],
)
def test_a_pattern_is_resolved_against_its_anchor(tmp_path: Path, pattern: str, expected: bool) -> None:
    # `ruff` resolves the pattern before matching, so one that walks out and
    # back in still names what it looks like; see ADR-0046.
    anchor = tmp_path / "project"
    spelled = pattern.format(anchor=anchor.name, absolute=anchor)
    ignores = PerFileIgnoreList((_entry(spelled, anchor),))

    assert ("a-check" in ignores.ignored_check_ids(anchor / "src" / "mod.py")) is expected


# Every expectation below was measured against `ruff 0.16.1`. Each pattern
# carries a directory, so the anchored half of the rule decides it -- the
# file's own name never matches these.
@pytest.mark.parametrize(
    ("pattern", "relative", "expected"),
    [
        (r"src/a\{b,c\}d.py", "src/a{b,c}d.py", True),
        ("src/[{]a.py", "src/{a.py", True),
        ("src/{a[,]b,c}.py", "src/a,b.py", True),
        ("src/{a[,]b,c}.py", "src/c.py", True),
        (r"src/{a\,b,c}.py", "src/a,b.py", True),
        ("src/a{b,{c,d}}e.py", "src/ade.py", True),
        ("src/{a,[!x]b}.py", "src/yb.py", True),
        ("src/{a,[]]b}.py", "src/]b.py", True),
        ("src/{a,b}.py", "src/c.py", False),
    ],
    ids=[
        "escaped-braces-are-literal",
        "a-brace-in-a-character-class-is-literal",
        "a-comma-in-a-character-class-does-not-split",
        "a-class-branch-still-alternates",
        "an-escaped-comma-does-not-split",
        "braces-nest",
        "a-negated-class-inside-a-branch",
        "a-closing-bracket-first-inside-a-branch",
        "a-branch-still-has-to-match",
    ],
)
def test_an_anchored_pattern_alternates_the_way_it_translates(
    tmp_path: Path, pattern: str, relative: str, expected: bool
) -> None:
    ignores = PerFileIgnoreList((_entry(pattern, tmp_path),))

    assert ("a-check" in ignores.ignored_check_ids(tmp_path / relative)) is expected


def test_a_glob_metacharacter_in_the_anchor_path_still_matches(tmp_path: Path) -> None:
    anchor = tmp_path / "pro[ject"
    ignores = PerFileIgnoreList((_entry("src/**", anchor),))

    assert "a-check" in ignores.ignored_check_ids(anchor / "src" / "mod.py")


@pytest.mark.parametrize(
    ("source", "relative", "exit_code"),
    [("config", "src/mod.py", 1), ("config", "tests/t.py", 0), ("flag", "src/mod.py", 1), ("flag", "tests/t.py", 0)],
    ids=["configured-spares-src", "configured-covers-the-rest", "flag-spares-src", "flag-covers-the-rest"],
)
def test_a_negated_pattern_reaches_matching_from_either_source(
    project: Path, source: str, relative: str, exit_code: int
) -> None:
    # The `!` is stripped where the pattern is read, so it has to survive both
    # readers rather than only the one a unit test constructs by hand.
    flagged = _flagged_file(project, relative)
    if source == "config":
        _write_config(project, '"!src/**" = ["meaningless-vars"]')
        argv = []
    else:
        argv = ["--per-file-ignores", "!src/**:meaningless-vars"]

    assert main([str(flagged), *argv, *_ARGV]) == exit_code
