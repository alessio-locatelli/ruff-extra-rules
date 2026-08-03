# `CheckUnavailableError` disables only the check that raised it, not the whole run

## Context

`redundant-type-conversion` (TR6, see `docs/adr/0035-redundant-type-conversion-ty-lsp-detection.md`) raises `CheckUnavailableError` when its own external prerequisite, Astral's `ty`, is missing or fails its compatibility self-test. TR6 is enabled by default, the same as every check in `ALL_CHECKS` — a consumer of the `ruff-extra-rules` pre-commit hook gets it automatically, with no `--select` needed.

`ty` is only this repo's own dev dependency; `.pre-commit-hooks.yaml` declares no `additional_dependencies` for the published hook, so a consumer's own `language: python` hook environment never installs it. `CheckUnavailableError` originally propagated out of `CheckOrchestrator.process_files()` uncaught, all the way to `main()`, which printed it once and returned exit code 1 — deliberately, so a missing prerequisite affecting every file identically wasn't reported once per file. But "propagate out of `process_files()`" also meant every other, unrelated check's results for every file in the run were discarded along with it: any consumer who upgraded to a version of `ruff-extra-rules` that shipped TR6, without already having `ty` on `PATH`, would find their entire hook broken — not just missing TR6's own coverage.

## Considered Options

- **Bundle `ty` as a hook dependency**: declare it in `.pre-commit-hooks.yaml`'s `additional_dependencies` (or as a runtime dependency) so pre-commit auto-installs it for every consumer. Rejected as the sole fix: it closes the gap for the pre-commit path specifically, but not for prek or a direct CLI invocation that bypasses pre-commit's own environment management, and it doesn't help a consumer whose `ty` is present but fails the self-test. The underlying blast-radius problem — one check's own unavailability taking down every other check's results — would still exist for that case.
- **Make TR6 opt-in only, excluded from the default "run everything" set**: would need a new "default-enabled" concept on `ASTCheck`/the CLI that doesn't exist today, and would apply this fix only to TR6 rather than to any future check that can raise `CheckUnavailableError`.
- **Isolate `CheckUnavailableError` to just the check that raised it**: adopted (below).

## Decision

`CheckOrchestrator` catches `CheckUnavailableError` itself now, inside `_check_file`'s own per-check loop, rather than letting it propagate out of `process_files()`. The first time a given check_id raises it, `CheckOrchestrator` records `(check_id, str(error))` in a new `unavailable_checks` list and adds that check_id to an internal skip-set; every subsequent file in the same run skips calling that check entirely rather than paying its own failure cost (and recording a duplicate entry) again. Every other check keeps running and reporting normally, for every file, including the same file the unavailable check failed on.

`_diagnostics.report()` prints each `unavailable_checks` entry once, the same actionable message `CheckUnavailableError` already carried, and fails the run (exit code 1) — preserving the original "a missing prerequisite is a failure, not silent empty output" intent, just without discarding every other check's own results to do it.

## Consequences

**Amended by `docs/adr/0039-tr6-unavailable-message-scope-and-wording.md`:** "one-time" below means once per process, not once per run — prek/pre-commit's default parallelism runs a non-`require_serial` hook as multiple worker processes, each with its own fresh state, so a consumer can see the message once per worker process in a single run.

- A consumer without `ty` installed still gets a clear, actionable, one-time failure message for TR6 specifically, and exit code 1 — but every other enabled check's violations are still reported for every file, exactly as if TR6 weren't enabled at all. Enabling a new check that depends on an optional external prerequisite can no longer silently break every other check for a consumer who hasn't installed it.
- `CheckOrchestrator.process_files()` no longer raises `CheckUnavailableError` at all; `_cli.py`'s own `try`/`except` around it is gone, replaced by `report()` consulting `orchestrator.unavailable_checks` the same way it already consults `unprocessable_files` and `rule_failures`.
- This is now the general contract for any check that can raise `CheckUnavailableError`, not a TR6-specific carve-out — a future check with its own external prerequisite gets the same isolation for free.
- A _cacheable_ check (unlike TR6, which opts out of caching entirely — see ADR-0034) raising `CheckUnavailableError` must also block that file's cache write for its whole cacheable group, the same way an ordinary crash already does: caching a "clean" result collected while a cacheable check was unavailable would let a later, post-recovery run keep serving that stale result instead of actually retrying the check once its prerequisite comes back.
- Bundling `ty` as a hook dependency remains a real, separate option for closing the "missing prerequisite" case entirely for the pre-commit path specifically; this decision doesn't preclude it, but doesn't require it either, since the blast-radius problem it would otherwise leave unaddressed for prek/CLI users is now fixed regardless.
