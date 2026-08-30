from __future__ import annotations

import ast
import stat
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks.ast_checks._base import (
    ConcurrentModificationError,
    FixOutcome,
    FixResult,
    FixValidationError,
    PytriageComment,
    SuppressionUsage,
    atomic_write_text,
    byte_col_to_char_col,
    classify_comment_lines,
    fast_get_source_segment,
    find_ignored_lines,
    find_ignored_lines_and_classify_comments,
    find_ignored_lines_and_pytriage_comments,
    find_suppression_usage,
    ignore_pattern_for,
    ignored_lines_from_tokens,
    line_terminator,
    normalize_for_tokenize,
    record_suppression_usage_if_ignored,
    split_lines_like_ast,
    tokenize_source,
)
from tests.factories import ViolationFactory

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ("    data = calc()", b"calc"),
        ('    x = "café"; return data', b"data"),
        ("    x = '😀😀'; data = 1", b"data"),
    ],
    ids=["ascii", "two-byte-char", "four-byte-char"],
)
def test_byte_col_to_char_col(line: str, needle: bytes) -> None:
    byte_offset = line.encode("utf-8").index(needle)
    char_offset = line.index(needle.decode())
    assert byte_col_to_char_col(line, byte_offset) == char_offset


def test_fix_result_keeps_a_distinct_outcome_for_each_violation() -> None:
    fix_result = FixResult((FixOutcome.APPLIED, FixOutcome.DECLINED, FixOutcome.FAILED))

    assert fix_result.outcomes == (FixOutcome.APPLIED, FixOutcome.DECLINED, FixOutcome.FAILED)


@pytest.mark.parametrize(
    "source",
    [
        "x = compute(1, 2)\n",
        "café = compute(x)  # café\n",
        "x = (\n    1 +\n    2\n)\n",
        "x = [1, 2, 3][0]\n",
        "x = 1",
        'x = requests.get("\x0curl", timeout=1)\n',
    ],
    ids=[
        "single-line",
        "unicode-before-node",
        "multiline-parenthesized",
        "single-line-subscript",
        "no-trailing-newline",
        "form-feed-inside-single-line-node",
    ],
)
def test_fast_get_source_segment_matches_ast_get_source_segment(source: str) -> None:
    tree = ast.parse(source)
    assign = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign))

    fast_result = fast_get_source_segment(source, split_lines_like_ast(source), assign.value)

    assert fast_result == ast.get_source_segment(source, assign.value)


def test_fast_get_source_segment_returns_none_without_end_position() -> None:
    source = "x = 1\n"
    tree = ast.parse(source)
    assign = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign))
    assign.value.end_lineno = None

    assert fast_get_source_segment(source, split_lines_like_ast(source), assign.value) is None


@pytest.mark.parametrize(
    "source",
    [
        "x = 1\ny = 2\n",
        "x = 1\r\ny = 2\r\n",
        "x = 1\ry = 2\r",
        "x = 1",
        'x = "a\x0cb"\ny = 2\n',
        "",
    ],
    ids=["lf", "crlf", "cr", "no-trailing-newline", "form-feed-is-not-a-boundary", "empty"],
)
def test_split_lines_like_ast_matches_ast_own_line_numbering(source: str) -> None:
    assert split_lines_like_ast(source) == ast._splitlines_no_ff(source)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("x = 1\r\n", "\r\n"),
        ("x = 1\n", "\n"),
        ("x = 1\r", "\r"),
        ("x = 1", ""),
        ("", ""),
    ],
    ids=["crlf", "lf", "cr", "no-newline", "empty"],
)
def test_line_terminator(line: str, expected: str) -> None:
    assert line_terminator(line) == expected


@pytest.mark.parametrize(
    ("source", "comment_only", "trailing"),
    [
        ("x = 1\nprint(x)\n", set(), set()),
        ("# standalone\nx = 1\n", {1}, set()),
        ("x = 1  # trailing\n", set(), {1}),
        ("    # indented standalone\nx = 1\n", {1}, set()),
        ("x = 1  # trailing\n# standalone\ny = 2\n", {2}, {1}),
    ],
    ids=["no-comments", "comment-only", "trailing-comment", "indented-comment-only", "both-kinds"],
)
def test_classify_comment_lines(source: str, comment_only: set[int], trailing: set[int]) -> None:
    assert classify_comment_lines(source) == (comment_only, trailing)


def test_classify_comment_lines_multiline_string_closing_line_is_code() -> None:
    source = 'x = """\nmulti\nline\n"""  # trailing comment\ny = 5\n'
    comment_only, trailing = classify_comment_lines(source)
    assert trailing == {4}
    assert comment_only == set()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("x = 1\rprint(x)\r", "x = 1\nprint(x)\n"),
        ("x = 1\r\nprint(x)\r\n", "x = 1\r\nprint(x)\r\n"),
        ("x = 1\nprint(x)\n", "x = 1\nprint(x)\n"),
        ("x = 1\r\nprint(x)\r", "x = 1\r\nprint(x)\n"),
    ],
    ids=["lone-cr", "crlf-unchanged", "lf-unchanged", "mixed-crlf-then-lone-cr"],
)
def test_normalize_for_tokenize(source: str, expected: str) -> None:
    assert normalize_for_tokenize(source) == expected


def test_classify_comment_lines_on_cr_only_source() -> None:
    source = 'x = 1\rsep = "\\\\"  # trailing comment\rprint(x, sep)\r'
    _comment_only, trailing = classify_comment_lines(source)
    assert trailing == {2}


def test_find_ignored_lines_on_cr_only_source() -> None:
    source = "x = 1\rdata = 2  # pytriage: TR1\r"
    assert find_ignored_lines(source, ignore_pattern_for("TR1")) == {2}


@pytest.mark.parametrize(
    ("comment", "code", "expected"),
    [
        ("# pytriage: TR1", "TR1", True),
        ("# pytriage: tr1", "TR1", True),
        ("# pytriage: TR1,TR5", "TR1", True),
        ("# pytriage: TR1,TR5", "TR5", True),
        ("# pytriage: TR7,TR1,TR12", "TR1", True),
        ("# pytriage: TR1, TR5", "TR5", True),
        ("# pytriage: TR10", "TR1", False),
        ("# pytriage: TR10", "TR10", True),
        ("# pytriage: TR10,TR5", "TR1", False),
        ("# some unrelated comment mentioning TR1", "TR1", False),
    ],
    ids=[
        "single-code",
        "case-insensitive",
        "list-first-entry",
        "list-second-entry",
        "list-middle-entry",
        "list-space-after-comma",
        "no-false-match-on-longer-code",
        "exact-match-on-longer-code",
        "no-false-match-with-list-prefix",
        "no-match-without-pytriage-prefix",
    ],
)
def test_ignore_pattern_for_comma_separated_codes(comment: str, code: str, expected: bool) -> None:
    assert bool(find_ignored_lines(f"x = 1  {comment}\n", ignore_pattern_for(code))) is expected


def test_pytriage_comment_parser_ignores_an_empty_comment() -> None:
    ignored, format_suppressed, comments = find_ignored_lines_and_pytriage_comments("x = 1  # pytriage:\n")

    assert ignored == set()
    assert format_suppressed == set()
    assert comments == ()


def test_find_suppression_usage_excludes_format_suppressed_comments() -> None:
    comment = PytriageComment(line=2, col=0, codes=("TR1",))

    assert find_suppression_usage((comment,), {2}, "meaningless-vars", "TR1", (2,)) is None


def test_record_suppression_usage_skips_a_nonmatching_comment() -> None:
    suppression_usages: list[SuppressionUsage] = []
    comment = PytriageComment(line=2, col=0, codes=("TR1",))

    assert record_suppression_usage_if_ignored(
        suppression_usages,
        (comment,),
        ignored_lines={2},
        format_suppressed=set(),
        check_id="meaningless-vars",
        error_code="TR2",
        candidate_lines=(2,),
    )
    assert suppression_usages == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "x = 1\n# fmt: off\nnot_formatted=3\nalso_not_formatted=4\n# fmt: on\ny = 2\n",
            {2, 3, 4, 5},
        ),
        (
            "x = 1\n# fmt: off\na=1\n# yapf: enable\ny = 2\n",
            {2, 3, 4},
        ),
        (
            "x = 1\n# yapf: disable\na=1\n# fmt: on\ny = 2\n",
            {2, 3, 4},
        ),
        (
            "x = 1\n# fmt: off\na=1\nb=2\n",
            {2, 3, 4},
        ),
        (
            "x = 1\n# fmt: off\n# just a comment\na=1\n# fmt: on\ny = 2\n",
            {2, 3, 4, 5},
        ),
        (
            "data = [\n    # fmt: off\n    '1',\n    # fmt: on\n    '2',\n]\n",
            set(),
        ),
        (
            "a = [1,2,3,4,5] # fmt: skip\nb = 2\n",
            {1},
        ),
        (
            "x=1;x=2;x=3 # fmt: skip\ny = 2\n",
            {1},
        ),
        (
            "@Test\n@Test2(a,b) # fmt: skip\ndef test(): ...\nz = 1\n",
            {2},
        ),
        (
            "def test(a,b,c,d,e,f) -> int: # fmt: skip\n    pass\nz = 1\n",
            {1},
        ),
        (
            "match point:\n    case Point(0, 0): # fmt: skip\n        pass\nz = 1\n",
            {2},
        ),
        (
            "a = call(\n    [\n        '1',\n        '2',\n    ],\n    b\n)  # fmt: skip\nz = 1\n",
            {1, 2, 3, 4, 5, 6, 7},
        ),
        (
            "a = [1,2,3,4,5]  # fmt: skip  # noqa: E501\nb = 2\n",
            {1},
        ),
        (
            "a = [1,2,3,4,5]  # noqa: E501  # fmt: skip\nb = 2\n",
            {1},
        ),
        (
            "a = 1  # fmt: skipper\nb = 2\n",
            set(),
        ),
        (
            "a = call(\n    [\n        '1',  # fmt: skip\n        '2',\n    ],\n    b\n)\nz = 1\n",
            {1, 2, 3, 4, 5, 6, 7},
        ),
        (
            "@Test\n# fmt: off\n@Test2(a,b)\n# fmt: on\ndef test(): ...\nz = 1\n",
            {2, 3, 4},
        ),
        (
            "# fmt: off\ndef f():\n    x = 1\n",
            {1, 2, 3},
        ),
    ],
    ids=[
        "fmt-off-on-block",
        "fmt-off-closed-by-yapf-enable",
        "yapf-disable-closed-by-fmt-on",
        "unterminated-fmt-off-suppresses-to-eof",
        "unrelated-standalone-comment-inside-fmt-off-block",
        "fmt-off-on-inside-expression-has-no-effect",
        "fmt-skip-simple-assignment",
        "fmt-skip-semicolon-joined-statements",
        "fmt-skip-decorator",
        "fmt-skip-def-header-only",
        "fmt-skip-case-header-only",
        "fmt-skip-multiline-call-covers-whole-statement",
        "fmt-skip-with-trailing-noqa-pragma",
        "fmt-skip-after-leading-noqa-pragma",
        "fmt-skip-substring-does-not-false-match",
        "fmt-skip-on-nested-expression-over-suppresses-whole-statement",
        "fmt-off-between-decorators-only-covers-marked-interval",
        "unterminated-fmt-off-ending-in-indented-suite-no-phantom-line",
    ],
)
def test_find_ignored_lines_honors_format_suppression_pragmas(source: str, expected: set[int]) -> None:
    assert find_ignored_lines(source) == expected


def test_find_ignored_lines_format_suppression_combines_with_inline_ignore_pattern() -> None:
    source = "x = 1  # pytriage: TR1\n# fmt: off\na=1\n# fmt: on\ny = 2\n"
    assert find_ignored_lines(source, ignore_pattern_for("TR1")) == {1, 2, 3, 4}


def test_ignored_lines_from_tokens_also_honors_format_suppression() -> None:
    source = "x = 1\n# fmt: off\na=1\n# fmt: on\ny = 2\n"
    assert ignored_lines_from_tokens(tokenize_source(source)) == {2, 3, 4}


def test_find_ignored_lines_and_classify_comments_also_honors_format_suppression() -> None:
    source = "x = 1\n# fmt: off\na=1\n# fmt: on\ny = 2\n"
    ignored, _comment_only, _trailing = find_ignored_lines_and_classify_comments(source)
    assert ignored == {2, 3, 4}


def _setup_plain(tmp_path: Path) -> Path:
    target = tmp_path / "mod.py"
    target.write_text("old\n")
    return target


def _verify_plain(target: Path) -> None:
    assert target.read_text() == "new\n"


def _setup_executable(tmp_path: Path) -> Path:
    target = tmp_path / "script.py"
    target.write_text("old\n")
    target.chmod(0o755)
    return target


def _verify_permission_preserved(target: Path) -> None:
    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def _setup_symlink(tmp_path: Path) -> Path:
    real_target = tmp_path / "real.py"
    real_target.write_text("old\n")
    link = tmp_path / "link.py"
    link.symlink_to(real_target)
    return link


def _verify_symlink_updated_in_place(target: Path) -> None:
    real_target = target.parent / "real.py"
    assert target.is_symlink()
    assert target.resolve() == real_target
    assert real_target.read_text() == "new\n"


def _setup_directory(tmp_path: Path) -> Path:
    target = tmp_path / "baddir.py"
    target.mkdir()
    return target


@pytest.mark.parametrize(
    ("setup", "verify", "raises_error"),
    [
        (_setup_plain, _verify_plain, False),
        (_setup_executable, _verify_permission_preserved, False),
        (_setup_symlink, _verify_symlink_updated_in_place, False),
        (_setup_directory, None, True),
    ],
    ids=["plain-file", "preserves-permissions", "updates-symlink-target", "target-is-directory-raises"],
)
def test_atomic_write_text(
    tmp_path: Path,
    setup: Callable[[Path], Path],
    verify: Callable[[Path], None] | None,
    *,
    raises_error: bool,
) -> None:
    target = setup(tmp_path)

    # A directory in place of the target makes the write fail without ever
    # renaming over it, exercising the cleanup path.
    ctx = pytest.raises(IsADirectoryError) if raises_error else nullcontext()
    with ctx:
        atomic_write_text(target, "new\n", "utf-8", "old\n")

    if verify is not None:
        verify(target)
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


@pytest.mark.parametrize(
    "content",
    [
        "def broken(:\n",
        # Valid per the grammar alone (ast.parse accepts it) but invalid at
        # compile time — a fix producing this must be rejected too, not
        # just fixes with a plain grammar error.
        "return 1\n",
    ],
    ids=["grammar-error", "compile-time-only-error"],
)
def test_atomic_write_text_rejects_invalid_syntax(tmp_path: Path, content: str) -> None:
    # A bad fix must never reach disk: validation runs before the temp file
    # is even created, so the target keeps its prior content untouched.
    target = tmp_path / "mod.py"
    target.write_text("old = 1\n")

    with pytest.raises(FixValidationError):
        atomic_write_text(target, content, "utf-8", "old = 1\n")

    assert target.read_text() == "old = 1\n"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_fix_validation_error_exposes_path_and_syntax_error(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"

    with pytest.raises(FixValidationError) as exc_info:
        atomic_write_text(target, "def broken(:\n", "utf-8", "")

    assert exc_info.value.path == target
    assert isinstance(exc_info.value.syntax_error, SyntaxError)


def test_atomic_write_text_aborts_when_disk_content_no_longer_matches_expected_source(tmp_path: Path) -> None:
    # expected_source stands in for what a caller read before computing its
    # own fix -- a mismatch against the file's real current bytes means
    # something else changed it in between, and the write must never land.
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n")

    with pytest.raises(ConcurrentModificationError) as exc_info:
        atomic_write_text(target, "x = 2\n", "utf-8", "x = 0\n")

    assert exc_info.value.path == target
    assert target.read_text() == "x = 1\n"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_atomic_write_text_detects_modification_that_happened_after_expected_source_was_captured(
    tmp_path: Path,
) -> None:
    # Simulates the exact race this guards against: a caller reads "x = 1\n",
    # some other process (an editor, a concurrent worker) overwrites the file
    # in between, and only then does the caller's own fix try to write back.
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n")
    expected_source = target.read_text()
    target.write_text("x = 999  # edited concurrently\n")

    with pytest.raises(ConcurrentModificationError):
        atomic_write_text(target, "x = 2\n", "utf-8", expected_source)

    assert target.read_text() == "x = 999  # edited concurrently\n"


def test_atomic_write_text_accepts_a_stateful_encoding_whose_bytes_dont_round_trip(tmp_path: Path) -> None:
    # A stateful codec like iso2022_jp can decode a redundant shift sequence
    # to the same text but never reproduce those exact bytes again on
    # encode -- comparing raw bytes against expected_source.encode() would
    # falsely abort every fix to an untouched file using such an encoding.
    # Comparing decoded text instead (what this test guards) is immune to
    # that, since the file's real content genuinely hasn't changed.
    raw = b"\x1b(B" + "x = 1\n".encode("iso2022_jp")
    target = tmp_path / "mod.py"
    target.write_bytes(raw)
    expected_source = raw.decode("iso2022_jp")
    assert expected_source.encode("iso2022_jp") != raw  # the premise this test guards against

    atomic_write_text(target, "x = 2\n", "iso2022_jp", expected_source)

    assert target.read_bytes().decode("iso2022_jp") == "x = 2\n"


def test_atomic_write_text_aborts_when_disk_content_no_longer_decodes(tmp_path: Path) -> None:
    # A concurrent edit that leaves bytes no longer valid in the original
    # encoding (e.g. a half-written save, or content switched to a
    # different encoding) is still a change this check must catch, not let
    # UnicodeDecodeError escape as an unrelated internal error.
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n")
    expected_source = target.read_text()
    target.write_bytes(b"x = \xff\xfe broken bytes\n")

    with pytest.raises(ConcurrentModificationError):
        atomic_write_text(target, "x = 2\n", "utf-8", expected_source)

    assert target.read_bytes() == b"x = \xff\xfe broken bytes\n"


def test_violation_stores_terminal_fix_outcome_separately_from_fix_data() -> None:
    violation = ViolationFactory.build(fix_data={"other_key": 1})

    violation.fix_outcome = FixOutcome.FAILED

    assert violation.fix_outcome is FixOutcome.FAILED
    assert violation.fix_data == {"other_key": 1}
