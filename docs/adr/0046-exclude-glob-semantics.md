# Match `ruff`'s two exclusion rules, and anchor patterns to where they came from

`--exclude` matched each pattern with `Path.match()`, plus a fallback that excluded a file if any single component of its path matched. `Path.match()` is right-anchored and treats `**` as `*`, which made two behaviors diverge from what a pattern looks like it does:

- `tests/fixtures/**` excluded `tests/fixtures/a.py` but not `tests/fixtures/x/a.py`.
- `vendor/*` excluded `src/vendor/v.py`, because matching from the right ignores everything to its left.

Adding an `exclude` key to `[tool.ruff-extra-rules]` (ADR-0045) turns this from a quirk into a trap: the key looks exactly like `ruff`'s, so a pattern copied between the two would silently exclude a different set of files.

## Decision

Exclusion follows the two rules `ruff` implements, verified against `ruff 0.16.1` rather than inferred from its documentation:

- A pattern with no path separator (`tests`, `*.py`) matches any file or directory of that name anywhere beneath the anchor.
- A pattern containing a separator (`src/vendor/*`) is anchored, `**` is recursive, and matching a directory excludes everything beneath it.

The any-component fallback is kept, not removed: it is what implements the first rule, and dropping it would have been a regression.

Both rules are evaluated in `ruff`'s own glob dialect, which is a third thing this decision has to get right and which `Path.full_match` does not implement: `*` and `?` span path separators, `**` standing alone as a path component also matches zero components, and braces alternate. `.`, `..`, and repeated separators are resolved before the anchored rule is applied, and left alone for the unanchored one — `ruff` resolves a pattern against its anchor but matches a bare name exactly as written, so `./tests/**` covers the whole `tests/` subtree while `./mod.py` covers the root's own `mod.py` and no other. `ruff` is the reference for the pattern language as much as for the two rules above — a pattern copied between the two tools has to cover the same files, and `src/*.py` covering only the direct children of `src/` in one tool and its whole subtree in the other is exactly the trap this decision exists to close. The dialect is shared with `per-file-ignores` (ADR-0049), which applies its own matching rules on top of it.

A pattern carries the directory it is anchored at, rather than being matched against whatever string form a path happened to arrive in. A pattern from `pyproject.toml` anchors at the project root; a pattern from `--exclude` anchors at the working directory. This is `ruff`'s own rule for relative paths in configuration, and it makes exclusion independent of which directory the process was launched from. A file outside its pattern's anchor is never excluded by that pattern, matching `ruff`'s treatment of `exclude` as project-relative.

## Consequences

- Breaking for anyone relying on the previous right-anchored matching, where a pattern with a separator matched any path ending in it. That behavior was undocumented and not covered by any ADR.
- Breaking, too, for a pattern that relied on `*` stopping at a path separator: `migrations/*.py` now excludes the whole subtree rather than only its direct children, as it always did in `ruff`. A pattern that means to match one level has to say so, as it does there.
- A pattern's meaning now depends on its source, since the two anchors differ. Under `prek`/`pre-commit` the two coincide, because the working directory is always the repository root.
- An uncompilable pattern is now a configuration error rather than a pattern that quietly matches nothing.
