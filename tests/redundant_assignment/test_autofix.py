from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks._base import is_fix_failed
from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck
from pre_commit_hooks.ast_checks.redundant_assignment.autofix import (
    _can_safely_inline,
    _cleanup_blank_lines_around_removals,
    apply_fixes,
)
from tests.factories import ViolationFactory

if TYPE_CHECKING:
    from collections.abc import Callable

    from pre_commit_hooks.ast_checks._base import Violation

    WriteSourceFn = Callable[[str], Path]
    CheckedFn = Callable[[str], tuple[Path, ast.Module, list[Violation]]]


@pytest.fixture
def check() -> RedundantAssignmentCheck:
    return RedundantAssignmentCheck()


@pytest.fixture
def write_source(tmp_path: Path) -> WriteSourceFn:
    def _write(source: str) -> Path:
        filepath = tmp_path / "source.py"
        filepath.write_text(source)
        return filepath

    return _write


@pytest.fixture
def checked(write_source: WriteSourceFn, check: RedundantAssignmentCheck) -> CheckedFn:
    def _checked(source: str) -> tuple[Path, ast.Module, list[Violation]]:
        filepath = write_source(source)
        tree = ast.parse(source)
        violations = check.check(filepath, tree, source)
        return filepath, tree, violations

    return _checked


def test_fix_method_with_fixable_violations(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    source = """def func_scope():
    x = "foo"
    func(x=x)
"""
    filepath, tree, violations = checked(source)

    assert len(violations) >= 1
    assert any(v.fixable for v in violations)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "x = " not in fixed_content
    assert 'func(x="foo")' in fixed_content


def test_fix_two_assignments_used_on_the_same_line(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    # Two independently-fixable assignments whose single uses
    # land on the same line must both be inlined, even when the
    # replacement text is a different length than the variable it
    # replaces (which shifts the column of whichever use is processed
    # second).
    source = """def f():
    x = 1
    y = 22
    return y + x
"""
    filepath, tree, violations = checked(source)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "x = " not in fixed_content
    assert "y = " not in fixed_content
    assert "return 22 + 1" in fixed_content


def test_fix_chained_assignment_where_use_line_is_another_assign_line(
    checked: CheckedFn, check: RedundantAssignmentCheck
) -> None:
    # `x`'s only use is on the same line as `y`'s assignment (`y
    # = x`). Applying `y`'s fix first blanks that whole line, so `x`'s own
    # fix must skip cleanly instead of crashing when its use is gone.
    source = """def f():
    x = 1
    y = x
    return y
"""
    filepath, tree, violations = checked(source)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "y = " not in fixed_content
    assert "return x" in fixed_content


def test_fix_write_failure_returns_false(
    tmp_path: Path, check: RedundantAssignmentCheck, caplog: pytest.LogCaptureFixture
) -> None:
    # apply_fixes() must catch atomic_write_text()'s OSError and return
    # False, like every other check's fix(), instead of letting it
    # propagate uncaught.
    source = """def func_scope():
    x = "foo"
    func(x=x)
"""
    # Point at a path inside a directory that doesn't exist so the
    # temp-file-then-rename write raises OSError.
    filepath = tmp_path / "missing_dir" / "source.py"

    tree = ast.parse(source)
    violations = check.check(filepath, tree, source)

    with caplog.at_level("DEBUG"):
        assert check.fix(filepath, violations, source, tree) is False
    # The write failure must be attributed to the violations it
    # actually affected, not left indistinguishable from "never attempted"
    # — the orchestrator's own report otherwise misleadingly suggests
    # re-running --fix, which would just fail identically again.
    assert all(is_fix_failed(v) for v in violations)
    # mark_fix_failed() above already reports this cleanly; a raw traceback
    # on stderr by default would just be redundant noise (ch. 7: "MUST NOT
    # emit uncontrolled human-oriented text into a machine-readable output
    # stream").
    assert all(record.levelname == "DEBUG" for record in caplog.records)


@pytest.mark.parametrize(
    ("source", "fix_data"),
    [
        ("x = 1\nprint(x)\n", None),
        ("x = 1\nprint(x)\n", {"other_key": "value"}),
        (
            "x = 1\nprint(x)\n",
            {
                "pattern": "IMMEDIATE_SINGLE_USE",
                "assign_line": 100,
                "var_name": "x",
                "rhs_source": "1",
                "use_line": 2,
                "use_col": 6,
            },
        ),
        (
            "x = 1\nprint(x)\n",
            {
                "pattern": "IMMEDIATE_SINGLE_USE",
                "assign_line": 1,
                "var_name": "x",
                "rhs_source": "1",
                "use_line": 100,
                "use_col": 6,
            },
        ),
        (
            # RedundantAssignmentCheck.check() leaves use_line/use_col
            # unset whenever a lifecycle doesn't have exactly one use.
            "x = 1\nprint(x)\nprint(x)\n",
            {
                "pattern": "SINGLE_USE",
                "assign_line": 1,
                "var_name": "x",
                "rhs_source": "1",
                "use_line": None,
                "use_col": None,
            },
        ),
        (
            "x = " + "a" * 40 + "\nresult = some_long_function_name(x, param1, param2)\n",
            {
                "pattern": "IMMEDIATE_SINGLE_USE",
                "assign_line": 1,
                "var_name": "x",
                "rhs_source": "a" * 40,
                "use_line": 2,
                "use_col": 33,
            },
        ),
    ],
    ids=[
        "missing-fix-data",
        "fix-data-missing-use-line",
        "invalid-assignment-line",
        "invalid-usage-line",
        "multiple-uses-unset-position",
        "unsafe-line-length",
    ],
)
def test_autofix_declines_fix_for_invalid_or_unsafe_fix_data(
    write_source: WriteSourceFn,
    check: RedundantAssignmentCheck,
    source: str,
    fix_data: dict[str, object] | None,
) -> None:
    violation = ViolationFactory.build(
        check_id="redundant-assignment", error_code="TR5", fixable=True, fix_data=fix_data
    )
    assert check.fix(write_source(source), [violation], source, ast.parse(source)) is False


def test_autofix_skips_multiline_rhs() -> None:
    assert _can_safely_inline("result", "func(\n    arg\n)", 0, ["result = func(x)\n"]) is False


def test_autofix_skips_line_length_violation() -> None:
    source_lines = ["x = " + "a" * 80 + "\n"]
    assert _can_safely_inline("x", "a" * 20, 0, source_lines) is False


@pytest.mark.parametrize("line_index", [-1, 10], ids=["negative", "out-of-bounds"])
def test_autofix_skips_invalid_line_indices(line_index: int) -> None:
    assert _can_safely_inline("x", "value", line_index, ["line1\n", "line2\n"]) is False


def test_fix_method_with_no_fixable_violations() -> None:
    source = """
x = "foo"
func(x=x)
"""
    violation = ViolationFactory.build(check_id="redundant-assignment", error_code="TR5", fixable=False, fix_data=None)
    assert apply_fixes(Path("test.py"), [violation], source) is False


@pytest.mark.parametrize(
    ("source", "removed", "kept"),
    [
        (
            """def f():
    y = 42
    result = y + 10
    return result
""",
            "y = 42",
            "result = 42 + 10",
        ),
        (
            """def func(days_with_routes_in_a_row: int) -> int:
    return days_with_routes_in_a_row


def caller() -> int:
    days_with_routes_in_a_row = 42
    return func(days_with_routes_in_a_row=days_with_routes_in_a_row)
""",
            "days_with_routes_in_a_row = 42",
            "func(days_with_routes_in_a_row=42)",
        ),
        (
            """def f():
    v = obj.attr
    use(v)
""",
            "v = obj.attr",
            "use(obj.attr)",
        ),
        (
            # A non-string RHS (e.g. a number) needs no re-quoting --
            # `f"{5}"` is fine as-is, no quotes involved -- so the f-string
            # splice handling must leave this path unaffected.
            """def f():
    n = 5
    return f"Total: {n}"
""",
            "n = ",
            'return f"Total: {5}"',
        ),
        (
            # A Name RHS used as a whole f-string field (e.g. `x = obj;
            # f"{x}"`) isn't a string-literal expression, so `rhs_source`
            # isn't eligible for splicing -- the splice path must recognize
            # that (via ast.literal_eval raising) and fall through to the
            # ordinary inlining path unchanged.
            """def f(obj):
    x = obj
    return f"value: {x}"
""",
            "x = obj",
            'return f"value: {obj}"',
        ),
        (
            # ast.col_offset is a UTF-8 byte offset, not a character
            # offset. A non-ASCII character earlier on the use's line must
            # not throw off the position used to locate the variable for
            # inlining.
            """def process():
    data = calc()
    return "café", data
""",
            "data",
            'return "café", calc()',
        ),
    ],
    ids=[
        "simple-constant",
        "keyword-argument-echo",
        "simple-attribute",
        "fstring-field-non-string-rhs",
        "fstring-field-name-rhs",
        "non-ascii-text-on-use-line",
    ],
)
def test_autofix_inlines_simple_redundant_assignment(
    checked: CheckedFn, check: RedundantAssignmentCheck, source: str, removed: str, kept: str
) -> None:
    filepath, tree, violations = checked(source)

    assert any(v.fixable for v in violations)
    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert removed not in fixed_content
    assert kept in fixed_content


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            """def f():
    x = 5
    return max(x, 10)
""",
            "return max(5, 10)",
        ),
        (
            """
def func(index):
    x = 5
    return max(x, index)
""",
            "max(5, index)",
        ),
    ],
    ids=["against-max", "against-index"],
)
def test_autofix_respects_word_boundaries(
    checked: CheckedFn, check: RedundantAssignmentCheck, source: str, expected: str
) -> None:
    # `return max(x, 10)` directly (no intermediate `result =`), so `x` is
    # the only redundant assignment in play -- `result = max(x, 10); return
    # result` would make `result` itself independently fixable too (issue
    # #76: a 1-arg call is no longer excluded from autofix), and inlining
    # both in a single pass is a pre-existing cascading-fix quirk (ADR-0009)
    # unrelated to what this test checks. A naive replace must also not
    # corrupt `max`/`index`, which both contain the letter "x".
    filepath, tree, violations = checked(source)

    assert any(v.fixable for v in violations)
    assert check.fix(filepath, violations, source, tree) is True
    assert expected in filepath.read_text()


def test_autofix_respects_line_length(checked: CheckedFn) -> None:
    # `x` and its RHS are both short enough to pass should_report_violation's
    # conservative report-time estimate, but the *actual* usage line (with
    # several other long arguments already on it) would exceed 79 chars once
    # inlined — real line length is checked again at fix time (see
    # `exceeds_line_length_when_inlined`), independent of variable-name
    # length (issue #76 dropped that as a redundant, less accurate proxy).
    source = """
def func():
    x = "hello world"
    print("first", "second", "third", "fourth", "fifth", "sixth", "seventh", x)
"""
    _filepath, _tree, violations = checked(source)

    assert violations
    assert all(not v.fixable for v in violations)


def test_zero_arg_call_immediate_single_use_is_fixable(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    # A zero-arg call has no operands whose evaluation order inlining
    # could disturb, so IMMEDIATE_SINGLE_USE allows it as a narrow
    # carve-out even though it's a Call RHS -- idiomatic test code like
    # `check = MeaninglessVarsCheck(); check.check(...)` must be
    # auto-fixable.
    source = """def test_something():
    check = MeaninglessVarsCheck()
    violations = check.check(Path("test.py"), tree, source)
"""
    filepath, tree, violations = checked(source)

    check_violations = [v for v in violations if "'check'" in v.message]
    assert check_violations
    assert all(v.fixable for v in check_violations)

    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "check = " not in fixed_content
    assert "MeaninglessVarsCheck().check(" in fixed_content


def test_augmented_assignment_use_not_flagged_for_zero_arg_call(
    checked: CheckedFn, check: RedundantAssignmentCheck
) -> None:
    # The zero-arg-call carve-out for IMMEDIATE_SINGLE_USE must not make
    # `x = Box(); x += 1` fixable — inlining would produce invalid syntax
    # (`Box() += 1`).
    source = """def f():
    x = Box()
    x += 1
"""
    filepath, tree, violations = checked(source)

    assert all("'x'" not in v.message for v in violations)

    # Even if something slipped through and marked it fixable, fix() must
    # never corrupt the file.
    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


@pytest.mark.parametrize(
    ("source", "message_filter"),
    [
        (
            # A dict literal's own AST field order (all keys, then all
            # values) doesn't match Python's real per-pair evaluation
            # order, so a naive evaluation-order walk would wrongly call
            # `x` in `{"a": side_effect(), x: 1}` safe — it isn't, since
            # "a": side_effect() runs as a pair before x is reached.
            """def f():
    x = make()
    d = {"a": side_effect(), x: 1}
""",
            "'x'",
        ),
        (
            # Binary/boolean/unary/compare operators can invoke arbitrary
            # user code via dunder overloads (__add__, __eq__, __bool__,
            # ...), so a sibling operator expression must count as a
            # preceding effect too. `x = make(); sink(a + b, x)` must not
            # become `sink(a + b, make())`.
            """def f():
    x = make()
    sink(a + b, x)
""",
            "'x'",
        ),
        (
            # A ternary's body/orelse are each conditional — never both
            # run, never unconditionally. `x = make(); sink(x if flag else
            # 0)` must not become `sink(make() if flag else 0)`.
            """def f():
    x = make()
    sink(x if flag else 0)
""",
            "'x'",
        ),
        (
            # `and`/`or` short-circuit, so a non-first operand may never
            # evaluate. `x = make(); sink(flag and x)` must not become
            # `sink(flag and make())`.
            """def f():
    x = make()
    sink(flag and x)
""",
            "'x'",
        ),
    ],
    ids=[
        "dict-value-after-earlier-pair",
        "after-operator-sibling",
        "ternary-branch",
        "short-circuited-boolop",
    ],
)
def test_zero_arg_call_use_not_fixable(
    checked: CheckedFn, check: RedundantAssignmentCheck, source: str, message_filter: str
) -> None:
    filepath, tree, violations = checked(source)

    matching = [v for v in violations if message_filter in v.message]
    assert matching
    assert all(not v.fixable for v in matching)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


@pytest.mark.parametrize(
    "source",
    [
        # `value = make(); for _ in r: sink(value)` hoists a call result out
        # of a loop it isn't part of — inlining would run make() N times
        # instead of once, so this isn't a redundant assignment at all.
        """def f(r):
    value = make()
    for _ in r:
        sink(value)
""",
        # Same loop-repetition risk with an intervening statement before
        # the loop.
        """def f(r):
    value = make()
    other()
    for _ in r:
        sink(value)
""",
    ],
    ids=["immediate-before-loop", "intervening-statement-before-loop"],
)
def test_single_use_call_in_loop_body_not_reported(checked: CheckedFn, source: str) -> None:
    _filepath, _tree, violations = checked(source)
    assert all("'value'" not in v.message for v in violations)


def test_call_rhs_across_await_in_same_statement_not_reported(checked: CheckedFn) -> None:
    # `await future` precedes `x` in evaluation order within this single
    # statement — inlining would run make() after the await instead of
    # before it, so this isn't a redundant assignment at all.
    source = """async def f(future):
    x = make()
    return sink(await future, x)
"""
    _filepath, _tree, violations = checked(source)
    assert all("'x'" not in v.message for v in violations)


def test_autofix_preserves_blank_lines_across_file(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    # autofix must not delete blank lines across the entire
    # file, only around the removed assignment.
    source = """class FirstClass:
    def method_one(self):
        pass


class SecondClass:
    def method_two(self):
        pass


def function_with_redundant_var():
    x = 42
    return x


def another_function():
    pass


class ThirdClass:
    def method_three(self):
        pass
"""
    filepath, tree, violations = checked(source)

    # This source always yields a fixable violation for `x`.
    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)
    fixed_content = filepath.read_text()

    assert "class FirstClass:\n    def method_one(self):\n        pass\n\n\nclass SecondClass:" in fixed_content
    assert (
        "class SecondClass:\n    def method_two(self):\n        pass\n\n\ndef function_with_redundant_var():"
        in fixed_content
    )
    assert "def another_function():\n    pass\n\n\nclass ThirdClass:" in fixed_content

    # Verify the fixed code is still valid Python; raises on failure.
    ast.parse(fixed_content)


def test_autofix_cleans_up_excessive_blank_lines(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    source = """def function_with_redundant():


    x = 42


    return x
"""
    filepath, tree, violations = checked(source)

    # This source always yields a fixable violation for `x`.
    assert any(v.fixable for v in violations)
    check.fix(filepath, violations, source, tree)
    fixed_content = filepath.read_text()

    lines = fixed_content.split("\n")
    def_index = next(i for i, line in enumerate(lines) if "def function_with_redundant" in line)
    return_index = next(i for i in range(def_index, len(lines)) if "return" in lines[i])
    blanks_before_return = 0
    j = return_index - 1
    while j >= 0 and lines[j].strip() == "":
        blanks_before_return += 1
        j -= 1

    assert blanks_before_return <= 2

    # Verify the fixed code is still valid Python; raises on failure.
    ast.parse(fixed_content)


def test_cleanup_blank_lines_only_excess_below() -> None:
    # Branch coverage: blank_above <= 1 but blank_below > 1 (total >= 3).
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {0})
    assert lines[2] == ""
    assert lines[3] == "code\n"


def test_cleanup_blank_lines_only_excess_above() -> None:
    # Branch coverage: blank_above > 1 but blank_below <= 1 (total >= 3).
    lines = ["", "", "", "code\n"]
    _cleanup_blank_lines_around_removals(lines, {2})
    assert lines[0] == ""
    assert lines[3] == "code\n"


def test_fix_preserves_trailing_comment_on_string_ending_in_escaped_backslash(checked: CheckedFn) -> None:
    # A naive comment-detection heuristic would miss the
    # trailing comment on a line like `sep = "\\"  # comment` (an escaped
    # backslash right before the closing quote), so should_report_violation
    # must still apply its "skip if there's an inline comment" rule rather
    # than letting --fix silently delete the comment along with the
    # assignment it decorated (ch. 2: "MUST preserve comments unless the
    # rule explicitly owns the relevant comment"; ch. 21: "MUST preserve
    # comments where possible").
    source = 'def get_sep() -> str:\n    sep = "\\\\"  # Windows path separator\n    return sep\n'
    filepath, _tree, violations = checked(source)

    assert violations == []
    assert filepath.read_text() == source


def test_check_reports_assignment_after_multiline_string_with_trailing_comment(checked: CheckedFn) -> None:
    # tokenize reports a multiline STRING token's line as only
    # its start line, not every line it spans, so a comment trailing the
    # closing `"""` on a later line was misclassified as comment-only —
    # wrongly making has_comment_above() true for the *next* line and
    # suppressing an otherwise-legitimate redundant-assignment report
    # (ch. 2/21: comment detection must reflect the actual parsed source,
    # not an under-counted token span).
    source = 'def f():\n    x = """\nmulti\nline\n"""  # trailing comment\n    y = 5\n    return x, y\n'
    _filepath, _tree, violations = checked(source)

    assert any("'y'" in v.message for v in violations)


def test_autofix_splices_string_literal_into_fstring_field(checked: CheckedFn, check: RedundantAssignmentCheck) -> None:
    # Inlining a string-literal variable into an f-string replacement
    # field must splice the literal's raw text directly into the
    # surrounding string, not re-quote it inside the braces (which would
    # produce `f"...{"requests-cache"}..."`).
    source = """def f():
    org = "requests-cache"
    return f"https://github.com/{org}/requests-cache"
"""
    filepath, tree, violations = checked(source)

    assert any(v.fixable for v in violations)
    assert check.fix(filepath, violations, source, tree) is True

    fixed_content = filepath.read_text()
    assert "org = " not in fixed_content
    assert 'return f"https://github.com/requests-cache/requests-cache"' in fixed_content

    # The fixed source must still be valid, unquoted-inside-braces Python.
    ast.parse(fixed_content)


@pytest.mark.parametrize(
    ("source", "message_filter"),
    [
        (
            # A literal containing a quote character can't be safely
            # spliced as raw text without knowing (and re-escaping for) the
            # f-string's own quote style — declined conservatively rather
            # than risking broken output.
            """def f():
    name = "O'Brien"
    return f"Hello {name}!"
""",
            "'name'",
        ),
        (
            # "\\x1b[0m" (an ANSI reset code) is a valid, non-newline,
            # non-NUL string literal, so it passed every prior unsafe-
            # character check. Splicing it as a raw byte is syntactically
            # fine but renders invisibly, making a diff look like the value
            # was silently dropped instead of inlined.
            """def f():
    reset = "\\x1b[0m"
    return f"{reset}"
""",
            "'reset'",
        ),
        (
            # "\\x00" is a perfectly valid string literal, but Python's
            # tokenizer rejects any *source file* containing a raw NUL
            # byte — splicing it as literal text would turn a fixable file
            # into an unparsable one.
            'def f():\n    label = "\\x00"\n    return f"<{label}>"\n',
            "'label'",
        ),
        (
            # a str object can legally hold an unpaired surrogate (e.g.
            # from a "\\ud800" escape) even though no real text encoding
            # can represent one — splicing it as raw source text would
            # make atomic_write_text's compile()/write() crash with an
            # uncaught UnicodeEncodeError instead of declining the fix.
            'def f():\n    label = "\\ud800"\n    return f"<{label}>"\n',
            "'label'",
        ),
        (
            # `{org!r}` applies repr() to the inlined literal, which is not
            # the same as splicing its raw text into the surrounding
            # string — must be declined rather than naively re-quoted.
            """def f():
    org = "requests-cache"
    return f"{org!r}"
""",
            "'org'",
        ),
        (
            # `org` isn't the whole replacement field here
            # (`org.upper()` is), so there's no clean way to remove the
            # braces and splice raw text without changing what the field
            # expression does.
            """def f():
    org = "requests-cache"
    return f"{org.upper()}"
""",
            "'org'",
        ),
    ],
    ids=[
        "unsafe-quote-character",
        "control-character",
        "nul-byte",
        "unpaired-surrogate",
        "conversion",
        "nested-expression",
    ],
)
def test_autofix_declines_fstring_splice(
    checked: CheckedFn, check: RedundantAssignmentCheck, source: str, message_filter: str
) -> None:
    filepath, tree, violations = checked(source)

    matching = [v for v in violations if message_filter in v.message]
    assert matching
    assert all(not v.fixable for v in matching)

    check.fix(filepath, violations, source, tree)
    assert filepath.read_text() == source


def test_autofix_declines_fstring_splice_when_value_unencodable_in_declared_encoding(
    tmp_path: Path, check: RedundantAssignmentCheck
) -> None:
    # A file's own declared encoding (detected upstream via a PEP 263
    # coding line, and passed in here as encoding="ascii" to isolate this
    # check from that detection step) can be narrower than UTF-8. "\xe9"
    # ('é') is a perfectly safe splice target under UTF-8 (should_autofix's
    # check()-time guess, which never learns the real declared encoding),
    # but writing it back into an ASCII-declared file would raise
    # UnicodeEncodeError — apply_fixes must re-check against the *real*
    # encoding it was actually given and decline rather than crash.
    source = 'def f():\n    label = "\\xe9"\n    return f"<{label}>"\n'
    filepath = tmp_path / "source.py"
    filepath.write_bytes(source.encode("ascii"))

    tree = ast.parse(source)
    violations = check.check(filepath, tree, source)

    label_violations = [v for v in violations if "'label'" in v.message]
    assert label_violations
    # should_autofix's UTF-8 guess still marks it [FIXABLE] — it has no way
    # to know the real encoding at check() time.
    assert all(v.fixable for v in label_violations)

    check.fix(filepath, violations, source, tree, encoding="ascii")
    assert filepath.read_bytes() == source.encode("ascii")


def test_autofix_fstring_splice_declines_when_earlier_fix_lengthens_line(
    checked: CheckedFn, check: RedundantAssignmentCheck
) -> None:
    # same-line violations are applied rightmost-first, so a
    # fix processed before this one can lengthen the line beyond what
    # should_autofix saw at check() time (see exceeds_line_length_when_inlined's
    # docstring on why both call sites must independently re-check the
    # *current* line). The f-string splice must do the same and decline —
    # leaving both the assignment and the f-string field untouched — rather
    # than emit an over-long line.
    source = 'def f():\n    o = "cccc"\n    p = "xxxx"\n    return "' + "a" * 48 + '" + f"{o}" + p\n'
    filepath, tree, violations = checked(source)

    o_violations = [v for v in violations if "'o'" in v.message]
    p_violations = [v for v in violations if "'p'" in v.message]
    assert o_violations
    assert all(v.fixable for v in o_violations)
    assert p_violations
    assert all(v.fixable for v in p_violations)

    check.fix(filepath, violations, source, tree)

    fixed_content = filepath.read_text()
    # p's fix (rightmost) is applied first and lengthens the line enough
    # that o's own splice — safe against the original, unmodified line —
    # would now exceed the limit, so it's declined.
    assert '    o = "cccc"' in fixed_content
    assert "{o}" in fixed_content
    assert 'p = "xxxx"' not in fixed_content
