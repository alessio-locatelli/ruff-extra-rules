# `ruff-extra-rules-ty` checks the files it is given, not the whole project

`redundant-type-conversion` (TR6) is this project's only cross-file check: whether a conversion is redundant can depend on a type declared in another file (ADR-0041). `ty`'s own pre-commit hook faces the same property and resolves it the opposite way — it sets `pass_filenames: false` and `always_run: true`, checking the entire project on every commit, on the stated grounds that a change in one file can produce diagnostics in another and that passing only the changed file would give a false sense of security.

`ruff-extra-rules-ty` passes filenames. Nothing recorded that as a decision, so a reader who knows `ty`'s hook would reasonably read it as an oversight.

## Decision

The hook checks the files `prek`/`pre-commit` gives it. Cross-file awareness comes from the persistent `ty` daemon accumulating knowledge of files as commits pass them through it, not from re-examining the whole project each run.

The cost of the alternative is what decides it. A whole-project run pays for every file on every commit, and at the aggressive level TR6 is roughly an order of magnitude more expensive than at conservative, because each additional candidate conversion needs its own `ty` query. That is a per-commit cost proportional to repository size rather than to the diff — the opposite of what the rest of this pipeline is built around, where prefiltering and caching exist specifically to keep per-commit work proportional to what changed.

## Consequences

- The coverage gap this accepts is the one ADR-0041 already records: a conversion depending on a file the daemon has never examined is not caught until that file passes through at least once. A full-tree run (`prek run --all-files`) closes it immediately, which is what `docs/rules/redundant-type-conversion.md` tells users to do.
- A commit that only deletes a file runs the hook against no Python files at all, so a conversion elsewhere that the deletion makes necessary again is not re-examined until something else touches it.
- Reversing this later means adopting `pass_filenames: false` and `always_run: true`, and the daemon's per-file caching would absorb much of the repeat cost. The trade-off should be re-measured rather than assumed if TR6's per-file cost changes materially.
