# validate-function-name autofix is restricted to small, single-return, non-method free functions

## Context

TRI004 can suggest a rename for any `get_*` function based on its detected behavior, but applying that rename automatically risks missing a real call site — a rename not accompanied by updating every caller breaks the program. `apply_fix`'s reference collector only finds true call sites reachable via normal AST traversal, and for a method specifically, only `self.x`/`cls.x` accesses within the same class body: it cannot see a call routed through a differently-named receiver (e.g. `reader.get_report()` invoked from unrelated code elsewhere in the file). Without a matching restriction on which suggestions `--fix` acts on, a method rename could leave real callers referring to a now-nonexistent name.

## Decision

`should_autofix()` gates `--fix` and requires ALL of:

- The suggestion is high-confidence (not a bare "no confident suggestion").
- The function is not a method — an autofix only ever targets free functions, since a method's true call sites can't be reliably enumerated (see above).
- The function's body is under 20 lines, excluding its docstring.
- Control-flow nesting inside the function is at most 1 level deep.
- The function has at most one `return` statement.

## Consequences

- A method is never auto-renamed regardless of confidence or size — its suggestion is always reported for manual review instead.
- A large or branchy free function's rename suggestion is reported but not applied, even at high confidence: the same "can we actually find every reference" reasoning extends to "the risk of an incomplete rewrite scales with how much logic sits between the definition and its uses."
- The five checks are independent, all-must-pass gates rather than a weighted score, so a maintainer reading the criteria can reason exactly about why a given suggestion wasn't auto-applied.
