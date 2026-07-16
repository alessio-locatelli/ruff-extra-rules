# Consolidate the duplicated ignore-comment regex

Status: Open
Kind: Refactor / duplication

## Problem

`IGNORE_PATTERN = re.compile(r"#\s*pytriage:\s*ignore=<code>", re.IGNORECASE)`
is defined near-identically in four places: `forbid_vars.py`,
`misplaced_comment.py`, `redundant_assignment/__init__.py`, and
`validate_function_name/analysis.py`. Only the error code differs each time.
`_base.py`'s `find_ignored_lines()` already centralizes the _matching_ logic
(via `tokenize`); only the pattern-construction call site is duplicated.

## Proposed Fix

Add a shared helper to `_base.py`, e.g.
`ignore_pattern_for(error_code: str) -> re.Pattern[str]`, and have every check
call it instead of hand-rolling the regex literal. Do this after
[02-inline-ignore-parity-tri002-tri003.md](02-inline-ignore-parity-tri002-tri003.md)
so all 6 checks (not just 4) migrate onto the shared helper in one pass.

## Priority

Medium — depends on item 02 landing first.
