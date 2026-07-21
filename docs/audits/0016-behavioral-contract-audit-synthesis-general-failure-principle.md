# Investigation report: behavioral contract audit synthesis — General Failure Principle pass (ch. 35)

Detailed findings behind `docs/adr/0027-behavioral-contract-audit-synthesis.md`. `docs/behavioral_contract.md` chapter **35** (General Failure Principle) was audited by applying its 10-question rubric to the codebase as a whole, after all 15 chapter-scoped audits (issues #30–#44, covering chapters 1–32, 34, and part of 33) had closed. Per issue #45, this pass's job was not to re-audit any single chapter, but to catch whatever fell in the seam between two chapter-scoped audits: a decision that was locally correct within one audit's narrow scope but produces an inconsistent or silently-wrong result once combined with a decision made by a different audit.

Method:

1. Confirmed all 15 blocking tickets (#30–#44) are closed.
2. Read all 15 ADRs (`docs/adr/0011`–`0026`) to build the chapter-by-chapter disposition below.
3. Read the core pipeline end to end as one system — `_base.py`, `_orchestrator.py`, `_cache.py`, `__main__.py`, `_cli.py`, `_prefilter.py`, `_discovery.py`, `_diagnostics.py`, `ast_checks/__init__.py` — rather than per-chapter, specifically looking for two subsystems whose individually-reasonable behaviors disagree once combined.
4. Applied chapter 35's own 10-question rubric directly to the fix-application pipeline (`_check_file()` → `_apply_fixes()` → `_refresh_stale_positions()` → `atomic_write_text()`).
5. Verified every candidate gap with a live reproduction against the current code before treating it as real, rather than trusting a docstring's own claim.

## Chapter disposition (1–34)

| Ch. | Topic                                     | Resolved by                            | Outcome                                                                         |
| --- | ----------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------- |
| 1   | Correctness/Safety of Auto-Fixes          | #30 / `adr/0011`                       | Fix                                                                             |
| 2   | Preservation of Source Semantics          | #39 / `adr/0023`                       | Fix                                                                             |
| 3   | Source File Integrity                     | #30 / `adr/0011`                       | Fix                                                                             |
| 4   | Parsing and Invalid Python                | #30 / `adr/0011`                       | Fix                                                                             |
| 5   | Internal Errors and Failure Isolation     | #31 / `adr/0012`                       | Fix                                                                             |
| 6   | CLI Exit Codes                            | #31 / `adr/0012`                       | Fix (docstring/contract clarified)                                              |
| 7   | Diagnostics and User Feedback             | #41 / `adr/0017`                       | Fix                                                                             |
| 8   | Auto-Fix Modes                            | #41 / `adr/0017`                       | Fix                                                                             |
| 9   | Determinism                               | #40 / `adr/0025`                       | Fix                                                                             |
| 10  | Idempotence                               | #40 / `adr/0025`                       | Test-only                                                                       |
| 11  | Caching                                   | #32 / `adr/0014`                       | Fix                                                                             |
| 12  | Incremental Execution                     | #33 / `adr/0015`                       | Fix, plus a cross-cutting gap found by this pass — see below                    |
| 13  | Filesystem and Path Handling              | #33 / `adr/0015`                       | Already correct (reconfirmed)                                                   |
| 14  | Cross-Platform Behavior                   | #42 / `adr/0020`                       | Fix; rest N/A (Linux/WSL-only by design)                                        |
| 15  | Process, Signals, and Cancellation        | #34 / `adr/0016`                       | Fix                                                                             |
| 16  | Parallelism and Concurrency               | #34 / `adr/0016`                       | N/A (no in-process concurrency exists)                                          |
| 17  | Configuration                             | #35 / `adr/0019`                       | Fix; rest N/A (no config file exists)                                           |
| 18  | Rule and Plugin Isolation                 | #36 / `adr/0021`                       | Test-only; plugin premise N/A                                                   |
| 19  | Python Version and Language Compatibility | #36 / `adr/0021`                       | N/A (no target-version concept)                                                 |
| 20  | Parsing, AST, CST, and Source Mapping     | #30 / `adr/0011`                       | Fix                                                                             |
| 21  | Formatting and Source Preservation        | #39 / `adr/0023`                       | Fix                                                                             |
| 22  | Git and VCS Integration                   | #43 / `adr/0024`                       | Fix, plus the same cross-cutting gap noted under ch. 12                         |
| 23  | Security                                  | #43 / `adr/0024`                       | Fix                                                                             |
| 24  | Resource Usage                            | #37 / `adr/0013`                       | Fix; remainder accepted as documented tradeoffs                                 |
| 25  | Timeouts and Hanging Operations           | #37 / `adr/0013`                       | Fix                                                                             |
| 26  | Standard Input, Output, and TTY Behavior  | #38 / `adr/0022`                       | N/A (no TTY interaction exists)                                                 |
| 27  | Logging and Debugging                     | #38 / `adr/0022`                       | Fix                                                                             |
| 28  | Configuration and Environment Discovery   | #35 / `adr/0019`                       | Fix (documentation); rest N/A                                                   |
| 29  | Installation and Runtime Environment      | #38 / `adr/0022`                       | N/A (zero runtime deps; version gate would be a forbidden compatibility branch) |
| 30  | Performance                               | #37 / `adr/0013`                       | Fix                                                                             |
| 31  | Testing Requirements                      | #44 / `adr/0026`                       | Test-only; 2 items N/A                                                          |
| 32  | Testing the Auto-Fix Pipeline             | #44 / `adr/0026`                       | Test-only                                                                       |
| 33  | Compatibility and Upgrade Behavior        | #32 / `adr/0014` (partial) + this pass | Split — see below                                                               |
| 34  | User Trust                                | #41 / `adr/0017`                       | Fix                                                                             |

## Cross-cutting gap found: directory expansion vs. explicit-file discovery disagree on untracked files

`ast_checks/_discovery.py`'s `_list_python_files_in_dir()` (added by #33/`adr/0015`, ch. 12/13, to expand a directory CLI argument into the `.py` files under it) calls plain `git -C <dir> ls-files -z` (`src/pre_commit_hooks/ast_checks/_discovery.py:85`), which lists only tracked files. `_prefilter.py`'s `git_grep_filter()` (hardened by #43/`adr/0024`, ch. 22/23, to search an explicitly-named file regardless of its VCS status) passes `--untracked --no-exclude-standard` specifically so a file named directly on the command line is never silently dropped for being untracked or `.gitignore`d (`src/pre_commit_hooks/_prefilter.py:60-74`).

Each decision was correct within its own audit's scope: #33 predates #43 and deliberately excluded untracked scratch files from a directory scan (avoiding sweeping in build artifacts); #43 later closed the identical "silently drop an untracked file with a false-clean result" failure mode, but only for the explicit-argument path, since that was the ticket's scope. `adr/0024` went further and explicitly concluded the directory-scan asymmetry this left behind was "not the same gap." Viewed per-chapter, in isolation, that conclusion was reasonable — but viewed as one system, the two paths now disagree about whether the same file is in scope, and the narrower path (directory expansion) gives no diagnostic that anything was skipped, which is exactly the failure mode `adr/0024` itself fixed for the other path. This synthesis pass therefore revises that specific conclusion in `adr/0024`; `docs/adr/0024` has been amended with a forward pointer to this report and to `adr/0027` so a future reader doesn't find two ADRs in direct, unresolved contradiction.

Reproduced against the current code: with `src/tracked.py` staged (`data = 1`) and `src/untracked.py` not staged (`result = 2`) in a fresh git repository —

```
$ uv run python -m pre_commit_hooks.ast_checks --select=forbid-vars src/untracked.py
src/untracked.py:1:1: TRI001: Forbidden variable name 'result' found. ...
(exit 1)

$ uv run python -m pre_commit_hooks.ast_checks --select=forbid-vars src/
src/tracked.py:1:1: TRI001: Forbidden variable name 'data' found. ...
(exit 1 -- but only because of tracked.py; untracked.py's own violation is never reported)
```

If `tracked.py` had no violation of its own, the directory-argument run would exit 0 — a false-clean result for a file that was never examined, and precisely the failure mode `adr/0024` fixed for the sibling code path. This also matches ch. 35's own general rule directly: a "no change" (skip the file) with no visible warning is exactly "try something, ignore errors, and report success," not "fail visibly."

This is also the exact workflow `AGENTS.md`'s own documented dev commands use (`uv run python -m pre_commit_hooks.ast_checks --select=forbid-vars,validate-function-name src/`) — the command a solo maintainer runs directly against a new, not-yet-staged file, before `git add`ing it.

Filed as [issue #67](https://github.com/alessio-locatelli/ruff-extra-rules/issues/67) rather than fixed inline here, per #45's own acceptance criteria ("Any cross-cutting gap found gets its own follow-up issue (not fixed inline in this ticket)").

## Chapter 33's residual disposition

`adr/0014` (#32) scoped itself to "chapter 33 ... only for its cache-invalidation-on-version-change portions ... the rest of ch. 33 is CLI/config UX and release process, not caching" and no later ticket picked up the remainder — an audit-trail completeness gap, not a behavior gap, closed here rather than by a follow-up issue since no code change results:

- **"MUST define how behavior changes between tool versions"** and **"MUST document behavior changes that can produce large source diffs"**: satisfied by this audit series' own existing practice, not a dedicated changelog — every behavior change chapters 1–34's audits made is recorded in its own ADR (`docs/adr/0011`–`0026`) with a linked, detailed audit report (e.g. `adr/0023`'s `forbid_vars` rewrite, which can change which names get renamed across a whole file, is documented exactly this way). No `CHANGELOG.md`/`HISTORY.md` exists in the repository, and this project's own GitHub releases have empty bodies — confirmed by `git ls-files | rg -i changelog` (no match) and `gh release view v0.0.31 --json body` (empty). No dedicated changelog is warranted: `AGENTS.md` states this is a personal hobby project "not used by anyone else," so there's no external consumer a changelog would need to inform.
- **"MUST avoid silently changing auto-fix semantics in a patch release unless the compatibility policy permits it"**: satisfied by a declared policy — `AGENTS.md` states "Breaking changes are allowed and expected" for this project's own hook ids/CLI surface, which is this project's compatibility policy, and it explicitly permits a behavior change at any release granularity.
- **"MUST provide a migration path for incompatible configuration changes"** and **"MUST avoid silently interpreting old configuration according to a materially different meaning"**: N/A — no configuration file exists anywhere in this pipeline (`adr/0019`/#35 confirmed this by grep: no `tomllib`, no config parsing); there is nothing to migrate or reinterpret.
- **"MUST ensure that serialized internal data is versioned or safely invalidated when its format changes"** and **"MUST ensure that old cache data cannot silently produce incorrect results after an incompatible upgrade"**: already satisfied — `CacheManager`'s own `cache_data["version"]` field is checked against `cache_version` on every read and discarded on mismatch (`_cache.py`'s `get_cached_result`/`set_cached_result`), and `cache_version` itself is computed from a hash of the package's own source tree (`adr/0005`, reconfirmed by `adr/0014`), so any upgrade that changes behavior necessarily changes the cache key. Predates this audit series; reconfirmed correct here, not newly fixed.

## Chapter 35's own rubric, applied to the fix-application pipeline as a whole

The pipeline's one operation that can modify source code is `CheckOrchestrator._apply_fixes()` (with `_refresh_stale_positions()` and `atomic_write_text()` as its own sub-operations). Walking the 10 questions against it as a single system, rather than per-check:

1. **Inputs**: a list of already-parsed violations, the file's current on-disk content (re-read fresh before every check's own fix, ch. 1/`adr/0011`), and each check's own `fix()` implementation.
2. **What can fail**: a re-read/re-parse race if the file changed concurrently; `fix()` itself raising; `atomic_write_text()` rejecting the result as invalid Python; an `OSError` during the write.
3. **Data corruption**: ruled out structurally — `atomic_write_text()` validates via `compile()` before any file I/O and writes through a temp-file-then-`replace()` rename, so the target is always either fully old or fully new (ch. 1/3/20, `adr/0011`).
4. **Incorrect lint result**: every failure mode above is caught, isolated per check, and recorded via `rule_failures`/`mark_fix_rejected`/`mark_fix_errored`/`mark_fix_failed` — never silently absorbed (ch. 5/7/8, `adr/0012`/`0017`).
5. **Detectable**: yes for everything inside `_apply_fixes()` itself — each outcome gets its own tag (`[FIXED]`/`[FIX REJECTED]`/`[FIX ERRORED]`/`[FIX FAILED]`) and forces a non-zero exit. **Not** yet true one layer earlier, at file discovery — the ch. 12/22 gap above is precisely a case where a failure (the file never entering the candidate set at all) is undetectable by construction.
6. **Safe fallback**: atomic per write, not per batch — `atomic_write_text()` never partially writes a single file replacement. A check that writes once per `fix()` call therefore leaves every violation it touched exactly as found on rejection/error/failure. A check that writes more than once per call (looping over violations individually, e.g. `validate_function_name`) can have already committed some of that call's renames before a later one in the same call is rejected or errors (`adr/0011`'s documented contract for such checks) — the orchestrator's own post-fix recheck (`_mark_resolved_and_get_still_present()`) re-verifies the file's actual state afterward and marks each already-committed violation `[FIXED]` rather than leaving it misreported as untouched, so the fallback is "every write is individually safe and its true outcome is always reported," not "the whole batch rolls back together."
7. **User visibility**: yes, via the report's per-outcome tags and hints (`_diagnostics.py`), except for the discovery-layer gap above.
8. **Repeatability**: yes — `test_fix_converges_after_one_pass_across_all_checks` (`adr/0025`) proves a second `--fix` pass changes nothing once the first has run.
9. **Interruptibility**: yes — SIGINT/SIGTERM both unwind through the same `try`/`finally`-guarded temp-file cleanup, stopping at the next safe per-file boundary rather than mid-write (ch. 15, `adr/0016`).
10. **Independent validation**: yes — `atomic_write_text()`'s `compile()` gate is itself the independent validator every fix's output must pass, regardless of which check produced it (ch. 1, `adr/0011`).

The one place this rubric doesn't fully hold is exactly the gap found above: a file that never becomes a candidate in the first place has no failure to detect, so question 5 ("can the failure be detected?") and question 7 ("will the user know?") both fail — not because any individual operation lacks error handling, but because two independently-correct discovery decisions combine to remove the file from scope before any of the well-audited machinery above ever sees it.

## Other cross-cutting interactions checked and ruled out

- **SIGTERM handler vs. cache lock (`_locked()`)**: a `KeyboardInterrupt` raised while a process holds the cache's advisory `flock` (during `_write_cache()`'s `json.dump()`, or while polling for the lock) still unwinds through `_locked()`'s own `try`/`finally` and the enclosing `with lock_file.open(...)`, releasing the lock and cleaning up the temp file either way (ch. 15 × ch. 11 interaction) — no stale lock, no partial cache file.
- **Actual production entry point vs. the SIGTERM fix's own test coverage**: `adr/0016` added the SIGTERM handler in `ast_checks/__main__.py`. Confirmed `.pre-commit-hooks.yaml`'s `entry: python -m pre_commit_hooks.ast_checks` and `README.md`'s own statement that `[project.scripts]` is deliberately empty both route every real invocation (pre-commit/prek and direct CLI alike) through `__main__.py`, so the fix applies to the one production entry point, not just a module-invocation path nothing actually uses.
- **`--exclude` vs. `expand_directories()` ordering**: `_cli.py` expands a directory argument before applying `--exclude`, so `--exclude` still matches against the resulting individual file paths rather than being defeated by an unexpanded directory argument — already correct, no gap.
- **Diagnostic ordering across checks within one file**: confirmed (already covered by `adr/0025`'s ch. 9 audit) that the per-file violation order is deterministic (fixed check iteration order, not sorted by line/col across checks) but not identical to `ruff`'s own line-sorted convention. Not a behavioral-contract violation — chapter 9 requires determinism, not any particular sort order — so not filed as a gap, just noted here as considered.

## Consequences

- No production code changed by this ticket itself.
- One real cross-cutting gap is tracked as [issue #67](https://github.com/alessio-locatelli/ruff-extra-rules/issues/67).
- Chapter 33's disposition is now complete (see above); no further ticket needed for it.
- This report is the closing reference for the full 35-chapter behavioral contract audit series (issues #30–#45): every chapter now has a recorded disposition, in `docs/adr/0011`–`0027` and their linked `docs/audits/0001`–`0016` reports.
