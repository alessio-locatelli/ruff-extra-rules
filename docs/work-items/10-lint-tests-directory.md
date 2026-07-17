# Consider running ast-checks against tests/ in this repo's own config

Status: Open — idea, not yet scoped
Kind: Feature (future)

## Problem

`.pre-commit-config.yaml`'s local `ast-checks`/`misplaced-comment` hooks
currently run with `files: ^src/`, excluding `tests/` entirely from this
repo's own self-dogfooding. The existing comment there explains why:
`tests/fixtures/` deliberately contains "bad" example code for the test
suite, and the test suite itself idiomatically uses forbidden names like
`result` far more than production code does.

This is a related but distinct idea from
[08-document-is-test-file-heuristic.md](08-document-is-test-file-heuristic.md)'s
decision, which only concerns TRI005's existing test-directory relaxation,
not whether checks run against `tests/` at all: extending self-checking to
`tests/` is a plausible next feature.

## Proposed Fix

Not scoped yet. Would need to reconcile at least:

- `tests/fixtures/<check>/bad/*.py` is intentionally-violating example code
  and must stay excluded regardless of any other change here.
- Whether `--exclude` on the existing hook invocations (excluding just
  `tests/fixtures/`) is sufficient, or whether per-check tuning is also
  needed for the rest of `tests/` given the forbidden-name-heavy idiom noted
  above.

## Priority

Idea only — no priority assigned until scoped.
