# Inline-ignore parity for TRI002 and TRI003

Status: Open
Kind: Inconsistency / missing feature

## Problem

TRI001 (`forbid_vars.py`), TRI005 (`redundant_assignment/__init__.py`), and
STYLE-001 (`misplaced_comment.py`) support `# pytriage: ignore=<code>` inline
suppression via the shared, tokenize-based `find_ignored_lines()` in
`_base.py`. TRI004 (`validate_function_name`) also supports the same syntax,
but through its own separate mechanism: `has_inline_ignore()`
(`validate_function_name/analysis.py:42-47`) does a plain substring check for
`IGNORE_COMMENT_MARKER` on the function definition's source line, instead of
calling `find_ignored_lines()`. TRI002 (`excessive_blank_lines.py`) and TRI003
(`redundant_super_init.py`) support neither — both docstrings say so
explicitly ("Inline ignore: ... (not currently supported)",
`excessive_blank_lines.py:6` and `redundant_super_init.py:7`), and neither
calls any ignore-detection function. This is a real user-facing
inconsistency: 2 of 6 checks can't be suppressed inline at all, with no
documented reason why.

## Proposed Fix

Wire `find_ignored_lines` into `excessive_blank_lines.py` and
`redundant_super_init.py`, the same way TRI001/TRI005/STYLE-001 already do.
Update both docstrings/README "Features" sections once implemented. Add
`ignore/` fixture cases matching the convention already used under
`tests/fixtures/validate_function_name/ignore/`. Whether TRI004 should also
be migrated onto `find_ignored_lines()` (rather than kept as its own
substring check) is handled separately in
[03-consolidate-ignore-pattern-regex.md](03-consolidate-ignore-pattern-regex.md).

## Priority

Medium.
