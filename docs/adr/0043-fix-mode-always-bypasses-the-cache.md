# `--fix` always bypasses the cache, even for a file with nothing to fix

## Context

`CheckOrchestrator._process_single_file()` reads from and writes to the per-file cache (`_cache.py`) only when `fix_mode` is `False`; `--fix` always re-parses and re-runs every cacheable check on every file, for every run, regardless of whether that file ends up having anything to fix. This was never a documented decision — the code comment just states what happens ("Skip the cache in fix mode, since the file will be modified"), not that it applies unconditionally to every file in the batch, including ones that turn out to have zero violations and are never actually written to.

Measured from the repository root against this repository's own `src/` (34 files, no violations for any enabled check, so `--fix` never performs a single write; `redundant-type-conversion` excluded via `--ignore` since its own `ty`-daemon cost is a separate, already-documented factor — ADR-0041). Cache cleared via `rm -rf .cache/pre_commit_hooks` immediately before each run 1 below.

Direct CLI, check only (`uv run python -m pre_commit_hooks.ast_checks --ignore=redundant-type-conversion src`), and `--fix` (`uv run python -m pre_commit_hooks.ast_checks --ignore=redundant-type-conversion --fix src`):

| Run         | check only | `--fix` |
| ----------- | ---------- | ------- |
| 1 (cleared) | 0.367s     | 0.375s  |
| 2           | 0.092s     | 0.347s  |
| 3           | —          | 0.350s  |

`prek run local-ruff-extra-rules-default --all-files` (this repo's own dev-code hook — `--fix` is always on; `.pre-commit-config.yaml` has no check-only equivalent to pair it against):

| Run         | `--fix` |
| ----------- | ------- |
| 1 (cleared) | 0.593s  |
| 2           | 0.566s  |
| 3           | 0.537s  |

A plain check run gets ~4x faster once the cache is warm (0.367s → 0.092s). `--fix` never gets that jump, through either invocation path — it stays within a few percent of the cold check-only cost on every run (0.375s → 0.347s → 0.350s direct; 0.593s → 0.566s → 0.537s via prek). The small, gradual reduction `--fix` does show run-over-run is consistent with ordinary OS-level filesystem/page-cache warming, not this project's own per-file result cache: `--fix` never consults that cache, so the ~4x shortcut a warm check-only run gets from it is never available, though incidental system-level effects outside this project's own control can still shave a few percent off any repeated run, `--fix` included.

## Considered Options

- **Leave `--fix` unconditionally bypassing the cache, document it as a known limitation**: adopted. This is the current, already-shipped behavior; this ADR records it rather than changing it.
- **Read the cache first even in `fix_mode`, trusting a cache hit for the cacheable group while still running `always_rerun_checks` fresh — `_process_single_file()` already does exactly this outside `fix_mode` (a cache hit doesn't short-circuit an always-rerun check like `redundant-type-conversion`) — and bypass the cache only for a file where either group's result turns up a violation**: not implemented, not decided against with a verified rationale — no ADR, commit, or comment predating this one explains why the current code takes the simpler, unconditional-bypass path instead. It stays a plausible future optimization rather than a rejected option, since fabricating a rationale here would misrepresent it as a considered trade-off when it is not documented anywhere as one.

## Decision

Document the existing behavior rather than change it: `CheckOrchestrator` treats `fix_mode=True` as reason enough to skip the cache entirely, for every file, before running a single check — not because every file will be modified (most files in a typical `--fix` run are not), but because knowing whether a given file needs fixing at all requires running its checks in the first place, and the current code does not attempt to answer that question from a cache hit first.

## Consequences

- `--fix` costs close to what a cold check run costs, for every file, on every invocation: the ~4x shortcut a warm check-only run gets from this project's own cache is never available to it, regardless of how many prior runs already confirmed the tree is clean. Measured above: `--fix` ran ~3.8x slower than a warm check-only run on this project's own `src/` tree (0.347s vs 0.092s).
- This is now a documented, known limitation (`docs/faq.md`) rather than an unstated implementation detail a user could only discover by profiling it themselves, as this ADR's own measurement required doing.
- No behavior changes: `--fix`'s own correctness (ADR-0042's concurrent-modification handling, per-file fix locking) is unaffected either way, since none of it depends on whether the cache was consulted first.
- The unimplemented "trust a clean cache hit even in fix mode" optimization from the options above remains open for a future change; this ADR does not authorize or design it, only records that the current cost is deliberate-by-inaction, not a bug to silently work around.
