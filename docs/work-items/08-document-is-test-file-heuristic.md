# Document the _is_test_file() path heuristic in the README

Status: Open — resolved by ADR 0007, not yet implemented
Kind: Documentation / hidden dependency

## Problem

`redundant_assignment/semantic.py:_is_test_file()` (lines 11-30) makes
TRI005's semantic-value threshold depend on whether a file's path contains
`tests`/`test` or its filename matches `test_*`/`*_test.py`. Moving a file
into or out of a test directory silently changes which violations get
reported, with nothing in the diagnostic output — or, until now, the
README — revealing why.

## Decision (2026-07-16 ADR interview, see docs/adr/0007)

Keep the heuristic exactly as-is; no code change. This project has no
consumers besides this repo itself, and its layout (`tests/`, `test_*.py`)
already matches the hardcoded convention — building configurability for a
hypothetical outside consumer isn't justified. What's actually missing is
documentation: nothing in `README.md`'s TRI005 section currently says this
relaxation exists, which is exactly what made it easy to forget it was even
there.

## Proposed Fix

Add a line to `README.md`'s `redundant-assignment` (TRI005) section
documenting that files under a conventional test directory/filename get a
relaxed semantic-value threshold. `docs/adr/0007-redundant-assignment-test-file-heuristic-deliberate.md`
records the reasoning; this item is just the doc update.

## Priority

Low risk, low effort.
