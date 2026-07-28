# File-local semantic variable rename suggestions

## Context

TRI001 must keep reporting meaningless names wherever they are bound, while useful rename suggestions require more information than a right-hand-side spelling pattern can provide. A misleading automatic rename is worse than no rename, and a hook must remain fast, deterministic, offline, and independent of repository-wide analysis.

## Decision

TRI001 keeps detection and scope-aware rewriting in `meaningless_vars.py`. A dedicated private module builds a file-local model from the parsed AST and returns structured rename proposals for eligible simple local assignments.

A small number of narrow exceptions cover a meaningless name bound as a _parameter_ rather than a local assignment: a test parameter declared through `@pytest.mark.parametrize` and compared for equality in the test body, and a `data` parameter of a function whose own name is a recognized transformation verb (e.g. `compress`/`decompress`). Both are always suggestion-only, never auto-fixed — a correct rewrite would have to touch the parameter's own declaration (and, for the `parametrize` case, the decorator's own argnames literal too), which is a different rewrite surface than the `ast.Name`-position rewriting the fixer performs everywhere else.

The model separates expression, type annotation, consumer, control-flow, import-resolved API, and lexical-safety evidence from name generation. It recognizes a small fixed vocabulary of standard APIs and derives ordinary producer, collection, predicate, and annotation names without network access or project configuration.

Only a locally unambiguous proposal with strong compatible evidence is fixable. A useful but weaker proposal is reported without a fix. Missing, conflicting, dynamic, rebinding, collision, reflection, or externally visible context produces no automatic rename. The analysis never follows context outside the current file.

A proposal is never surfaced if the generated name collides with a Python builtin identifier, regardless of which evidence produced it. A rename that trades one meaningless name for one that shadows a builtin is strictly worse than no rename, so this check is a final, unconditional gate rather than a per-source-of-evidence concern.

## Consequences

- New naming patterns can be added without changing TRI001 detection or its rewrite engine.
- Suggestions are reproducible and have bounded per-file analysis cost.
- Many generic bindings intentionally receive no proposal; precision takes priority over coverage.
- The API vocabulary is deliberately limited and must be expanded with explicit semantics and tests rather than inferred from arbitrary call names.
- A suggested rename never makes the code worse: builtin-shadowing candidates (e.g. from a capitalized `typing` alias like `Dict`/`Tuple` snake-casing down to `dict`/`tuple`) are rejected outright instead of being reported as a weaker suggestion.
