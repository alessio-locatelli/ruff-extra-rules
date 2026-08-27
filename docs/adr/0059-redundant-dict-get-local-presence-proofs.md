# Redundant dict.get uses local key-presence proofs

## Context

`dict.get(key)` expresses that a key might be absent. When the current path already proves that key exists, the optional-facing access hides an invariant and makes the code less direct. Replacing it with `dict[key]` is not an automatic fix: Python mapping implementations may give the two operations distinct behavior, even for a present key.

`docs/audits/type-checker-coverage-for-redundant-dict-get.md` records that ty, Mypy, and Pyright do not report the supported proven-presence examples as redundant access.

Required `TypedDict` keys cannot be inferred from `ty` hover or diagnostic comparison: nullable required values and optional values remain indistinguishable. TR9 therefore reads only complete local declarations, where `Required`, `NotRequired`, and `total=False` are syntactic facts. Imported, inherited, or otherwise unresolved declarations remain unknown.

## Decision

`redundant-dict-get` (TR9) is a report-only AST check. It reports only `.get(key)` calls with one positional argument where the receiver is a direct local name and the key is a string literal or the same local-name key used in a supported membership proof.

The proof providers are deliberately independent:

- a direct local dict display with explicit string keys;
- required fields on a complete local `TypedDict` declaration;
- direct aliases of known dictionaries and collections;
- short-circuit Boolean membership guards, branch intersections, and paths after structural termination;
- validated local key collections through `required <= mapping.keys()` and `all(key in mapping for key in required)`.

Facts are confined to one lexical scope and discarded when a tracked name is rebound, mutated through a subscription, passed to an unknown call, stored through an unsupported construct, or crosses a suspension or comprehension boundary. Branch joins retain only facts shared by every normal path.

TR9 is default-enabled in the dedicated `ruff-extra-rules-ty` hook and remains excluded from the normal hook. `# pytriage: TR9` is its only inline suppression. It never applies an autofix. The normal/ty-hook split is represented by an explicit check-id collection rather than a one-off class-identity exclusion, so future checks can join the dedicated hook without changing entrypoint architecture.

The default `conservative` level accepts only built-in local dictionaries and collections plus complete local `TypedDict` declarations. `aggressive` additionally accepts direct built-in `dict[...]` parameter annotations; this is a deliberate heuristic because subclasses can customize mapping operations.

## Consequences

TR9 catches local invariants without starting an external process. It intentionally leaves unresolved type declarations and unbounded flow cases unreported at the default level to preserve false-positive safety.

An imported or inherited type-backed provider needs its own positive and negative compatibility controls, non-cacheable direct-input reconciliation, real-type-checker integration coverage, and a documented performance measurement before it can be enabled.
