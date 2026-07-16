# Pre-Commit Extra Hooks

Custom pre-commit/prek hooks providing fast, AST-based Python code-quality checks, plus the shared runtime (caching, prefiltering, orchestration) they run on.

## Language

**Hook**:
An installable unit registered in `.pre-commit-hooks.yaml` that pre-commit/prek invokes as a subprocess against a set of files. This repo exposes two, both backed by the same `ast_checks` orchestrator: `ast-checks` (every check, report-only by default) and `misplaced-comment` (STYLE-001 only, `--fix` on by default).
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
An in-place edit a check applies to resolve its own violations, requested via `--fix` and applied by the check's own `fix()` method against a freshly re-read file/tree.
_Avoid_: autofix is fine informally; "fix" is the protocol method name
