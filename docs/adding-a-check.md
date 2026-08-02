# Adding a new AST check

Checks live under `src/pre_commit_hooks/ast_checks/` and plug into the grouped `ruff-extra-rules` hook — there is no per-check `.pre-commit-hooks.yaml` entry or console script to add.

## 1. Design

- Purpose: one check, one responsibility.
- Check id: kebab-case (e.g. `no-bare-except`).
- Error code: `TR<N>` (next unused number). `test_all_checks_have_unique_check_ids_and_error_codes` (`tests/test_orchestrator.py`) fails loudly if a new check's id or code collides with an existing one — see `docs/adr/0021-behavioral-contract-audit-rule-isolation-python-compat.md`.
- Violation message format and whether the check needs an autofix mode.

For the general prefilter-then-parse pipeline shape, see AGENTS.md's "Suggested Check Architecture". Concretely for this repo: almost nothing qualifies for a grep-only check, because every existing check needs to distinguish syntax context that only an AST gives you — e.g. `meaningless-vars` must tell `data = 1` (violation) apart from `obj.data = 1` (attribute, fine) and `"data = 1"` (inside a string, fine), and must catch `def foo(data):` (a parameter, not an assignment) that grep would miss entirely. Use `get_prefilter_pattern()` for a cheap `git grep` pass to skip files that can't possibly match, then do the real detection with `ast`.

## 2. Implement

Every check implements the `ASTCheck` protocol (`src/pre_commit_hooks/ast_checks/_base.py`) and should inherit `BaseCheck` from the same module:

```python
class ASTCheck(Protocol):
    @property
    def check_id(self) -> str: ...  # e.g. "meaningless-vars"

    @property
    def error_code(self) -> str: ...  # e.g. "TR1"

    def get_prefilter_pattern(self) -> list[str] | None: ...  # git-grep fast path, None = check every file

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]: ...

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool: ...

    # Optional: register/parse check-specific CLI arguments, e.g. --your-check-option
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None: ...
    def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]: ...
```

`CheckOrchestrator` parses each file's AST **once** and hands the same `tree`/`source` to every enabled check — `check()` must not re-parse the file.

`add_cli_arguments`/`cli_kwargs_from_args` are part of the protocol, so `type[ASTCheck]` (as used by `ALL_CHECKS`) requires both. `BaseCheck` provides a no-op default for each — inherit it (`class YourCheck(BaseCheck):`) unless your check actually needs its own CLI option, in which case override both.

Create `src/pre_commit_hooks/ast_checks/your_check.py` (or a package with `__init__.py` if the check needs multiple modules — see `validate_function_name/` for an example). Register the class in `ALL_CHECKS` in `src/pre_commit_hooks/ast_checks/__init__.py`. That's the whole registration step — no `.pre-commit-hooks.yaml` entry and no `[project.scripts]` entry. The check becomes selectable via `--select=your-check`/`--ignore=your-check` on the `ruff-extra-rules` hook and shows up in `python -m pre_commit_hooks.ast_checks --list-checks`.

**Requirements:**

- Standard library only, no external runtime dependencies.
- Never touch text inside string/byte literals or comments when writing an autofix — locate targets via AST node positions (`node.lineno`/`node.col_offset`/`node.end_lineno`/`node.end_col_offset`), not blind regex substitution over the whole file. See `validate_function_name/autofix.py` for a worked example of AST-scoped renaming.
- Support inline suppression: `# pytriage: TR<N>` (also matches as one entry in a comma-separated list, e.g. `# pytriage: TR1,TR5`).
- If the check is experimental or prone to false positives, keep it out of the default-enabled set via `args: [--ignore=your-check-id]` in `.pre-commit-hooks.yaml`.
- Write fixed content via `atomic_write_text(path, content, encoding, expected_source)` (`_base.py`), never a direct `open()`/`Path.write_text()`. `expected_source` must be the exact source string this write's edits were computed against — immediately before the rename that would commit the write, `atomic_write_text()` re-reads `path`'s current on-disk bytes and compares them (encoded via `encoding`) against `expected_source`, refusing the write (via `ConcurrentModificationError`) if something else changed the file since it was read. It also validates that `content` parses as Python before doing any of that, refusing (via `FixValidationError`) rather than ever writing broken syntax to disk. Both exceptions leave `path` itself untouched, and both follow the same split: if your `fix()` writes once per call (most checks), let either exception propagate uncaught — `CheckOrchestrator._apply_fixes` attributes the rejection to the check's violations for you. If it writes more than once per call, looping over violations individually (like `validate_function_name`), catch both around each write and call `mark_fix_rejected()`/`mark_fix_aborted()` on that specific violation so a later write in the same call still gets attempted. See `docs/adr/0010-fix-validation-before-write.md` and `docs/adr/0042-abort-fixes-on-concurrent-source-modification.md`.
- Catch `OSError` around your own `atomic_write_text()` call (missing parent directory, permission denied, disk full) and return `False` — don't let it propagate. `ASTCheck.fix()`'s contract is "`True` if fixes were applied, `False` otherwise," not "or raises"; every shipped check with autofix follows this (see `meaningless_vars.fix()`, `excessive_blank_lines.fix()`, or `validate_function_name/autofix.apply_fix()` for the pattern). `CheckOrchestrator._apply_fixes`'s own outer `except Exception` will mask a missed case when running through the full pipeline, but calling your check's `fix()` directly (as unit tests do) will raise instead of returning `False`. See `docs/adr/0011-behavioral-contract-audit-fix-engine.md`.
- Any other exception your `fix()` raises (a genuine bug, not `FixValidationError` and not a caught `OSError`) is caught by `CheckOrchestrator._apply_fixes` and reported to the user as `[FIX ERRORED]`, distinct from `[FIX REJECTED]` — you don't need to do anything extra for this, just don't swallow exceptions yourself in a way that would hide them as a plain unfixed violation. See `docs/adr/0012-behavioral-contract-audit-internal-errors-exit-codes.md`.

## 3. Write tests

Create `tests/test_your_check.py` using `tmp_path` and `pytest.mark.parametrize` (see `tests/test_misplaced_comment.py` for the idiomatic pattern used here). Required coverage:

- Detection: true positives across the patterns the check targets.
- No false positives on idiomatic code the check should leave alone.
- Inline suppression (`# pytriage: TR<N>`).
- Autofix, if implemented — including that it never mutates unrelated text (string literals, comments, identically-named symbols in unrelated scopes).

For larger example files, add fixtures under `tests/fixtures/your_check/`, following the `good/`/`bad/`/`ignore/` (and `autofix/`, if relevant) convention used by `tests/fixtures/validate_function_name/`.

## 4. Document the check

Add `docs/rules/your-check.md` (why it exists, a short example, suppression syntax — follow the format of an existing page like `docs/rules/meaningless-vars.md`), then add a row for it to the table under README.md's "Available Checks".

## 5. Validate

Run linters, tests, coverage.

## Conventions

For the error message format (shown to the user), follow the up-to-date Ruff format. Do not reinvent the wheel.

## Performance

The existing optimizations a new check should reuse rather than reimplement:

- **`_cache.py`**: a SHA-1 + mtime disk cache (like mypy/ruff's), keyed per file, so an unchanged file isn't re-analyzed on the next run.
- **`_prefilter.py`**: a `git grep`-based pass that skips files that can't possibly match before any Python parsing happens. `git_grep_filter()` always keeps a file it can't confirm is readable (missing, permission-denied) as a candidate regardless of what `git grep` itself reports for it — never trust silence from a prefilter as proof a file doesn't need checking. See `docs/adr/0015-behavioral-contract-audit-file-discovery-path-handling.md`. It also searches with `--untracked --no-exclude-standard`, so a file passed explicitly (by this hook's own CLI, or by pre-commit/prek) is always actually examined regardless of whether it's been `git add`ed or matches `.gitignore` — an explicit file argument is always in scope, whatever its VCS status. See `docs/adr/0024-behavioral-contract-audit-git-vcs-integration-and-security.md`.
- **`CheckOrchestrator`**: parses each file's AST once per run and hands the same `tree`/`source` to every enabled check.

Guidelines:

- Let `CheckOrchestrator` read/parse each file once; don't re-read in `check()`/`fix()`.
- Prefer O(n) over O(n²); use set lookups instead of list searches.
- `prek`/`pre-commit` may run hooks in parallel across files — avoid shared mutable state without locking (see `_cache.py`).

```bash
uv run python scripts/benchmark.py --iterations=3 --clear-cache  # measures this repo's own src/+tests/
python -m cProfile -o profile.stats -m pre_commit_hooks.ast_checks --select=your-check src/
python -c "import pstats; p = pstats.Stats('profile.stats'); p.sort_stats('cumulative'); p.print_stats(20)"
```

Each check invocation pays Python interpreter startup once per subprocess, which tends to dominate over per-file analysis cost at this repo's current size — don't trust a single run's cold-vs-warm percentage as a stable signal, the sign can flip between runs. Re-run `scripts/benchmark.py` yourself to get current numbers rather than relying on a hardcoded figure in docs. Cache location: `.cache/pre_commit_hooks/` (safe to delete).

**Incremental-analysis limitations**: a `check()` implementation must only ever look at the single file it's given (`filepath`/`tree`/`source`) — no check reads another file, imports, or any other cross-file state — so a cache hit for an unchanged file always reproduces exactly what a full re-run of that file would produce. Keep it that way: a check that started inspecting other files would silently break this guarantee, since the cache key (`CheckOrchestrator._generate_cache_key()`) has no way to invalidate one file's cached result because a _different_ file it depended on changed. `redundant-type-conversion` (TR6) is the one documented exception, and it doesn't break this guarantee: it's marked `cacheable = False` (see `ASTCheck.cacheable`, ADR-0034) so its own results are never cached in the first place, and it reports files outside the ones it was given through a separate, explicit extension point instead (`ASTCheck.drain_cross_file_candidates`, ADR-0041) rather than by reading cross-file state from inside `check()` itself. A new check needing similar cross-file awareness should use that same extension point rather than reaching outside the file it's given from within `check()`.
