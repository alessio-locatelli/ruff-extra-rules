# TR5 resolves a same-file, unambiguous, undecorated positional-argument echo

## Context

[ADR-0056](0056-redundant-assignment-keyword-argument-echo.md) reports `func(name=name)` at the default level regardless of `name`'s descriptiveness, because the keyword itself states the callee's parameter name at the call site — no signature resolution needed. It explicitly left `func(name)` (positional) alone: "which parameter `name` binds to lives in the callee's signature, possibly in another file, and resolving that is a materially larger feature (defaults, `*args`/`**kwargs`, keyword-only parameters, decorators, overloads) than this decision covers."

Issue [#174](https://github.com/alessio-locatelli/ruff-extra-rules/issues/174) asks for exactly that resolution, scoped to a callee defined in the same file, and lists the same complications ADR-0056 flagged as open questions: positional-only/keyword-only/`*args`/`**kwargs` slot-shifting, decorators/overloads changing the effective signature, and cross-file/aliased/reassigned callees (the last three the issue names as non-goals outright).

## Decision

`redundant-assignment` resolves a positional argument's callee only when doing so is exact, local evidence — never a best-effort guess. A use `func(name)` is treated as an argument echo, exactly like `func(name=name)`, only when **all** of the following hold:

- `func` is a bare `Name` call (not `obj.method(...)` or `mod.func(...)`).
- `func` names exactly one function, defined directly in the module body — not a method, not nested inside another function, and not redefined or overloaded anywhere else in the file. A method or nested function is only reachable from within its own enclosing scope; resolving those correctly would need real lexical-scope resolution, which this feature deliberately doesn't do, so TR5 doesn't guess and simply leaves the name unresolved.
- That definition has no decorators, since a decorator can change the effective signature.
- `func` is not bound to anything else anywhere in the file — not a parameter, local variable, import, `del` target, match-statement capture, generic type parameter, or class name. Any such binding means some scope in the file could make `func` refer to something other than the module-level function, and TR5 can't tell a shadowed call site apart from an unshadowed one without scope resolution, so the name is excluded everywhere, not just at the shadowing scope.
- The file contains no `from module import *` anywhere, since a wildcard import can bind any name without saying which, making no name in the file trustworthy.
- The call passes no unpacked (`*iterable`) argument, since that makes the mapping from position to parameter statically unknowable.
- The variable's positional slot maps to a real, named parameter (not one absorbed by `*args`), and that parameter has the same name as the variable.

A parameter's default value doesn't affect this: a positional argument at a given index always binds to the same parameter regardless of what follows it, so defaults don't disqualify a match.

Once every check above passes, the remaining evidence is exact structural fact — this variable's value is bound to a specific, named parameter, not a heuristic judgment about whether the name looks descriptive. TR5 therefore reports it at the same conservative (default) level as the keyword-echo case, for the same reason ADR-0056 gives: this doesn't belong behind the flag that exists to gate heuristic-but-arguable reports.

No new autofix logic is needed: this reuses [ADR-0032](0032-redundant-assignment-autofix-safety-criteria.md)'s existing pattern-independent autofix gate unchanged.

## Considered Options

- **Full signature resolution — cross-file, through imports/aliases/reassignment, honoring decorators and overloads**: rejected. This is exactly the "materially larger feature" ADR-0056 deferred, and issue #174 names cross-file/aliased/reassigned callees as explicit non-goals. Nothing in this codebase resolves an import graph or evaluates a decorator's effect on a signature, and building that machinery for one check's edge case is a different, much larger project.
- **Report only at the `permissive` level**: rejected, for the same reason ADR-0056 rejected it for the keyword case — once the bail-outs above are satisfied, the evidence is exact, not an arguable heuristic, so it doesn't belong behind the flag reserved for heuristic-but-arguable reports.
- **Resolve by nearest-enclosing-scope lookup instead of whole-module uniqueness** (so a nested `helper` shadowing an outer `helper` would still resolve to the correct one): rejected. It would require the same kind of scope-resolution machinery this check's analysis deliberately doesn't build (see [ADR-0002](0002-redundant-assignment-scope-tracker-not-unified.md)), for a case an author is unlikely to write deliberately. Requiring whole-module uniqueness is simpler and conservative: it silently declines to report rather than guessing wrong.

## Consequences

- `func(name)` now reports at the conservative (default) level when `func` is a module-level definition, unique across the whole module (including methods and nested functions), undecorated, unshadowed anywhere in the file, and its parameter at that position is also named `name` — previously silent regardless of level. This is a MINOR change per [docs/releases.md](../releases.md): a run that was previously clean on code matching this pattern can now report it.
- A positional call to a callee that's ambiguous, decorated, defined elsewhere, a method, a nested function, accessed via attribute, shadowed by any other binding in the file, or reached through unpacking or a wildcard import is unaffected — TR5 does not attempt to resolve those, by design.
