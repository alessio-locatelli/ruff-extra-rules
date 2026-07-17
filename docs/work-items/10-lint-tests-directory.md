# Consider running ast-checks against tests/ in this repo's own config

Status: On hold — see docs/work-items/11-ruff-check-cli-parity.md
Kind: Feature (future)

## Problem

`.pre-commit-config.yaml`'s local `ast-checks`/`misplaced-comment` hooks
currently run with `files: ^src/`, excluding `tests/` entirely from this
repo's own self-dogfooding. The existing comment there explains why:
`tests/fixtures/` deliberately contains "bad" example code for the test
suite, and the test suite itself idiomatically uses forbidden names like
`result` far more than production code does.

This is a related but distinct idea from
[archived/08-document-is-test-file-heuristic.md](archived/08-document-is-test-file-heuristic.md)'s
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

## Session Note (2026-07-17)

Reviewed alongside the rest of `docs/work-items/` this session but not
implemented: this needs a maintainer scoping decision (whether to extend
self-checking to `tests/` at all, and if so how aggressively to reconcile
the forbidden-name-heavy idiom noted above), not just an implementation.
Left open for a future session once scoped.

## Put On Hold (2026-07-17)

A separate session starting from this item's own `.pre-commit-config.yaml`
confusion (the `ast-checks`/`misplaced-comment` hook split) escalated into
a broader redesign: collapsing that split into a single `ruff-extra-rules`
hook with `ruff check`-style `--fix`/`--select`/`--ignore` flags. See
`docs/adr/0008-ruff-check-cli-parity.md` and
`docs/work-items/11-ruff-check-cli-parity.md`. Scoping _this_ item (whether
and how to lint `tests/`) against a CLI surface that's about to change
would be wasted effort, so it's on hold until item 11 lands.
