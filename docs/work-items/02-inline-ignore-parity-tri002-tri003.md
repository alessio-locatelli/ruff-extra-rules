# Inline-ignore parity for TRI002 and TRI003

Status: Open
Kind: Inconsistency / missing feature

## Problem

TRI001, TRI004, TRI005, and STYLE-001 all support
`# pytriage: ignore=<code>` inline suppression via the shared
`find_ignored_lines()` in `_base.py`. TRI002 (`excessive_blank_lines.py`) and
TRI003 (`redundant_super_init.py`) don't — both docstrings say so explicitly
("Inline ignore: ... (not currently supported)", `excessive_blank_lines.py:6`
and `redundant_super_init.py:7`), and neither calls `find_ignored_lines`. This
is a real user-facing inconsistency: 2 of 6 checks can't be suppressed inline
at all, with no documented reason why.

## Proposed Fix

Wire `find_ignored_lines` into both checks the same way the other four do.
Update both docstrings/README "Features" sections once implemented. Add
`ignore/` fixture cases matching the convention already used under
`tests/fixtures/validate_function_name/ignore/`.

## Priority

Medium.
