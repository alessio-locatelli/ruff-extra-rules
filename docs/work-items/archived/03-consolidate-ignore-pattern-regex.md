# Consolidate the duplicated ignore-comment regex

Status: Open
Kind: Refactor / duplication

## Problem

`IGNORE_PATTERN = re.compile(r"#\s*pytriage:\s*ignore=<code>", re.IGNORECASE)`
plus a call to `_base.py`'s tokenize-based `find_ignored_lines()` is defined
near-identically in three places: `forbid_vars.py`, `misplaced_comment.py`,
and `redundant_assignment/__init__.py`. Only the error code differs each
time.

A fourth check, `validate_function_name`, supports the same
`# pytriage: ignore=TRI004` syntax but through a separate, weaker mechanism:
`IGNORE_COMMENT_MARKER` (a bare string, not a compiled regex) and
`has_inline_ignore()` (`validate_function_name/analysis.py:15,42-47`), which
does a plain substring check on the function definition's source line.
Unlike `find_ignored_lines()`, this can't distinguish a real comment from the
same text appearing inside a string or byte literal on that line, and it's
case-sensitive where the other three checks are not (they compile their
pattern with `re.IGNORECASE`).

## Proposed Fix

Add a shared helper to `_base.py`, e.g.
`ignore_pattern_for(error_code: str) -> re.Pattern[str]`, and have every
check call it instead of hand-rolling the regex literal — including
migrating `validate_function_name` onto `find_ignored_lines()` in the same
pass, which also closes its string-literal and case-sensitivity gaps. Do
this after
[02-inline-ignore-parity-tri002-tri003.md](02-inline-ignore-parity-tri002-tri003.md)
so all 6 checks converge on the same mechanism in one pass.

## Priority

Medium — depends on item 02 landing first.
