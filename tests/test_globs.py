from __future__ import annotations

import re
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._globs import InvalidGlobError, anchored_pattern, compile_glob, glob_matches


# Every expectation below was measured against `ruff 0.16.1` before being
# written down; see ADR-0049.
@pytest.mark.parametrize(
    ("pattern", "candidate", "expected"),
    [
        ("__init__.py", "__init__.py", True),
        ("__init__.py", "mod.py", False),
        ("*.py", "mod.py", True),
        ("*.py", "mod.pyi", False),
        ("tests/*.py", "tests/t.py", True),
        ("tests/*.py", "tests/deep/deeper/t2.py", True),
        ("tests/**", "tests/deep/t.py", True),
        ("tests/**", "src/t.py", False),
        ("src/**.py", "src/pkg/mod.py", True),
        ("src/**.py", "tests/t.py", False),
        ("a/**/b.py", "a/b.py", True),
        ("a/**/b.py", "a/mid/b.py", True),
        ("a/**/b.py", "a/x/y/b.py", True),
        ("a/**/b.py", "z/b.py", False),
        ("**/deeper/*.py", "tests/deep/deeper/t2.py", True),
        ("**", "anything/at/all.py", True),
        ("***/foo.py", "src/foo.py", True),
        ("***/foo.py", "foo.py", False),
        ("src/***/foo.py", "src/mid/foo.py", True),
        ("src/***/foo.py", "src/foo.py", False),
        ("a/?/b.py", "a/x/b.py", True),
        ("a/?/b.py", "a/mid/b.py", False),
        ("a/{mid,x}/**", "a/mid/b.py", True),
        ("a/{mid,x}/**", "a/b.py", False),
        ("a/[mx]*/**", "a/mid/b.py", True),
        ("a/[mx]*/**", "a/b.py", False),
        ("*{ab,a}*b.py", "ab.py", True),
        ("*{ab,a}*b.py", "xaXb.py", True),
        ("*{ab,a}*b.py", "zzz.py", False),
        ("[!a].py", "b.py", True),
        ("[!a].py", "a.py", False),
        ("[^a].py", "b.py", True),
        ("[^a].py", "a.py", False),
        ("[a^].py", "^.py", True),
        ("[a^].py", "b.py", False),
        ("[]].py", "].py", True),
        ("[[].py", "[.py", True),
        ("[a&b].py", "a.py", True),
        ("[a|b].py", "b.py", True),
        ("[a~b].py", "~.py", True),
        (r"a\*.py", "a*.py", True),
        (r"a\*.py", "ab.py", False),
        (r"b\.py", "b.py", True),
        (r"b\.py", "b\\.py", False),
        (r"*\.py", "b\\.py", True),
        ("a,b.py", "a,b.py", True),
        (r"a\{b,c\}d.py", "a{b,c}d.py", True),
        ("[{]a.py", "{a.py", True),
        ("{a[,]b,c}.py", "a,b.py", True),
        ("{a[,]b,c}.py", "c.py", True),
        (r"{a\,b,c}.py", "a,b.py", True),
        ("a{b,{c,d}}e.py", "ade.py", True),
        ("a.b.py", "ab.py", False),
    ],
    ids=[
        "basename-literal",
        "basename-literal-no-match",
        "star-matches-within-a-name",
        "star-respects-the-rest-of-the-name",
        "star-matches-a-direct-child",
        "star-spans-separators",
        "double-star-suffix-is-recursive",
        "double-star-suffix-is-anchored",
        "double-star-inside-a-component-is-recursive",
        "double-star-inside-a-component-stays-anchored",
        "double-star-component-matches-zero-components",
        "double-star-component-matches-one-component",
        "double-star-component-matches-many-components",
        "double-star-component-keeps-its-prefix",
        "double-star-prefix-is-recursive",
        "bare-double-star-matches-everything",
        "three-stars-are-not-a-recursive-component",
        "three-stars-still-need-a-component",
        "three-stars-inside-a-path-need-a-component",
        "three-stars-inside-a-path-are-not-recursive",
        "question-mark-matches-one-character",
        "question-mark-does-not-match-many",
        "braces-alternate",
        "braces-must-match-one-alternative",
        "character-class-matches",
        "character-class-must-match",
        "an-alternation-between-two-stars-can-still-give-back",
        "an-alternation-between-two-stars-matches-further-in",
        "an-alternation-between-two-stars-must-still-match",
        "negated-character-class-matches",
        "negated-character-class-excludes",
        "caret-negates-a-character-class-too",
        "caret-negated-class-excludes",
        "caret-inside-a-class-is-a-member",
        "caret-member-must-match",
        "closing-bracket-first-is-a-member",
        "an-opening-bracket-is-a-member",
        "an-ampersand-is-a-member",
        "a-pipe-is-a-member",
        "a-tilde-is-a-member",
        "backslash-escapes-a-metacharacter",
        "escaped-metacharacter-is-not-a-wildcard",
        "an-escaped-dot-is-an-ordinary-dot",
        "an-escaped-dot-is-not-a-backslash",
        "a-star-spans-a-backslash-in-the-name",
        "comma-outside-braces-is-literal",
        "escaped-braces-are-literal",
        "a-brace-in-a-character-class-is-literal",
        "a-comma-in-a-character-class-does-not-split",
        "a-class-branch-still-alternates",
        "an-escaped-comma-does-not-split",
        "braces-nest",
        "dot-is-literal",
    ],
)
def test_glob_matches(pattern: str, candidate: str, expected: bool) -> None:
    assert glob_matches(pattern, candidate) is expected


# `ruff 0.16.1` refuses each of these too, rather than quietly matching nothing.
@pytest.mark.parametrize(
    ("pattern", "needle"),
    [
        ("[abc", "leaves a `[` unclosed"),
        ("[a\\", "leaves a `[` unclosed"),
        ("[z-a].py", "could not be compiled"),
        (r"[a-\z].py", "could not be compiled"),
        ("{a,b", "leaves a `{` unclosed"),
        ("a}", "closes a `{` that was never opened"),
        ("a\\", "ends with an unfinished escape"),
        # `{a,aa}` repeated overlaps with itself, which a backtracking engine
        # explores exhaustively; the ceiling turns a stall into a rejection.
        ("{a,aa}" * 30 + "b", "more than 1024 ways to match"),
    ],
    ids=[
        "unclosed-class",
        "class-ending-in-a-backslash",
        "reversed-range",
        "range-ending-in-a-backslash",
        "unclosed-brace",
        "unopened-brace",
        "trailing-escape",
        "too-many-alternatives",
    ],
)
def test_an_uncompilable_pattern_is_rejected(pattern: str, needle: str) -> None:
    with pytest.raises(InvalidGlobError, match=re.escape(needle)):
        compile_glob(pattern)


def test_an_anchored_pattern_keeps_its_alternation_rather_than_expanding_it() -> None:
    # One pattern per branch would be 2**n of them for n adjacent groups, so
    # the alternation stays in the pattern for the matcher to handle.
    assert anchored_pattern("{a,b}{c,d}/x.py", Path("/p")) == "/p/{a,b}{c,d}/x.py"


def test_an_anchor_is_escaped_rather_than_read_as_a_pattern() -> None:
    # A checkout path is a real directory name, not a glob; an unescaped `[`
    # here would be an unclosed character class and fail to compile at all.
    assert anchored_pattern("src/**", Path("/base/pro[ject")) == "/base/pro\\[ject/src/**"


def test_a_pattern_may_still_spell_out_a_useful_number_of_alternatives() -> None:
    assert glob_matches("{a,b}" * 10 + ".py", "ababababab.py")
