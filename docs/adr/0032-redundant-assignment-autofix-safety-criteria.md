# redundant-assignment autofix is capped to mechanically safe inlines

## Context

TRI005 can report a violation whenever an assignment adds no clarity, but not every reported violation is safe to rewrite unattended. Inlining a constant or a plain name is always safe, but inlining an attribute access or a call can change how many times, and in what order, side-effecting code runs — a rewrite that silently changes program behavior is unacceptable for a check that runs automatically in a pre-commit hook. `should_autofix()` is the sole gate on whether `--fix` touches a reported violation (issue #76): pattern-independent, with no separate semantic-value ceiling narrowing it further below whatever reporting already decided.

## Decision

`--fix` only inlines an assignment when ALL of the following hold:

- The assignment is not inside a loop or other conditional control flow.
- The RHS is single-line, and inlining it would not push the real usage line past PEP 8's 79-character limit.
- The RHS is a constant or a plain name (always safe to move verbatim), **or** an attribute access or call whose one use is the very next statement after the assignment, with nothing effectful evaluating between the assignment and that use — and, specifically for a call, capped at 2 positional arguments with no keywords, or the reverse (keyword-only, capped at 2).

Constants and names are always safe because evaluating them can't run arbitrary code. An attribute access or a call can (e.g. a `@property` getter, or a function with side effects), so moving either is only safe when its new call site runs at exactly the same point in program order as the original assignment — which is why those two require the immediate-next-statement condition the first two don't. The argument cap on calls exists so inlining doesn't turn the use site into a visually complex expression, defeating the readability goal the check exists for.

## Consequences

- A reported violation whose RHS is a multi-argument call, or whose single use isn't the very next statement, is never auto-fixed regardless of `--redundant-assignment-level` — it's left for a human to decide, not silently rewritten.
- The 79-character check runs against the real usage line at fix time (`exceeds_line_length_when_inlined`), the same function reporting uses for its own `[FIXABLE]` estimate, so the two can never disagree about whether a given rewrite is safe.
- Because the gate is pattern-independent, a future reporting pattern doesn't need its own separate autofix logic — it either satisfies these mechanical constraints or it doesn't.
