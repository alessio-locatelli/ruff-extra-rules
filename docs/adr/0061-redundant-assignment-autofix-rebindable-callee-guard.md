# redundant-assignment autofix declines to reorder an effectful RHS past a rebindable callee

## Context

Issue [#194](https://github.com/alessio-locatelli/ruff-extra-rules/issues/194): [ADR-0032](0032-redundant-assignment-autofix-safety-criteria.md) lets `should_autofix()` inline an Attribute/Call RHS into a call-site argument when the assignment's one use is the very next statement:

```py
x = replace()
return func(x)
```

Python evaluates a call's callee expression before its arguments, so the original code evaluates `replace()` (in the assignment statement) before it ever looks up `func` (in the next statement). The autofixed form,

```py
return func(replace())
```

evaluates `func` first and `replace()` second — the opposite order. ADR-0032 already requires nothing effectful to run between the assignment and the use, but that condition only protects against effects already visible _within_ the statements being merged; it says nothing about the callee lookup the merge newly promotes ahead of the RHS's own evaluation. If `replace()` has the side effect of rebinding `func`, the two versions call different functions. The same reasoning applies whether the argument is positional or keyword, and whether the callee is a bare name (`func(x)`) or an attribute access (`obj.method(x)`), since `obj` is looked up before its arguments the same way a bare name is.

## Decision

Before inlining an Attribute/Call RHS into a use that sits directly in a `Call`'s argument list (positional or keyword, not itself the callee), `should_autofix()` requires that the call's callee — the bare name itself, or the base name of a plain `name.attr` chain — is provably stable: never reassigned, deleted, shadowed, or imported anywhere in the file (the same whole-file `shadowed` set [ADR-0060](0060-redundant-assignment-positional-argument-echo.md) already computes), and if it's also the subject of a `def`/`async def` anywhere in the file, that definition must be the single, undecorated, top-level one ADR-0060's own `functions` index already requires for its callee-resolution guarantee, _and_ it must appear earlier in the file than the call being inlined into. A nested, conditional, redefined, or decorated `def` doesn't give that stability guarantee at all; a single top-level one only gives it once its own `def` line has actually executed, which a call positioned earlier in the file — whether at module level directly, or inside a function invoked before that point — cannot assume. For the same reason ADR-0060 treats a wildcard import as making no name in the file trustworthy, a file containing `from module import *` disqualifies every callee outright, since any name could be rebound by the star-import without saying which.

A callee expression that isn't a bare name or a `name.attr` chain — an attribute access through a subscript (`obj[index].method(x)`), a call (`get_func()(x)`), or anything else not statically resolvable to a base name — is treated the same as a rebindable name: there's no static evidence it's safe, so the conservative default is to decline.

This covers a use that is a direct argument of some `Call` in its immediate context — positional, keyword, or unpacked via `*args`/`**kwargs`. It doesn't walk out through further layers of nesting (e.g. `outer(inner(x))`, where `x` is `inner`'s argument but the reordering risk also touches `outer`'s callee) — a narrower case ADR-0032's argument-count cap already keeps rare in practice.

## Consequences

- `func(x)` / `func(key=x)` / `obj.method(x)` inlining an Attribute or Call RHS is now skipped when `func` (or `obj`) is reassigned, deleted, shadowed by a parameter, imported, bound by a nested/conditional/redefined/decorated `def`, or otherwise bound anywhere else in the file — the same evidence ADR-0060 already treats as disqualifying, plus the `def`-specific stability requirement above. This is a MINOR change per [docs/releases.md](../releases.md): a violation that was previously auto-fixed can now be reported as fixable=false instead.
- A Constant or Name RHS is unaffected — ADR-0032 already allows those unconditionally, and moving a side-effect-free lookup earlier or later can't change what a rebound callee would have looked up to.
- This does not defend against dynamic rebinding invisible to static analysis (e.g. `globals()["func"] = other` as a side effect of the RHS call) — the same category of gap ADR-0060 already accepts for callee resolution generally, and one this codebase has no machinery to close without evaluating arbitrary code.
