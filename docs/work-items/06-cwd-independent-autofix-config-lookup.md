# CWD-independent pyproject.toml lookup for forbid-vars autofix config

Status: Open
Kind: Bug / hidden dependency

## Problem

`forbid_vars.load_autofix_config()` (`forbid_vars.py:94`) reads
`Path("pyproject.toml")` relative to the process's current working directory,
not the repo root. `ForbidVarsCheck.__init__` calls it once at check-load
time. The documented CLI usage pattern
(`uv run python -m pre_commit_hooks.ast_checks ... path/to/file.py`) allows
invocation from any CWD; if that CWD isn't the repo root, the user's
`[tool.forbid-vars.autofix]` customization silently stops applying — no
error, no warning, just the default `http`-only category.

## Proposed Fix

Resolve the repo root via `git rev-parse --show-toplevel` (this repo already
depends on `git` being present, via `_prefilter.py`) before looking for
`pyproject.toml`, or, if that's judged unnecessary, explicitly document and
test the CWD assumption so the behavior is at least intentional rather than
accidental.

## Priority

Low-medium.
