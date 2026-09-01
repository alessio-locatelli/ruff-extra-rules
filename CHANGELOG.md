# Changelog

Every change a run of these hooks can show you: new and retired checks, what `--fix` rewrites, the command-line flags and `pyproject.toml` keys, and the output itself. [Releases](docs/releases.md) explains how a version number tells you whether an upgrade can change your results, and what to do about it.

Notes start at 0.0.50. Earlier tags shipped without them.

## [Unreleased]

### Added

- `redundant-dict-get` (TR9) reports local `.get()` calls whose key presence is already proven. It is report-only.
- `redundant-dict-get` recognizes bounded control-flow, aliases, relational containers, and required-`TypedDict` proofs. It keeps literal and variable-key facts distinct, accepts collection membership only from plain loop targets and trusted builtins, and discards facts after rebinding, mutation, unsafe assignments, context-manager exits, `try` `else` transitions, and match fall-through. Its `level` setting defaults to `conservative`; `aggressive` retains direct `dict[...]` annotation heuristics.

### Changed

- Replaced the `permissive` reporting level with `aggressive` for `meaningless-vars`,
  `redundant-assignment`, and `redundant-type-conversion`. Update their command-line flags and
  `pyproject.toml` settings accordingly.
- `redundant-assignment` no longer reports module-level uppercase string constants at the aggressive level, preserving named configuration values that carry a stable meaning beyond their single use.
- `redundant-assignment` now reports an assignment whose only use is `func(name)` — a positional argument echoing the variable's own name — at the default (conservative) level, when `name` binds to a same-named parameter of a `func` defined exactly once, undecorated, in the same file. Ambiguous, decorated, cross-file, or attribute-accessed callees are left alone, since resolving those isn't exact evidence. See [ADR-0060](docs/adr/0060-redundant-assignment-positional-argument-echo.md).

## [0.2.2] - 2026-08-22

### Changed

- `validate-function-name` no longer reports `get_`-prefixed context-manager functions, since their names can describe project-specific resource acquisition.

## [0.2.1] - 2026-08-22

### Added

- `extend-select` enables opt-in checks such as `unused-pytriage` alongside the normal default checks from `pyproject.toml` or the command line, including TR6 suppressions when the dedicated ty hook is installed.

### Changed

- `misplaced-comment` preserves `#:` documentation comments wherever they appear.

## [0.2.0] - 2026-08-21

### Changed

- `unused-pytriage` now retains cached suppression evidence through fix-mode refreshes and audits the final source with the complete active-check context.
- Suppression tracking now covers TR5 markers on assignment lines, so TR8 reports reflect their actual use.
- `--fix` reports whether each fix was applied, declined for safety, rejected, aborted, errored, or failed to write, so an operational failure is visible without being confused with a safety decision.
- `meaningless-vars --fix` now renames enclosing references in class bodies and methods, assigns distinct names to related nested renames, and declines class scopes whose `global` or `nonlocal` declarations cannot be safely rewritten.
- `meaningless-vars --fix` preserves type-alias peer type parameters referenced inside nested scopes while still renaming unrelated enclosing variables.
- `meaningless-vars --fix` preserves peer type-parameter references inside nested generic bounds and signature annotations while still renaming unrelated enclosing variables.

### Added

- `unused-pytriage` (TR8) reports redundant known `# pytriage` suppression codes when explicitly selected; it is report-only and does not auto-fix comments.

## [0.1.0] - 2026-08-16

### Changed

- `misplaced-comment` leaves Sphinx `#:` variable documentation comments trailing multiline module-level assignments, where Sphinx recognizes them, instead of moving them into the expression.
- `redundant-assignment` keeps separately named variables with identical right-hand-side expressions in the same scope, so comparisons can show results from independent evaluations without reports.
- `redundant-assignment` no longer reports a value used only inside a lambda, preserving its capture and evaluation timing.
- `validate-function-name` keeps a rename report-only when the existing name occurs in another tracked Python file, preventing a `--fix` run from leaving cross-file references stale.

## [0.0.51] - 2026-08-16

### Changed

- `redundant-assignment` no longer treats a file differently for living under `tests/`/`test/` or being named `test_*.py`/`*_test.py`. Such a file used to get a quieter version of the check; it now reports exactly what the same code reports anywhere else, at both levels. To keep the check off your tests, list it under `[tool.ruff-extra-rules.per-file-ignores]`. See [ADR-0055](docs/adr/0055-redundant-assignment-ignores-the-file-path.md).
- `redundant-assignment` now reports an assignment whose only use is `func(name=name)` — a keyword argument echoing the variable's own name — at the default (conservative) level, no matter how descriptive `name` looks. The keyword already states the name at the same call site, so it can't be adding information the name alone provides. `func(name)` positional calls are unaffected. See [ADR-0056](docs/adr/0056-redundant-assignment-keyword-argument-echo.md).

## [0.0.50] - 2026-08-15

### Changed

- A `--fix` run now reports a violation that some other fix removed along the way as `[RESOLVED INDIRECTLY]`, instead of leaving it out of the report. Every violation the run found is now accounted for in its output, so a run can print lines for the same files it used to stay silent about. See [ADR-0053](docs/adr/0053-indirect-resolution-outcome.md).
