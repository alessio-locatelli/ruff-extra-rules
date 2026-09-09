# Changelog

Every change a run of these hooks can show you: new and retired checks, what `--fix` rewrites, the command-line flags and `pyproject.toml` keys, and the output itself. [Releases](docs/releases.md) explains how a version number tells you whether an upgrade can change your results, and what to do about it.

Notes start at 0.0.50. Earlier tags shipped without them.

## [Unreleased]

## [0.4.4] - 2026-09-09

### Fixed

- `redundant-type-conversion` no longer reports a non-exact-match conversion whose result feeds an ordering comparison (`<`/`<=`/`>`/`>=`), not just an equality one, when doing so could raise `TypeError` or silently compare unrelated values at runtime (e.g. `expected <= set(a_tuple)`, where `a_tuple`'s own type isn't a `set`).
- `redundant-type-conversion` `aggressive` mode no longer reports a non-exact-match conversion feeding any comparison operand when the wrapped value's own type has no shared comparison behavior with the constructor (e.g. a `dict` compared as a `set`, a `tuple` compared as a `set`, or an `int` compared as a `float`, which can silently lose precision) — previously only `pathlib`'s own path classes were excluded this way. That family-based allowance doesn't hold for two comparison contexts, which are instead held to an exact-match-only standard: an identity comparison (`is`/`is not`), since only an immutable constructor's own CPython fast path can return the same object for an already-exact-type argument — a mutable one (`list`, `dict`, `set`, `bytearray`) always allocates a new object regardless, changing the comparison's result — and a membership test (`in`/`not in`), since a hash-based container (`set`, `frozenset`, or a `dict`'s keys) can raise `TypeError` on an unhashable query that the family allowance (e.g. accepting a `bytearray` for `bytes`, or a `set` for `frozenset`) would otherwise let through.
- `redundant-type-conversion` no longer reports every conversion in a file as redundant when `ty`'s own project configuration (e.g. `[tool.ty.src] exclude`, commonly used to exempt loosely-typed test code) puts that file outside `ty`'s own checked scope — a condition under which `ty`'s LSP diagnostics silently come back empty regardless of the file's real content. The check now probes for this and skips such a file with a warning instead.

## [0.4.3] - 2026-09-08

### Fixed

- `redundant-assignment` no longer reports (or auto-fixes) a value that is later deleted with `del`, since inlining it would leave the `del` statement with nothing to delete.
- `redundant-assignment` no longer lets a nested function's own local variable of the same name suppress detection of an unrelated, genuinely redundant assignment in the enclosing scope.
- `redundant-assignment --fix` no longer inlines a value into a call whose callee (or, for `obj.method(...)`, the object) isn't provably stable — reassigned, redefined, or otherwise not resolvable to a single, unshadowed name anywhere in the file — since doing so could change the order in which the value's side effects and the callee lookup run. See [ADR-0061](docs/adr/0061-redundant-assignment-autofix-rebindable-callee-guard.md).
- `redundant-type-conversion` `aggressive` mode no longer treats every non-union hover as a "looser match" by default; it now requires the wrapped value's type to be plausibly related to the constructor (e.g. an `Iterable`/`Mapping`-family type, or the matching scalar family), so wrapping a value with no structural relationship to the constructor (a custom class in `str(...)`, an unrelated literal) no longer reaches the expensive `ty` diagnostics-diff stage.
- `redundant-type-conversion` no longer reports a conversion as redundant when the file being checked falls outside the `ty` session's own project root, a condition under which `ty` silently returns no diagnostics for the file in every state and every conversion in it looked "verified safe" by construction. The check now skips such a file and logs a warning instead.
- `redundant-type-conversion` no longer reports a non-exact-match conversion whose result (directly, or via the variable it's assigned to) is later interpolated into an f-string, `str.format()` call, or `%`-formatting expression, since two types can be interchangeable to `ty` while producing different `repr()`/`str()` output once interpolated.

## [0.4.2] - 2026-09-07

### Fixed

- `redundant-type-conversion` now runs its `ty` compatibility check in an isolated workspace, so a project's own
  `ty` source-scope configuration cannot make the hook report a false compatibility failure.

## [0.4.1] - 2026-09-06

### Fixed

- `redundant-assignment` now explains why it reports a value passed to an argument with the same name, instead of presenting a generic immediate-use message.

## [0.4.0] - 2026-09-06

### Added

- Add TR10, `redundant-enum-value`, which reports direct runtime `.value` accesses on directly declared members of
  local `StrEnum` and `IntEnum` classes without mixins, custom `__getattribute__` or `__str__` behavior or construction, later `.value` or
  member `_value_` rebindings, ambiguous name resolution, or unproven member values.

### Fixed

- Git search output from pre-commit checks is now consistently uncolored, improving readability and compatibility in automated environments.

## [0.3.0] - 2026-09-01

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
