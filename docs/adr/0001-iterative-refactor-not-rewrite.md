# Iterative refactor toward target architecture, not a rewrite

The codebase was vibe-coded without deliberate architectural decisions, and has real structural debt (duplicated hook pipelines, four independent hand-rolled AST scope walkers, a leaky `Violation.fix_data`, import-order-dependent check registration). We decided to migrate toward a target architecture through small, independently reviewable steps against the existing tree, rather than rewriting from scratch.

## Considered Options

- **Rewrite from scratch**: rejected because `tests/fixtures/<check>/{good,bad}/*.py` is the only executable spec for each check's heuristics (e.g. TRI005's semantic-value scoring, TRI004's behavioral pattern classification). A rewrite would have to re-derive or re-import that same spec, without the safety net of running it after every change. The debt found is concentrated in the plumbing layer (`ast_checks/__init__.py`, `_base.py`, the two hook entrypoints), not spread through the check logic itself, so it doesn't require discarding the checks to fix.

## Consequences

- The existing test/fixture suite is the fixed point for the migration: each step must keep it green, and it stands in for behavioral intent that was never written down elsewhere.
- The migration plan is expressed as an ordered sequence of small, revertible diffs against `main`, not a parallel branch merged in one shot.
