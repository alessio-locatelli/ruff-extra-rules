from __future__ import annotations

import re

import pytest

from pre_commit_hooks.ast_checks._globs import InvalidGlobError, compile_glob, glob_matches


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
        (r"a\*.py", "a*.py", True),
        (r"a\*.py", "ab.py", False),
        ("a,b.py", "a,b.py", True),
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
        "backslash-escapes-a-metacharacter",
        "escaped-metacharacter-is-not-a-wildcard",
        "comma-outside-braces-is-literal",
        "dot-is-literal",
    ],
)
def test_glob_matches(pattern: str, candidate: str, expected: bool) -> None:
    assert glob_matches(pattern, candidate) is expected


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
    ],
    ids=[
        "unclosed-class",
        "class-ending-in-a-backslash",
        "reversed-range",
        "range-ending-in-a-backslash",
        "unclosed-brace",
        "unopened-brace",
        "trailing-escape",
    ],
)
def test_an_uncompilable_pattern_is_rejected(pattern: str, needle: str) -> None:
    with pytest.raises(InvalidGlobError, match=re.escape(needle)):
        compile_glob(pattern)
