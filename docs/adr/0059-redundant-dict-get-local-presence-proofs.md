# Redundant dict.get uses local key-presence proofs

## Context

`dict.get(key)` expresses that a key might be absent. When the current path already proves that key exists, the optional-facing access hides an invariant and makes the code less direct. Replacing it with `dict[key]` is not an automatic fix: Python mapping implementations may give the two operations distinct behavior, even for a present key.

`docs/audits/type-checker-coverage-for-redundant-dict-get.md` records that ty, Mypy, and Pyright do not report the supported proven-presence examples as redundant access.

Required `TypedDict` keys were investigated as a type-backed proof source. `ty`'s hover response exposes an overload for both required and optional keys. Its return type separates a required `int` key from an optional `int` key, but cannot separate a required `int | None` key from an optional `int` key. Replacing `.get()` with subscription adds no `ty` diagnostic for an optional key, so diagnostic comparison cannot fill that gap. A local parser for TypedDict declarations, imports, inheritance, and requiredness would duplicate a partial type checker.

## Decision

`redundant-dict-get` (TR9) is a report-only AST check. It reports only `.get(key)` calls with one positional argument where the receiver is a direct local name and the key is a string literal or the same local-name key used in a supported membership proof.

The initial proof providers are deliberately independent:

- a direct local dict display with explicit string keys;
- the true branch of `if key in mapping:` for a local dict display or a direct builtin `dict[...]` parameter annotation;
- the path after `if key not in mapping:` for the same receiver forms, when its body terminates with `raise` or `return`.

Facts are confined to one lexical scope and discarded when a tracked name is rebound, mutated through a subscription, passed to an unknown call, stored through an unsupported construct, or crosses a loop, try, with, class, function, lambda, or comprehension boundary. Dictionary unpacking, comprehensions, aliases, non-string literal keys, boolean-combined predicates, branch merges, and alternative terminating forms are out of scope.

TR9 is default-enabled in the dedicated `ruff-extra-rules-ty` hook and remains excluded from the normal hook. `# pytriage: TR9` is its only inline suppression. It never applies an autofix. The normal/ty-hook split is represented by an explicit check-id collection rather than a one-off class-identity exclusion, so future checks can join the dedicated hook without changing entrypoint architecture.

Required TypedDict detection is deferred until a type checker exposes a stable requiredness query that handles nullable required values. The local proof protocol is the extension point for that provider and for future relational container proofs; neither is approximated meanwhile.

## Consequences

TR9 catches local invariants without starting an external process or duplicating type-checker behavior. It intentionally leaves valid but more complex cases unreported to preserve false-positive safety.

A future type-backed provider needs its own positive and negative compatibility controls, non-cacheable direct-input reconciliation, real-type-checker integration coverage, and a documented performance measurement before it can be enabled.
