# Checks keep tokenizing independently; no orchestrator-level shared-token protocol

## Context

`CheckOrchestrator._check_file()` reads each file once and parses its AST once, then hands the same `tree`/`source` pair to every enabled check through the `ASTCheck.check(filepath, tree, source)` contract. Tokenizing is not shared the same way. Several checks each call `tokenize`-based helpers in `_base.py` (`find_ignored_lines`, `find_ignored_lines_and_classify_comments`, `ignored_lines_from_tokens`) to detect `# pytriage: <code>` suppression comments, and `misplaced-comment` tokenizes for its own primary structural analysis (finding bracket-only closing lines). Each of these already tokenizes a given file only once per its own `check()`/`fix()` call — the redundant _within-check_ double-tokenize passes were fixed separately (fusing `find_ignored_lines`+`classify_comment_lines` in `redundant-assignment`, reusing one token list across `misplaced-comment`'s own scan and its own suppression-comment lookup). What remains is duplication _across_ checks: several independent checks tokenize the same file's `source` string, each paying the full tokenize cost again.

## Considered Options

- **Extend the `ASTCheck` protocol so `CheckOrchestrator` tokenizes each file once and passes the result (raw tokens, or a precomputed ignored-lines index) into every check**: evaluated via profiling below; rejected for now.
- **A process-global cache in `_base.py` keyed by `id(source)`, transparently memoizing `tokenize_source()` across checks within one `_check_file()` call, without touching the `ASTCheck` protocol**: rejected. `id()` reuse after garbage collection is a real correctness hazard, and a hidden mutable cache keyed on object identity contradicts `ASTCheck`'s own documented "independent and stateless across files" contract — a wrong cache hit would fail silently rather than visibly.
- **Cache each check's own tokens between its `check()` and its later `fix()` call** (same file, called back-to-back by `_apply_fixes()`) via `Violation.fix_data`: considered for `misplaced-comment`/`excessive-blank-lines`, whose `fix()` currently re-derives everything from `source` rather than trusting `fix_data`, deliberately, so a caller invoking `fix()` with a stale or hand-built `violations` list can never cause a wrong fix (see `excessive_blank_lines.fix()`'s own reasoning). Caching raw tokens rather than the higher-level scan result wouldn't break that guarantee — tokenizing is a pure function of `source` alone — but the win only applies in `--fix` mode, on files that already have violations, and would blur a currently clean boundary for a small return. Not adopted.
- **Leave every check tokenizing independently, apply only targeted micro-optimizations**: adopted (below).

Before choosing, `ast_checks` was profiled (`cProfile`) running every check, cold and warm cache, against `mongo-python-driver` (426 `.py` files after `.gitignore` exclusion). All tokenize-related work (`tokenize_source`, `find_ignored_lines*`, `ignored_lines_from_tokens`, and the underlying C tokenizer) accounted for ~9.8% of total profiled cold-run time and ~4.1% of warm-run time. The dominant costs are AST tree-walking (each check's own `ast.walk`/`NodeVisitor.visit` pass over the shared tree) and `redundant-type-conversion`'s `ty` LSP round-trips (ADR-0034/ADR-0035) — the latter is the only check that can't be served from cache at all, so it alone accounts for the large majority of warm-run time. A shared-tokenize protocol could only reclaim, at best, the ~10%/~4% ceiling measured above — and less than that in practice, since `redundant-assignment` (prefilter `" = "`) and `misplaced-comment` (prefilter `"#"`) both need real token content, not just suppression-comment positions, for their own core logic on almost every file in a typical repo. Only the smaller remainder held by the other, conditionally-tokenizing checks (`meaningless-vars`, `redundant-super-init`, `excessive-blank-lines`, `redundant-type-conversion`, `validate-function-name`) is genuinely poolable.

## Decision

Do not extend `ASTCheck.check()`/`fix()` to accept shared tokenize output from `CheckOrchestrator`. Every check keeps tokenizing its own file independently, exactly when its own logic needs to, under the existing `ASTCheck.check(filepath, tree, source)` contract (unchanged).

Two low-risk, in-pattern micro-optimizations were applied instead, without touching the protocol:

- `validate-function-name` now defers its `find_ignored_lines` call until at least one AST-cheap-filtered candidate function survives (name prefix, not a decorator override/abstract method, not a simple accessor) — mirroring the same "tokenize only once there's already a reason to" pattern `meaningless-vars`/`redundant-super-init`/`excessive-blank-lines`/`redundant-type-conversion` already use.
- `misplaced-comment` reuses the shared `tokenize_source()` helper instead of duplicating its `StringIO(normalize_for_tokenize(...))` boilerplate inline in both `check()` and `fix()`.

## Consequences

- Every check tokenizing a file it's given, independently of every other check, remains an accepted cost rather than a bug to keep chasing.
- The `ASTCheck` protocol's `check()`/`fix()` signatures, and every check's own tests, are unaffected — no project-wide signature change was needed.
- If a future check is added with a broad prefilter (matching most files) that always needs real token content unconditionally, the addressable "shared tokenize" ceiling shrinks further, not grows.
- This decision should be revisited only if profiling on a real target repository later shows tokenize-related work materially exceeding the ~10% cold-run / ~4% warm-run share measured here — e.g. if the `redundant-type-conversion`/`ty` cost (or the AST-walk cost) is reduced first, tokenizing's relative share of total runtime would grow and could justify the interface cost then.
