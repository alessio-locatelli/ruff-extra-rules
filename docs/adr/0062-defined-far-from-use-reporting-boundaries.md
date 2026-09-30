# defined-far-from-use reports a move only where the reorder is locally defensible

## Context

[astral-sh/ruff#15477](https://github.com/astral-sh/ruff/issues/15477) asks for a rule that flags a variable assigned before an early exit that doesn't use it. This project extends that to a variable assigned many statements before its first use. Upstream maintainers identified the blocker: moving an assignment reorders its right-hand side relative to every statement it moves past, and syntax alone cannot prove that reorder is safe. Common patterns depend on the original order:

- a snapshot (`before = len(self.items)`) taken before a call that changes `self.items`
- a timing start (`start = time.monotonic()`)
- a validating call that should raise before a later check does
- a call made for its side effect

A rule that reports every such assignment is noisy. A rule that reports only what is provably safe misses the calls the issue is actually about.

## Decision

TR11 (`defined-far-from-use`) is one default-enabled, report-only check with two triggers and a `level` option. The level applies the same gate to both triggers.

- **Triggers.**
  - _Early exit_: a `return` or `raise`, or a `continue` or `break` that targets a loop enclosing the assignment, sits between the assignment and its first use. The suggested position is directly after the last such exit, not directly before the use. That keeps statements between the last exit and the use, which is where snapshot-then-mutate code usually lives, out of the reorder.
  - _Distance_: the gap is larger than `max-distance` (default 5). The gap is measured in statements, not lines, so formatting doesn't change it. A statement that binds or mutates a name the use statement also reads prepares the same consumer and is not counted.
- **Binding guards (both levels).** Report only when every other reference to the variable in the function lies at or after its first use in the same block, and that first use reads the value rather than overwriting it. The variable must not be `global`/`nonlocal`, and the function must not reach its locals dynamically (`locals()`, `vars()`, `exec()`, `eval()`). The right-hand side must not suspend or bind (`await`, `yield`, walrus). Module and class bodies are never checked, because other code can observe their bindings.
- **`conservative` (default).** Only order-independent right-hand sides are reported. These are:
  - literals
  - exact arithmetic on numeric literals
  - containers of order-independent items
  - calls to unshadowed empty built-in constructors
  - local names that are definitely bound before the assignment and that the reorder window doesn't rebind

  Moving these can't change behavior.

- **`aggressive`.** Any right-hand side is reported unless a local heuristic says the reorder may change behavior:
  - the right-hand side reads a name that the reorder window rebinds, mutates (attribute/subscript store or delete, or a method call not known to be read-only), or passes to a call not known to be pure
  - the right-hand side calls something whose name reads as validation (`check`, `validate`, `coerce`, …)

  These heuristics are expected to grow. A false positive at this level is treated as a gap to close with more special handling, not as an accepted cost of the level (see [adding-a-check.md](../adding-a-check.md)).

## Considered Options

- **Opt-in check with no level**: rejected. It hides the safe, common case (a literal initialized far from its use) behind a flag, while offering nothing to reduce noise for users who do opt in.
- **Two check ids (early exit, distance)**: rejected. Both triggers make the same suggestion with the same reorder risk. Separate ids would duplicate the gate and the options.
- **Line-based distance**: rejected. A wrapped call or a comment block would change the result for code with the same structure.
- **Suggest moving to directly before the use in the early-exit case**: rejected. It widens the reorder window to include the code between the exit and the use, which is where state changes that a snapshot guards against usually happen.

## Consequences

- The default level never reports a move that changes behavior, but it misses the issue's own motivating example (`val = get_val()`). That case needs `aggressive`.
- `aggressive` stays heuristic. It can still report a call whose side effects interact with the code between it and its use through state the syntax doesn't show, such as a global, I/O, or the clock. It also declines some safe moves, for example when the code in between merely passes a shared, immutable argument to another function.
- There is no autofix, because an unsafe reorder is a silent behavior change.
