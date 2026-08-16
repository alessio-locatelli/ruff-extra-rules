# TR5 reports a keyword-argument echo at the default level, regardless of name descriptiveness

## Context

TR5 exempts a descriptively-named assignment from being reported through several independent guards: `_is_named_constant_pattern` treats a multi-part name assigned a numeric literal as a deliberately named constant; `_is_generic_call_result_name` exempts a `Call` RHS whose name isn't a generic placeholder or a restatement of the callee; `_is_named_string_constant_pattern` exempts a module-level SCREAMING_SNAKE_CASE string constant; and `calculate_semantic_value` awards points for a descriptive prefix/suffix, a transformative verb, a multi-part name, and a name longer than its RHS, any of which can clear the reporting ceiling on their own. Each is sound in general — a descriptive name usually does carry information the value does not.

Issue [#172](https://github.com/alessio-locatelli/ruff-extra-rules/issues/172) reported a case where that reasoning breaks down: `func(days_with_routes_in_a_row=days_with_routes_in_a_row)`. The keyword argument already states the name, at the same source position, so the name cannot be adding information the call site doesn't already carry — no matter how descriptive it looks in isolation, and no matter which of the guards above would otherwise credit it. None of them looks at the use site to notice this.

## Decision

`redundant-assignment` reports an assignment whose single use is `func(name=name)` — a keyword argument whose keyword is exactly the variable's own name — regardless of how descriptive the name is, at both aggressiveness levels including the conservative default.

Every rule that judges mechanical or structural safety still applies unchanged: loop/control-flow position, comments, `await`, line length, required parentheses, non-deterministic RHS calls, and the control-flow/comprehension usage-site mismatches. Only the rules whose entire purpose is judging whether `name` itself is "descriptive enough" — the named-constant pattern, the generic-call-result-name check, the module-level string-constant convention, and the semantic-value scoring — are withheld for this one pattern. A verbatim keyword echo is exact, local evidence available from the assignment and its use site alone, unlike the rest of that scoring, which is a heuristic guess at descriptiveness.

The pattern is decided from the assignment and its single use, never from the callee's signature. `func(name)` positional is unaffected: which parameter `name` binds to lives in the callee's signature, possibly in another file, and resolving that is a materially larger feature (defaults, `*args`/`**kwargs`, keyword-only parameters, decorators, overloads) than this decision covers. That case is tracked separately rather than folded into this one.

This is a MINOR change per [docs/releases.md](../releases.md): a run that was previously clean on code matching this pattern can now report it, at the level that runs by default.

## Considered Options

- **Withhold only the two guards the issue's reproduction exercised (the named-constant pattern and the semantic-value scoring), leaving the `Call`-RHS and string-constant guards active** — rejected. Both of those guards are just as name-keyed as the two the issue names; leaving them active would still under-report a keyword echo whose RHS happens to be a call or a module-level string constant, contradicting "regardless of how descriptive."
- **Report only at the permissive level** — rejected. The evidence a keyword echo supplies is exact, not a heuristic judgment call the way the rest of TR5's scoring is, so it doesn't belong behind the flag that exists specifically to gate heuristic-but-arguable reports.
- **Also resolve same-file positional arguments to their callee's parameter name** — rejected for this decision; tracked as a separate, larger feature (see Context).

## Consequences

- An assignment whose only use is `func(name=name)` is now reported at the conservative (default) level regardless of `name`'s shape or RHS — where it previously wasn't reported at all (numeric-literal RHS), was reported only at `permissive` (other literal RHS), or depended on whether the name restated its callee (`Call` RHS).
- No autofix change was needed for a `Constant`/`Name` RHS: it was already always safe to inline per [ADR-0032](0032-redundant-assignment-autofix-safety-criteria.md), so `func(days_with_routes_in_a_row=42)` is fixable the same way any other constant-RHS single use already was. A `Call`/`Attribute` RHS keyword echo is reported the same as any other RHS shape, but ADR-0032's existing immediate-use and argument-count criteria still gate whether `--fix` touches it.
- `func(name)` (positional) is unaffected by this decision.
