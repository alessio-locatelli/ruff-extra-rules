# Atomic writes for check fix() output

Status: Open
Kind: Bug / correctness

## Problem

Every check's `fix()` writes its result via a plain `Path.write_text(...)`:
`misplaced_comment.py:228`, `excessive_blank_lines.py:234`, `forbid_vars.py:505`,
`redundant_assignment/autofix.py:128`. None of these use a temp-file-then-rename
pattern. `_cache.py`'s `_write_cache` (`_cache.py:218-229`) already demonstrates
that pattern in this same codebase (atomic on POSIX via `Path.replace()`), just
not applied to the higher-stakes write path — the user's actual source file. A
process kill or crash mid-`write_text()` could leave a truncated, invalid file
on disk instead of either the old or new content.

## Proposed Fix

Add `atomic_write_text(path: Path, content: str, encoding: str) -> None` to
`_base.py`, mirroring `_write_cache`'s temp-file + `Path.replace()` approach.
Swap the four direct `write_text()` call sites above to use it. Mechanical,
should require no fixture changes.

## Priority

High — the only correctness/data-safety item in this backlog. Do this first.
