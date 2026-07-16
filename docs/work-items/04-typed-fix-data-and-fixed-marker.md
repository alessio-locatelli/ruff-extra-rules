# Typed fix_data and a shared "fixed" marker

Status: Open — resolved by ADR (2026-07-16 interview), not yet implemented
Kind: Refactor / type safety

## Problem

`Violation.fix_data: dict[str, Any] | None` is a per-check-invented payload
shape: `forbid_vars` stores a raw violation dict, `redundant_assignment`
stores `{pattern, assign_line, var_name, rhs_source, use_line, use_col}`,
`validate_function_name` stores a `Suggestion` dataclass under a `"suggestion"`
key. On top of that, there's an implicit `fix_data["fixed"] = True`
convention read by `CheckOrchestrator._apply_fixes`
(`ast_checks/__init__.py:384-390`) and `main()`'s reporter
(`ast_checks/__init__.py:562`), but written independently by each check's own
`fix()` — no shared type or helper enforces this contract. This is the "leaky
`fix_data`" debt named in `docs/adr/0001-iterative-refactor-not-rewrite.md`
and it's still open.

While confirming exactly how `"fixed"` gets set today, found a concrete
instance of the problem: `CheckOrchestrator._apply_fixes` already marks a
check's _original_ violations as fixed itself, unconditionally, for every
check, whenever `check.fix()` returns `True`. `validate_function_name/__init__.py`'s
own `fix()` (line ~148) _also_ sets `violation.fix_data["fixed"] = True`
internally — but on `fresh_violations`, a throwaway list of new `Violation`
objects discarded when `fix()` returns and never read again. It's dead code,
and exactly the kind of asymmetry a single shared helper used consistently
would make impossible.

## Decision (2026-07-16 ADR interview)

Both parts confirmed:

1. Add `mark_fixed(violation)` / `is_fixed(violation)` helpers to `_base.py`;
   route every read/write of the `"fixed"` convention through them,
   including removing the now-identified dead marking in
   `validate_function_name/__init__.py`'s `fix()`.
2. Give each check a private `TypedDict` for its own `fix_data` shape (e.g.
   `ForbidVarsFixData`), defined and used only inside that check's own
   module where `check()` constructs it and `fix()` reads it back.
   `Violation.fix_data` itself stays `dict[str, Any] | None` and `ASTCheck`
   stays non-generic — `CheckOrchestrator` never reads check-specific
   fields, only the shared `"fixed"` flag via the helpers above, so making
   `Violation` generic would only fight the orchestrator's heterogeneous
   `list[Violation]` for no benefit at the one place that needs it.

## Priority

Medium-high. Ready to implement — no longer blocked.
