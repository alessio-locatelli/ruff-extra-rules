# Switch a check off for some files, matching `ruff`'s own rule

**Update, ADR-0058:** global check selection now has an additive `extend-select` setting; per-file ignores remain replacement-valued and have no `extend-` variant.

Check selection was all-or-nothing for a run: `select` and `ignore` (ADR-0045) apply to every file the hook is given. Real codebases need one check off in `tests/`, in generated or vendored trees, or in `__init__.py`, without losing it everywhere else — and the alternative, an inline suppression comment per occurrence, does not scale to a whole subtree.

`ruff` answers this with `lint.per-file-ignores`, so entries will be copied between the two configurations. That makes matching behavior, not just the key name, part of the decision.

## Decision

`[tool.ruff-extra-rules.per-file-ignores]` maps a file pattern to a list of check ids. `--per-file-ignores <file pattern>:<check>` is the same setting on the command line, comma-separated and repeatable; like every other setting it replaces the configured value rather than extending it. This per-file setting has no `extend-` variant; global check selection's additive `extend-select` is defined by ADR-0058.

An entry applies to a file when its pattern matches the file's own name, or matches the file's path anchored at the pattern's source — the project root for the configuration file, the working directory for the flag, exactly as ADR-0046 anchors `exclude`. A leading `!` inverts that: the entry then applies to every file the pattern does not match. Every applying entry contributes its checks, so entries accumulate rather than the last one winning. This is `ruff`'s rule, verified against `ruff 0.16.1` rather than inferred from its documentation, including the consequence that a bare directory name matches nothing: only a file's own name is matched unanchored.

Patterns are written in `ruff`'s glob dialect, which ADR-0046 now shares: `*` and `?` span path separators, `**` standing alone as a path component also matches zero components, and braces alternate. A pattern that dialect cannot compile is a configuration error, reported like any other (exit 2, naming its source), rather than silently matching nothing.

A suppressed check does not run on that file at all. Running it and discarding its violations would pay the full cost of the check the configuration just said to skip, and under `--fix` the file would already have been rewritten before anything could be discarded.

One exception: a check that declares `tracks_direct_inputs` is still handed the file's content, wherever its own prefilter would have handed it that file anyway. Suppressing a _report_ in one file must not withhold that file from the cross-file analysis that decides what to report in _other_ files — the gap ADR-0041 exists to close, which a plain "don't run it" would silently reopen. The declaration is explicit rather than inferred from whether a check overrides the lifecycle method, so a file is never read for the benefit of the checks that would do nothing with it.

Cache identity is therefore decided per file, from the cacheable checks that survive its own entries, rather than once per run (ADR-0044). A cached result only means anything alongside the set of checks that produced it, and a file whose entries change simply moves to a different identity. Prefiltering (ADR-0034) deliberately does not participate: a prefiltered-out check genuinely has nothing to report for that file, so its absence is already part of what a cache entry means.

## Considered Options

- **Run every check and drop the suppressed violations afterwards**: rejected. It is the cost of the check without the benefit, and it cannot stop `--fix` from writing.
- **Fold the whole table into one run-wide cache identity**: rejected. Correct, but any edit to any pattern would then invalidate every file's cached result, and a table that differs between branches would thrash the cache for files no entry ever touched.
- **Reuse `exclude`'s matching rule**: rejected. `exclude` matches a separator-less pattern against _any_ path component, so directory names work there; `ruff`'s per-file-ignores matches only the file's own name, and adopting the wrong one of the two would silently change which files a copied entry covers.
- **Accept only check ids the running entry point can run**: rejected for the reason ADR-0045 already settled — one table serves both published hooks.

## Consequences

- Different files in one run can now be checked by different sets of checks, so "which checks ran" is no longer answerable from `select`/`ignore` alone.
- A pattern that names no existing file is accepted silently, as in `ruff`; a typo in a _pattern_ is not detectable, while a typo in a _check id_ is and exits 2.
- Only the anchored half of the rule is anchored. A file named on the command line from outside the project still matches a name-only entry, and still picks up every negated entry, because it matches neither half of those. This is `ruff`'s behavior and follows from matching a bare file name unanchored at all; `exclude` (ADR-0046) is the setting that keeps a file out of the run entirely.
- A pattern containing a comma — brace alternation included — cannot be written on the command line, since the flag's own list separator claims it first. `ruff`'s flag has the same limitation; the configuration file has neither.
- Existing cache entries stay valid: with no entries configured, a file's identity is exactly the run-wide one this replaced.
