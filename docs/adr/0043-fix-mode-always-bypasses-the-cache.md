# `--fix` always bypasses the cache, even for a file with nothing to fix

## Context

`CheckOrchestrator._process_single_file()` reads from and writes to the per-file cache (`_cache.py`) only when `fix_mode` is `False`; `--fix` always re-parses and re-runs every cacheable check on every file, for every run, regardless of whether that file ends up having anything to fix. This was never a documented decision — the code comment just states what happens ("Skip the cache in fix mode, since the file will be modified"), not that it applies unconditionally to every file in the batch, including ones that turn out to have zero violations and are never actually written to.

Measured directly against this repository's own `src/` (34 files, no violations for any enabled check, so `--fix` never performs a single write):

| Command                                                           | Cold   | Warm (2nd run)                              |
| ----------------------------------------------------------------- | ------ | ------------------------------------------- |
| `--select=validate-function-name` (check only)                    | 0.140s | 0.074s                                      |
| `--select=validate-function-name --fix`                           | 0.128s | 0.116s (no improvement on a 3rd run either) |
| default checks, `redundant-type-conversion` excluded (check only) | 0.379s | 0.102s                                      |
| default checks, `redundant-type-conversion` excluded, `--fix`     | 0.369s | 0.357s (no improvement on a 3rd run either) |

A plain check run gets ~1.9x–3.7x faster once the cache is warm, scaling with how much of the enabled check set's own cost the cache would otherwise avoid. `--fix` never sees that improvement, on any file, even across repeated consecutive runs on an already-clean tree — it costs the same as a cold check run every single time.

## Considered Options

- **Leave `--fix` unconditionally bypassing the cache, document it as a known limitation**: adopted. This is the current, already-shipped behavior; this ADR records it rather than changing it.
- **Read the cache first even in `fix_mode`, and only bypass it for a file whose cached result already has violations (the only case that could actually need a write)**: not implemented, not decided against with a verified rationale — no ADR, commit, or comment predating this one explains why the current code takes the simpler, unconditional-bypass path instead. It stays a plausible future optimization rather than a rejected option, since fabricating a rationale here would misrepresent it as a considered trade-off when it is not documented anywhere as one.

## Decision

Document the existing behavior rather than change it: `CheckOrchestrator` treats `fix_mode=True` as reason enough to skip the cache entirely, for every file, before running a single check — not because every file will be modified (most files in a typical `--fix` run are not), but because knowing whether a given file needs fixing at all requires running its checks in the first place, and the current code does not attempt to answer that question from a cache hit first.

## Consequences

- `--fix` costs roughly what a cold check run costs, for every file, on every invocation — never less, regardless of how many prior runs already confirmed the tree is clean. Measured above: 1.6x–3.6x slower than a warm check-only run on this project's own `src/` tree.
- This is now a documented, known limitation (`docs/faq.md`) rather than an unstated implementation detail a user could only discover by profiling it themselves, as this ADR's own measurement required doing.
- No behavior changes: `--fix`'s own correctness (ADR-0042's concurrent-modification handling, per-file fix locking) is unaffected either way, since none of it depends on whether the cache was consulted first.
- The unimplemented "trust a clean cache hit even in fix mode" optimization from the options above remains open for a future change; this ADR does not authorize or design it, only records that the current cost is deliberate-by-inaction, not a bug to silently work around.
