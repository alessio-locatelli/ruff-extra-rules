# Ruff Extra Rules

Custom pre-commit/prek hooks providing fast, AST-based Python rule checks that ruff doesn't (yet) cover, plus the shared runtime (caching, prefiltering, orchestration) they run on. Runs alongside ruff, not instead of it.

## Language

**Hook**:
An installable unit registered in `.pre-commit-hooks.yaml` that pre-commit/prek invokes as a subprocess against a set of files. Each hook runs a fixed set of checks in one pass, report-only by default, with `--select`/`--ignore`/`--fix` narrowing and fixing like `ruff check`'s own flags (see `docs/adr/0008-ruff-check-cli-parity.md`). A selection is intersected with the hook's own fixed set rather than rejected, so one project's configuration can serve every hook.
_Avoid_: linter, tool

**Check**:
A single, independently toggleable rule (e.g. `meaningless-vars`, `redundant-assignment`, `misplaced-comment`) implementing the `ASTCheck` protocol, identified by a `check_id` and an error code (e.g. `TR1`). The `check_id` is also what names the check's own configuration sub-table. Many checks run inside one hook invocation, orchestrated by `CheckOrchestrator`.
_Avoid_: rule, linter, hook — a check is not a hook; several checks share one hook

**Project root**:
The directory holding the nearest `pyproject.toml` that carries a `[tool.ruff-extra-rules]` table, found by searching upward from the working directory and never leaving the enclosing git repository. Anchors the cache directory and every relative path in the configuration (see `docs/adr/0045-pyproject-toml-configuration.md`).
_Avoid_: config directory, workspace root, repository root — the git repository bounds the search but is not itself the answer

**Resolved configuration**:
The single settings object a run operates on, layering command-line arguments over the project root's table over each option's declared default. One per process, never per file — which is what lets every check be instantiated once for the whole run.
_Avoid_: settings, config, config file — the file is one input to it, not the thing itself

**Option**:
A single setting a check declares once, as data, from which its command-line flag, its TOML key, its accepted values, and the constructor keyword it supplies are all derived (see `docs/adr/0047-declarative-option-descriptors.md`).
_Avoid_: flag, argument — a flag is one of an option's two surfaces, not the option

**Violation**:
A single reported instance of a check failing on one file, at one line/column, optionally carrying data needed to auto-fix it.
_Avoid_: error, issue, finding

**Prefilter pattern**:
A fixed string a check declares via `get_prefilter_pattern()` so `git grep` can cheaply skip files that can't possibly contain a violation, before the file is read or parsed.
_Avoid_: filter — the user-facing `--exclude` glob is a distinct, unrelated concept (excludes files outright; a prefilter pattern only skips _checking_, never skips reporting if matched)

**Per-file ignore**:
A `per-file-ignores` entry switching one check off for the files a glob pattern matches, so a check can be dropped in a subtree without being dropped from the run (see `docs/adr/0049-per-file-ignores.md`). The check does not run on those files at all, and so never fixes them either.
_Avoid_: exclude — an excluded file is not checked by anything; a per-file ignore only removes some checks from it. Also avoid calling it a suppression: an inline ignore comment suppresses one violation on one line, this decides which checks a file gets.

**Inline ignore comment**:
A `# pytriage: TR1` comment, or a comma-separated list (`# pytriage: TR1,TR5`) to suppress more than one check's violation on the same line. Detected via `tokenize`, never text/regex matching, so a string or byte literal containing the same text can't be mistaken for one.
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

**Fix failure**:
The outcome when a check's own `fix()` catches an `OSError` from `atomic_write_text()` itself (disk full, permission denied, missing parent directory) and returns `False` without raising — an environmental failure, distinct from a fix error (which means `fix()` itself raised, a bug in the check's own logic). Reported as `[FIX FAILED]`, with a hint about checking permissions/disk space rather than `[FIX ERRORED]`'s "this is a bug, please report it" (see `docs/adr/0017-behavioral-contract-audit-diagnostics-fix-modes-user-trust.md`).
_Avoid_: fix error, fix rejection — "failed" is the term the CLI output and `is_fix_failed()`/`mark_fix_failed()` use; unlike a fix error, retrying `--fix` after fixing the underlying environmental issue may actually succeed
