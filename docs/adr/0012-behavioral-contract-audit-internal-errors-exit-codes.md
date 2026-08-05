# Behavioral contract audit: internal error isolation & CLI exit codes (ch. 5, 6)

**Update, ADR-0045**: the exit-code contract documented below gains a third value. Invalid configuration — including an unknown `--select`/`--ignore` name, which this ADR recorded as exiting 1 — now exits 2. The 0 and 1 outcomes for runs that actually happened are unchanged.

`docs/behavioral_contract.md` chapters 5 (Internal Errors and Failure Isolation) and 6 (CLI Exit Codes) were audited against `CheckOrchestrator` and `main()`'s reporting/exit-code logic. Failure isolation itself was already correct: a check's `check()`/`fix()` exception is caught per-file-per-check, so one broken check can't stop another check or file from being processed. The gap was that an isolated failure wasn't always surfaced to the user or the exit code. Full findings are in `docs/audits/0003-behavioral-contract-audit-internal-errors-exit-codes.md`.

## Decision

An internal failure must never look like a clean, successful run. Two outcomes previously did: a check whose `check()` raised on every file produced zero violations and exit code `0` (and, in non-fix-mode, cached that incomplete result); a check's `fix()` raising anything other than `FixValidationError` left the violation reported as ordinary `[FIXABLE]`, indistinguishable from one that hadn't been attempted. Both are now tracked via `CheckOrchestrator.rule_failures: list[tuple[str, str]]` (filepath, check_id), which forces `exit_code = 1`, skips caching for the affected file, and — for the fix case — reports the violation as `[FIX ERRORED]` with a "this is a bug" hint instead of the misleading "run with --fix" suggestion.

`main()`'s exit-code contract was also under-documented: its docstring said only "0 if no violations, 1 if violations found," missing that unparseable files, unknown `--select`/`--ignore` names, and a malformed CLI argument (which `argparse` exits `2` for, bypassing `main()` entirely) all already produced other outcomes. The docstring now states the full contract.

A `--fix` run that resolves every violation still exits `1`, deliberately — matched to the pre-commit/prek convention that a hook which modifies files should fail the commit so the user reviews and re-stages the diff (the same convention `black`/`ruff-format` follow), not a gap.

## Consequences

- `CheckOrchestrator.rule_failures` is populated whenever a check's `check()` or `fix()` raises unexpectedly; existing callers reading only `all_violations`/`unprocessable_files` are unaffected.
- `main()`'s exit code is now `1` (previously `0`) when a check crashes during analysis or fixing and nothing else in the run would otherwise have reported it.
- `Violation.fix_data` can carry a `fix_errored` key; `[FIX ERRORED]` is a new report tag distinct from `[FIXABLE]`/`[FIXED]`/`[FIX REJECTED]`.
- A file where any check crashed is no longer cached, so a transient failure doesn't freeze a false "clean" result into subsequent cache-hit runs.
- Fixes are committed per (check, file), in each check's own fixed order — never batched across checks or the whole invocation — which was already true and is now documented as a deliberate, safe commit boundary rather than left implicit.
