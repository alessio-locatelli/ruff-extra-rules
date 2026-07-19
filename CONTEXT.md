# Ruff Extra Rules

Custom pre-commit/prek hooks providing fast, AST-based Python rule checks that ruff doesn't (yet) cover, plus the shared runtime (caching, prefiltering, orchestration) they run on. Runs alongside ruff, not instead of it.

## Language

**Hook**:
An installable unit registered in `.pre-commit-hooks.yaml` that pre-commit/prek invokes as a subprocess against a set of files. This repo exposes a single one, `ruff-extra-rules`, backed by the `ast_checks` orchestrator: every check runs in one pass, report-only by default, with `--select`/`--ignore`/`--fix` narrowing and fixing like `ruff check`'s own flags (see `docs/adr/0008-ruff-check-cli-parity.md`).
_Avoid_: linter, tool

**Check**:
A single, independently toggleable rule (e.g. `forbid-vars`, `redundant-assignment`, `misplaced-comment`) implementing the `ASTCheck` protocol, identified by a `check_id` and an error code (`TRI00N` or `STYLE-001`). Many checks run inside one hook invocation, orchestrated by `CheckOrchestrator`.
_Avoid_: rule, linter, hook — a check is not a hook; several checks share one hook

**Violation**:
A single reported instance of a check failing on one file, at one line/column, optionally carrying data needed to auto-fix it.
_Avoid_: error, issue, finding

**Prefilter pattern**:
A fixed string a check declares via `get_prefilter_pattern()` so `git grep` can cheaply skip files that can't possibly contain a violation, before the file is read or parsed.
_Avoid_: filter — the user-facing `--exclude` glob is a distinct, unrelated concept (excludes files outright; a prefilter pattern only skips _checking_, never skips reporting if matched)

**Inline ignore comment**:
A `# pytriage: ignore=TRI00N` (or `=STYLE-001`) comment suppressing one violation on the line it appears on. Detected via `tokenize`, never text/regex matching, so a string or byte literal containing the same text can't be mistaken for one.
_Avoid_: pragma — this repo's own suppression comment is distinct from the _third-party_ linter pragmas (`noqa`, `type: ignore`, `pylint:`, etc.) that `misplaced-comment` recognizes and refuses to ever move; don't conflate the two.

**Fix**:
An in-place edit a check applies to resolve its own violations, requested via `--fix` and applied by the check's own `fix()` method against a freshly re-read file/tree. Every fix is written through `atomic_write_text()`, which validates the result parses as Python before committing it — a fix that would produce invalid syntax is rejected and the file is left untouched (see `docs/adr/0010-fix-validation-before-write.md`).
_Avoid_: autofix is fine informally; "fix" is the protocol method name

**Fix rejection**:
The outcome when `atomic_write_text()` refuses a fix because the content it was asked to write doesn't parse as valid Python. Reported as `[FIX REJECTED]`, distinct from `[FIXED]`/`[FIXABLE]` — it signals a bug in the check's own fix logic, not something re-running `--fix` will resolve.
_Avoid_: failed fix, broken fix — "rejected" is the term the CLI output and `is_fix_rejected()`/`mark_fix_rejected()` use

**Fix error**:
The outcome when a check's own `fix()` raises an exception other than `FixValidationError` — a bug in the check's fix logic itself, distinct from a fix rejection (which means `fix()` ran to completion but its _output_ didn't parse). Reported as `[FIX ERRORED]`, also not something re-running `--fix` will resolve (see `docs/adr/0012-behavioral-contract-audit-internal-errors-exit-codes.md`).
_Avoid_: fix rejection, crash — "errored" is the term the CLI output and `is_fix_errored()`/`mark_fix_errored()` use; a fix rejection never reaches this path since it's a normal, expected outcome, not an unhandled exception
