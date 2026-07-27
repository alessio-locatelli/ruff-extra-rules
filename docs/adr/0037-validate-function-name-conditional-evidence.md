# validate-function-name requires call-shape evidence to be unconditional and in-scope

## Context

TRI004 infers a `get_`-prefixed function's behavior by looking for specific call shapes anywhere in its body (disk I/O, network I/O, object construction, parsing, ...) and suggesting a more specific verb when it finds one. The original detection walked the function's entire AST subtree with no notion of control flow or scope, so it couldn't distinguish a call that always runs from one that only runs on a fallback path, or a call inside the function's own body from one inside a nested `def`/`class`/`lambda` that may never execute at all.

This produced false positives on the well-established "lazy singleton" / "cache-then-compute-on-miss" `get_` pattern — `logging.getLogger` is the canonical stdlib example. This repo had two real instances: a lazy singleton accessor whose constructor call only runs the first time (behind an `if <cache> is None:` guard), and a cache accessor whose disk read only runs on the guard's happy path (behind `try`/`except`). Both were suppressed with an inline ignore rather than the rule recognizing the shape.

## Decision

A call-shape flag (I/O, network access, object construction, parsing/rendering, aggregation, output, search-by-call, transformation, call-based validation) only counts as evidence when the qualifying call is reached **unconditionally** from the function's own entry point — not nested inside the body of an `if`/`while`/`for`/`try`/`match`/ternary branch that some invocations could skip, and not inside a generator expression, whose body only runs if a caller iterates the result (unlike a list/set/dict comprehension, which evaluates eagerly). A call reached only through such a branch is fallback evidence, not the function's defining action, and no longer influences its suggested name.

This evidence collection is also now scoped to the function's own execution: a nested `def`/`class`/`lambda` is a separate scope that may never run, so calls inside one no longer leak into the enclosing function's flags.

Flags that aren't "one specific call shape happened" evidence — a `@property` decorator, a `-> bool` return annotation, presence of `yield`, returning another `get_*` call's result, returning a class, mutating a parameter, building up and returning a container, or building an error list by variable name — are unaffected; nothing about the issue this fixes implicates them.

## Alternatives considered

- **Idiom-matching specific shapes** (e.g. detect `if x is None: x = ...; return x` or `try: return cache[k] except KeyError: ...` by pattern): rejected as a set of hand-matched shapes that would need to grow indefinitely as new variations appeared, exactly the "accumulate special cases" outcome the original issue objected to. The unconditional-reachability rule generalizes to any guard shape without enumerating them.
- **Data-flow/alias analysis** to prove a guarded call's result is definitely cached: rejected as disproportionate — this is a fast, local AST heuristic, not a type checker, and the reachability rule already resolves the reported false positives without it.

## Consequences

- A `get_` function whose expensive operation is conditional — with some path that doesn't need it — keeps its `get_` name unless another, unconditional signal applies. This is deliberate: optionality is exactly what "get" (as opposed to "load"/"create"/"fetch") should mean.
- A function that guards unrelated code (e.g. a debug-only log call) around an otherwise-unconditional operation is unaffected — only the call actually nested inside the guard loses evidence, not the rest of the function.
- Within a `try`, the body/`except`/`else` are all treated as fallback evidence, but `finally` is not — a `finally` block always runs once the `try` is reached, so it inherits the `try`'s own conditional level instead. This is a deliberate asymmetry: the `try` body is technically the attempted/primary path too, but treating it as fallback evidence is what makes the cache-then-compute-on-miss shape above resolve correctly, and the cost is a possible false negative (a function whose sole defining action sits in an otherwise-unguarded `try` body keeps its `get_` name) rather than a false positive.
- Not handled, by design: `and`/`or` short-circuit evaluation is not treated as a guard (modeling it precisely adds real complexity for a much rarer real-world idiom than `if`/`try`), and a locally-defined helper's effects are never attributed to its caller even when the caller unconditionally invokes it once — this is a scope-boundary rule, not call-graph resolution.
