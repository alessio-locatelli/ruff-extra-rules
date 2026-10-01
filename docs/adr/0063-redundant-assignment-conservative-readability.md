# TR5 preserves expression readability at the conservative level

## Context

A single-use variable can separate operations and make a use expression easier to read. A parameter that repeats the variable's name establishes that the name adds no new information, but does not establish that combining the expressions improves readability. [ADR-0056](0056-redundant-assignment-keyword-argument-echo.md) and [ADR-0060](0060-redundant-assignment-positional-argument-echo.md) establish argument-echo detection; readability must also govern whether the conservative level reports those assignments.

## Decision

Every TR5 reporting pattern at the conservative level applies these additional readability limits:

- Preserve an assignment whose RHS source spans multiple lines.
- Preserve an assignment if its physical use line already exceeds 79 characters, or would exceed that limit after replacing the variable with its single-line RHS. Count indentation and Unicode characters. A line of exactly 79 characters remains eligible.
- Preserve an assignment if its resulting containing expression would have three or more nested calls. Calls in arguments, keywords, and method receivers contribute to nesting; sibling calls do not add to each other's depth. Two layers, such as `consume(load())`, remain eligible.

For a multiline use expression, the length limit applies to the physical line containing the variable. Call nesting applies to the containing expression, including sibling subexpressions, independently of line breaks. These limits apply before an argument echo can bypass name-based heuristics.

The aggressive level retains its reporting behavior. Both levels retain the same mechanical autofix safety criteria in [ADR-0032](0032-redundant-assignment-autofix-safety-criteria.md).

## Alternatives

- Reject every nested call: this would also preserve simple combinations such as `consume(load())`, making the default level unnecessarily quiet.
- Apply the limits only to argument echoes: this would treat equivalent readability concerns differently across TR5 patterns.
- Configure the line limit or derive it from Ruff: a fixed limit matches the existing autofix constraint without adding another configuration surface.

## Consequences

The conservative level favors keeping a readable intermediate variable over reporting every structurally redundant name. Some redundant assignments remain unreported even when a human could format their inlined expressions differently. The limits approximate readability rather than proving it; aggressive reporting remains available for users who want broader suggestions.
