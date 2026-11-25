# Changelog

Every change a run of these hooks can show you: new and retired checks, what `--fix` rewrites, the command-line flags and `pyproject.toml` keys, and the output itself. [Releases](docs/releases.md) explains how a version number tells you whether an upgrade can change your results, and what to do about it.

Notes start at 0.0.50. Earlier tags shipped without them.

## [Unreleased]

### Added

- Add TR13, `overly-broad-return-type`, which reports an abstract return annotation such as `Mapping[str, int]` or `Sequence[int]` when every `return` in the function produces the same concrete type, such as `MappingProxyType` or `list`. By default it checks functions and methods that no subclass can override; `level = "aggressive"` also checks other methods. It is report-only.

### Fixed

- `validate-function-name` can rename a nested function when the enclosing function's default argument binds the same name in an outer scope. It declines when a `global` or `nonlocal` declaration can affect the rename.
- `redundant-dict-get` accounts for decorator and class construction calls when checking dictionary keys across loop iterations. An undecorated function body alone no longer invalidates the proof.
- `redundant-enum-value` suppresses suggestions when nested definitions can change an enum member's value.
- `defined-far-from-use` tracks function locals from the body. A default argument that reads a same-named outer variable no longer hides a distant use of the local.

- The `redundant-type-conversion` daemon shuts down after its socket is deleted or replaced. Existing connections can finish, and new hook runs can start a replacement.
- `redundant-type-conversion` at `aggressive` level preserves conversions from mutable collections and live views to `tuple` or `frozenset`. It still reports redundant conversions when the argument already has the target type.
- `redundant-type-conversion` requires every possible type in a union to match the conversion target. For example, `tuple(x)` stays when `x` could be either a tuple or a list.
- `redundant-enum-value` no longer reports `.value` on an enum class that a nested function's default argument rebinds, as in `def inner(arg=(State := other)):`.
- `validate-function-name`'s fix renames references in the decorators, default arguments, and annotations of a function whose body rebinds the renamed name, instead of leaving them behind to raise `NameError`. It also stops renaming a same-named comprehension variable or type parameter, which silently changed what the code referred to, and declines when a class body both assigns and reads the name, where which binding a read sees depends on the order of execution.
- `meaningless-vars`'s fix leaves a variable alone when a class body in its function both assigns and reads the same name. When the read ran before the class's own assignment, the fix left it unchanged, and the class raised `NameError` when it was created.
- The hint after a `[FIXABLE]` violation reads "Run with --fix to apply it automatically." It used to say "inline" for every check, including checks whose fix doesn't inline anything.

## [0.8.0] - 2026-11-24

### Fixed

- `redundant-assignment` counts a variable read inside an assignment target, such as `calls[source] += 1` or `self.name: str = source`, as a use. It no longer reports such a variable as used only once.
- `validate-function-name` no longer treats the exception a function raises as evidence of what it does. A `get_` function whose loop returns a match and which ends with `raise KeyError(key)` keeps its name instead of being reported as `create_`.
- `defined-far-from-use` at `aggressive` level no longer suggests moving a clock reading, such as `start = time.perf_counter()` or `now = datetime.now()`, closer to its use, since that changes what it measures.
- `redundant-type-conversion` at `aggressive` level no longer reports a conversion passed straight to a parameter that accepts anything, such as `json.dumps(tuple(a_set))` or `print(tuple(a_set))`, unless the value already has the target type. Such a parameter can't reveal a mismatch, and the conversion can still change what the call does.
- `redundant-type-conversion` at `aggressive` level no longer reports `dict(x)` when `x` is a `defaultdict`, `Counter`, or `OrderedDict`. Dropping the conversion would pass that object on, so a missing key would no longer raise `KeyError` (`defaultdict`, `Counter`), or equality would start to depend on key order (`OrderedDict`).

## [0.7.0] - 2026-11-18

### Added

- Add an optional `hooks` setting to `[tool.ruff-extra-rules]` that lists the hooks a project runs. When it is set, selecting or configuring a check that none of the listed hooks runs — for example, `redundant-type-conversion` settings with only `ruff-extra-rules` listed — fails with exit code 2 instead of being silently ignored.

## [0.6.0] - 2026-10-06

### Added

- Add opt-in TR12, `suppression-rationale`, which requires an inline explanation or an immediately preceding comment block for Ruff and `pytriage` suppression comments. Preceding blocks containing only pragmas do not count. It accepts ambiguous cases, does not judge explanation quality, and provides no automatic fix.

## [0.5.0] - 2026-10-01

### Fixed

- `redundant-assignment` preserves intermediate variables at the default level when inlining would create a deeply nested call expression or an overly long use line, or when the assigned value spans multiple lines. This also applies to argument echoes; aggressive reporting is unchanged.

### Added

- Add TR11, `defined-far-from-use`, which reports a local variable assigned before an early `return`, `raise`, `continue`, or `break` that skips its use, or more than `max-distance` (default 5) unrelated statements before its first use. By default it reports only values that are safe to move, such as literals and empty containers; `level = "aggressive"` also reports calls and attribute reads. It is report-only.

## [0.4.6] - 2026-09-17

### Fixed

- `redundant-type-conversion`'s persistent `ty` daemon no longer keeps offering a file it can no longer read (e.g. deleted after a direct, non-`pre-commit`/`prek` invocation examined it) to a later, unrelated commit's reconciliation pass — which previously failed that unrelated commit outright with "could not be read or parsed; file skipped". The daemon now forgets a file the moment a re-read of it fails.

## [0.4.5] - 2026-09-16

### Fixed

- `redundant-type-conversion` `aggressive` mode no longer reports a `list`/`dict`/`set`/`bytearray` conversion assigned to a name (directly, through a chain of plain reassignments, through any depth of attribute/subscript target, or through a `for`/comprehension target over a literal list/tuple/set) when that name is later mutated in place — a subscript assignment or deletion, an augmented assignment, or a call to a mutating method such as `.append()`, `.update()`, or `.add()` — since removing the conversion there would turn a distinct copy into a shared, mutable reference to the original value (e.g. `record = dict(record); record["_id"] = 42`, or `obj.items = dict(source); obj.items["id"] = 1`).
- `redundant-type-conversion` `aggressive` mode no longer reports `set(...)`/`frozenset(...)` as redundant when the wrapped value's own type permits duplicates (a `list`, `tuple`, or any other type without set semantics) unless it's already exactly a `set`/`frozenset`-family type — converting a duplicate-permitting value is deduplication, a real behavior change `ty`'s type-only view can't see.
- `redundant-type-conversion` `aggressive` mode no longer reports a non-exact-match conversion as redundant when it's a direct positional or keyword argument of a call to a logging method (`.debug()`, `.info()`, `.warning()`, `.warn()`, `.error()`, `.exception()`, `.critical()`, `.fatal()`) — a structured logger commonly renders the raw argument differently from its converted form (e.g. `logger.warning("msg", path=str(a_path))` loses the distinct `PosixPath(...)` rendering a logging backend would otherwise show for `a_path` itself).

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
