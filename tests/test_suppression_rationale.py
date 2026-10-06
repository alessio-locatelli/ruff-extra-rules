from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._base import FixOutcome, SuppressionUsage
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks._orchestrator import CheckOrchestrator, load_checks
from pre_commit_hooks.ast_checks.suppression_rationale import SuppressionRationaleCheck


@pytest.mark.parametrize(
    "source",
    [
        "import foo  # noqa\n",
        "import foo  # noqa: F401\n",
        "# noqa\n",
        "# noqa: F401, F841\n",
        "# noqa: F401 F841\n",
        "# noqa: F401,\n",
        "# noqa: F401, #\n",
        "# noqa: F401, --\n",
        "#NOQA:F401\n",
        "# NoQa : F401\n",
        "#\tnoqa\t:\tF401\n",
        "#### noqa: F401\n",
        "# ruff: noqa\n",
        "#ruff:noqa:F401\n",
        "# ruff : NoQa : F401\n",
        "# flake8: noqa\n",
        "#flake8 : NOQA: F401, F841\n",
        "# ruff: ignore[F401]\n",
        "import foo  #ruff :ignore [ F401 , F841, ]\n",
        "# ruff: ignore[unused-import]\n",
        "# ruff: file-ignore[F401, unused-import]\n",
        "# pytriage: TR1\n",
        "#PYTRIAGE:tr1, TR2\n",
        "# isort: skip\n",
        "# isort:skip_file\n",
        "# ruff: isort: skip_file\n",
        "import foo  # ruff: isort:skip\n",
        "import foo  #isort:skip\n",
        "#ruff:isort:skip_file\n",
        "# ruff : isort:skip\n",
    ],
    ids=[
        "inline-blanket",
        "inline-coded",
        "own-line-blanket",
        "comma-codes",
        "space-codes",
        "trailing-comma",
        "trailing-comma-empty-separator",
        "trailing-comma-empty-dashes",
        "uppercase",
        "mixed-case",
        "tabs",
        "multiple-hashes",
        "file-blanket",
        "file-compact",
        "file-spacing-case",
        "flake8-blanket",
        "flake8-coded",
        "native-own-line",
        "native-spacing-trailing-comma",
        "native-rule-name",
        "native-file",
        "pytriage",
        "pytriage-case-list",
        "isort-skip",
        "isort-file",
        "ruff-isort-file",
        "ruff-isort-skip",
        "compact-isort",
        "compact-ruff-isort",
        "isort-prefix-spacing",
    ],
)
def test_supported_suppressions_require_a_rationale(source: str) -> None:
    violations = SuppressionRationaleCheck().check(Path("example.py"), ast.parse(source), source)

    assert len(violations) == 1
    violation = violations[0]
    assert violation.check_id == "suppression-rationale"
    assert violation.error_code == "TR12"
    assert violation.line == 1
    assert violation.col == source.index("#")
    assert not violation.fixable
    assert "explanation" in violation.message


@pytest.mark.parametrize(
    "directive",
    [
        "# noqa",
        "# noqa: F401",
        "# ruff: noqa: F401",
        "# flake8: noqa",
        "# ruff: ignore[F401]",
        "# ruff: file-ignore[F401]",
        "# pytriage: TR1",
        "# isort: skip",
    ],
    ids=["blanket", "coded", "file-coded", "flake8", "native", "native-file", "pytriage", "isort"],
)
@pytest.mark.parametrize(
    ("trailing", "expected_count"),
    [
        (" imported for side effects", 0),
        (" # explanation", 0),
        (" -- explanation", 0),
        (" #", 1),
        (" --", 1),
        (" # pylint: disable=unused-import", 0),
        (" x", 0),
        (" -- #", 0),
    ],
    ids=["plain", "hash", "dashes", "empty-hash", "empty-dashes", "other-pragma", "short", "ambiguous-separators"],
)
def test_inline_rationales(directive: str, trailing: str, expected_count: int) -> None:
    source = f"{directive}{trailing}\n"

    assert len(SuppressionRationaleCheck().check(Path("example.py"), ast.parse(source), source)) == expected_count


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        ("# Register the plugin.\nimport foo  # noqa: F401\n", ()),
        ("# Register the plugin.\n# Importing performs registration.\nimport foo  # noqa: F401\n", ()),
        ("# Today's weather.\nimport foo  # noqa: F401\n", ()),
        ("# Register the plugin.\n\nimport foo  # noqa: F401\n", (3,)),
        ("# Explain the module.\n# ruff: file-ignore[F401]\n", ()),
        ("# Explain the module.\n\n# ruff: file-ignore[F401]\n", (3,)),
        ("# Explain the module.\n#\n# noqa\n", ()),
        ("#\n# noqa\n", (2,)),
        ("# noqa: F401 #\n# noqa\n", (1, 2)),
        ("value = 1  # unrelated trailing comment\nimport foo  # noqa: F401\n", (2,)),
        ("text = '''\n# Explanation in a string\n'''  # noqa\n", (3,)),
        ("def run():\n    # Different topic.\n    import foo  # noqa: F401\n", ()),
        ('name = "é"; import foo  # noqa: F401\n', (1,)),
        ("# Explain.\r\nimport foo  # noqa\r\n", ()),
        ("# Explain.\r\rimport foo  # noqa\r", (3,)),
    ],
    ids=[
        "preceding",
        "block",
        "unrelated",
        "blank-line",
        "own-line",
        "own-line-blank",
        "block-empty-end",
        "empty-block",
        "preceding-directive",
        "preceding-inline",
        "string",
        "indentation",
        "unicode",
        "crlf",
        "cr",
    ],
)
def test_preceding_rationale_association(source: str, expected_lines: tuple[int, ...]) -> None:
    violations = SuppressionRationaleCheck().check(Path("example.py"), ast.parse(source), source)

    assert tuple(violation.line for violation in violations) == expected_lines


@pytest.mark.parametrize(
    "preceding",
    [
        "#NOQA:F401",
        "#### noqa: F401",
        "# ruff: ignore[F401]",
        "# pytriage: TR1",
        "# type: ignore",
        "# pylint: disable=unused-import",
        "# NOSONAR",
    ],
    ids=["legacy-case", "legacy-hashes", "native", "pytriage", "type-ignore", "pylint", "sonar"],
)
def test_pragma_only_blocks_do_not_explain_a_suppression(preceding: str) -> None:
    source = f"{preceding}\n# noqa\n"
    violations = SuppressionRationaleCheck().check(Path("example.py"), ast.parse(source), source)

    assert 2 in {violation.line for violation in violations}


@pytest.mark.parametrize(
    "preceding",
    ["# Imported for registration.\n# ruff: ignore[F401]", "# This mentions # noqa without being a pragma."],
    ids=["prose-in-block", "prose-mentions-pragma"],
)
def test_prose_in_a_comment_block_still_counts(preceding: str) -> None:
    source = f"{preceding}\n# noqa\n"

    assert SuppressionRationaleCheck().check(Path("example.py"), ast.parse(source), source) == []


@pytest.mark.parametrize(
    "source",
    [
        'text = "# noqa: F401"\n',
        "# This mentions noqa without a directive.\n",
        "# type: ignore\n# NOSONAR\n# pylint: disable=all\n# mypy: ignore-errors\n# pyright: ignore\n# nosec\n",
        "# noqaF401\n",
        "# noqa:\n",
        "# noqa: lower\n",
        "# noqa: F401suffix\n",
        "# noqa: F401F841\n",
        "# noqa: F401,,F841\n",
        "# ruff: noqa: F401F841\n",
        "# Ruff: noqa\n",
        "# FLAKE8: NOQA\n",
        "# ruff: IGNORE[F401]\n",
        "# ruff: ignore\n",
        "# ruff: ignore[]\n",
        "# ruff: ignore[F401\n",
        "# ruff: ignore[F401 F841]\n",
        "# ruff: ignore[123]\n",
        "# ruff: ignore[F401,,F841]\n",
        "# pytriage:\n",
        "# pytriage: TR1,\n",
        "# pytriage: TR1, nonsense\n",
        "# Leading text # noqa\n",
        "# noqa  # noqa: F401\n",
        "# ruff: ignore[F401] # noqa\n",
        "# noqa: F401 # pytriage: TR12\n",
        "import foo  # ruff: noqa\n",
        "import foo  # ruff: file-ignore[F401]\n",
        "def run():\n    # ruff: file-ignore[F401]\n    pass\n",
        "# isort: on\n",
        "# isort: split\n",
        "# fmt: off\nvalue = 1  # noqa\n# fmt: on\n",
    ],
    ids=[
        "literal",
        "ordinary-comment",
        "third-party",
        "invalid-blanket",
        "missing-noqa-code",
        "lower-code",
        "code-suffix",
        "joined-codes",
        "missing-code",
        "file-joined",
        "ruff-case",
        "flake8-case",
        "action-case",
        "native-no-brackets",
        "native-empty",
        "native-unclosed",
        "native-no-comma",
        "native-nonword",
        "native-empty-code",
        "pytriage-empty",
        "pytriage-trailing-comma",
        "pytriage-partial",
        "nested-leading",
        "multiple-noqa",
        "multiple-families",
        "pragma-tail",
        "inline-file-noqa",
        "inline-file-ignore",
        "indented-file-ignore",
        "isort-on",
        "isort-split",
        "format-suppressed",
    ],
)
def test_unsupported_malformed_and_ambiguous_comments_are_accepted(source: str) -> None:
    assert not SuppressionRationaleCheck().check(Path("example.py"), ast.parse(source), source)


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        ("# ruff: disable[F401]\nimport foo\n# ruff: enable[F401]\n", (1,)),
        ("# ruff : disable [ F401, F841, ]\nimport foo\n# ruff: enable[F401,F841]\n", (1,)),
        ("# ruff: disable[unused-import]\nimport foo\n# ruff: enable[unused-import]\n", (1,)),
        ("# ruff: disable[F401] side effects\nimport foo\n# ruff: enable[F401]\n", ()),
        ("# Side effects.\n# ruff: disable[F401]\nimport foo\n# ruff: enable[F401]\n", ()),
        ("# ruff: disable[F401] #\nimport foo\n# ruff: enable[F401]\n", (1,)),
        ("# ruff: disable[F401]\nimport foo  # noqa: F401 side effects\n# ruff: enable[F401]\n", (1,)),
        ("# ruff: disable[F401]\nimport foo\n", ()),
        ("# ruff: enable[F401]\n", ()),
        ("# ruff: disable[F401]\nimport foo\n# ruff: enable[F841]\n", ()),
        ("# ruff: disable[F401,F841]\nimport foo\n# ruff: enable[F841,F401]\n", ()),
        ("def run():\n    # ruff: disable[F401]\n    import foo\n    # ruff: enable[F401]\n", (2,)),
        ("def run():\n    # ruff: disable[F401]\n    import foo\n# ruff: enable[F401]\n", ()),
        (
            (
                "def first():\n    # ruff: disable[F401]\n    import foo\n\n"
                "def second():\n    # ruff: enable[F401]\n    pass\n"
            ),
            (),
        ),
        ("def run():\n  # ruff: disable[F401]\n    import foo\n  # ruff: enable[F401]\n", ()),
        ("import foo  # ruff: disable[F401]\n# ruff: enable[F401]\n", ()),
        ("# ruff: disable[F401]\n# ruff: disable[F841]\nimport foo\n# ruff: enable[F841]\n# ruff: enable[F401]\n", ()),
        ("# ruff: disable[F401]\n# ruff: disable[\nimport foo\n# ruff: enable[F401]\n", ()),
        ("items = [\n    # ruff: disable[F401]\n    foo,\n    # ruff: enable[F401]\n]\n", ()),
        ("# isort: off\nimport foo\n# isort: on\n", (1,)),
        ("# Explanation.\n# ruff: isort: off\nimport foo\n# ruff: isort: on\n", ()),
        ("# isort: off\nimport foo\n", ()),
    ],
    ids=[
        "matched",
        "spacing",
        "rule-names",
        "inline",
        "preceding",
        "empty-separator",
        "inline-suppression-within-range",
        "unmatched-disable",
        "unmatched-enable",
        "mismatched-codes",
        "reordered-codes",
        "suite",
        "indent-mismatch",
        "separate-suites",
        "unusual-indent",
        "trailing-range",
        "nested",
        "malformed-inner",
        "expression-range",
        "isort",
        "ruff-isort",
        "unmatched-isort",
    ],
)
def test_range_rationale_policy(source: str, expected_lines: tuple[int, ...]) -> None:
    violations = SuppressionRationaleCheck().check(Path("example.py"), ast.parse(source), source)

    assert tuple(violation.line for violation in violations) == expected_lines


@pytest.mark.parametrize("codes", ["TR12", "TR1,TR12", "tr12"], ids=["self", "list", "case"])
def test_pytriage_self_suppression_tracks_usage(codes: str) -> None:
    source = f"value = 1  # pytriage: {codes}\n"
    checked = SuppressionRationaleCheck().check(Path("example.py"), ast.parse(source), source)

    assert not checked
    assert checked.suppression_usages == (SuppressionUsage("suppression-rationale", "TR12", 1),)


@pytest.mark.parametrize("explanation", ["", " needed"], ids=["used", "unused"])
def test_unused_pytriage_and_cached_usage(tmp_path: Path, explanation: str) -> None:
    filepath = tmp_path / "example.py"
    filepath.write_text(f"value = 1  # pytriage: TR12{explanation}\n")
    orchestrator = CheckOrchestrator(
        load_checks(select={"suppression-rationale", "unused-pytriage"}),
        cache_dir=tmp_path / "cache",
    )
    filename = str(filepath)

    for _ in range(2):
        reported = orchestrator.process_files([filename]).get(filename, ())
        assert tuple(violation.error_code for violation in reported) == (("TR8",) if explanation else ())


def test_rule_is_opt_in_and_has_no_configuration() -> None:
    assert "suppression-rationale" not in {check.check_id for check in load_checks()}
    checks = load_checks(extend_select={"suppression-rationale"})
    assert checks[-1].check_id == "suppression-rationale"
    assert checks[-1].OPTIONS == ()
    assert checks[-1].get_prefilter_pattern() == ["#"]


def test_fix_mode_reports_without_changing_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = tmp_path / "example.py"
    source = "import foo  # NOQA: F401\n"
    filepath.write_text(source)

    assert main(["--select=suppression-rationale", "--fix", str(filepath)]) == 1
    assert "TR12" in capsys.readouterr().err
    assert filepath.read_text() == source
    check = SuppressionRationaleCheck()
    tree = ast.parse(source)
    violations = check.check(filepath, tree, source)
    assert check.fix(filepath, violations, source, tree).outcomes == (FixOutcome.DECLINED,)
