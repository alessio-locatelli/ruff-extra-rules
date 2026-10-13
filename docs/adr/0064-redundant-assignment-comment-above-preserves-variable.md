# TR5 preserves an assignment explained by a comment directly above it

## Context

A comment placed directly above an assignment is usually the author explaining that variable: why the value exists, where it comes from, or why it has its name. TR5 judges redundancy from the assignment and its single use alone, so it cannot tell whether inlining would lose that explanation. Inlining would also leave the comment above an unrelated statement.

## Decision

TR5 does not report an assignment when the line directly above it contains only a comment. This applies at both levels and in every scope. TR5 does not read the comment's text, so any comment-only line counts. A blank line between the comment and the assignment breaks the association, and TR5 then judges the assignment as if the comment were absent.

TR5 reports the same code without the comment, and that difference is intended. It is not an inconsistency to fix.

## Alternatives

- Report the assignment and only withhold the autofix: the rewrite would stay safe, but TR5 would still ask the author to remove a variable they deliberately documented and to relocate or rewrite the comment by hand.
- Inspect the comment's text to decide whether it explains the variable: a text heuristic would be unreliable, and a comment directly above the assignment is already strong evidence of intent.
- Also honor a comment separated by blank lines: such a comment usually introduces a group of statements rather than one assignment.

## Consequences

- An author can keep a single-use variable by explaining it, without an inline `# pytriage: TR5` suppression.
- A comment directly above an assignment that describes something else, such as a section header, also preserves that assignment, so TR5 under-reports in that case.
