# Cache key doesn't account for per-check runtime configuration

Status: Open — resolved by docs/adr/0005-cache-key-source-hash-and-config-fingerprint.md, not yet implemented
Kind: Bug / correctness

## Problem

`CheckOrchestrator._generate_cache_key()` (`ast_checks/__init__.py:185-192`)
builds the cache key from the sorted `check_id` list only:

```python
check_ids = sorted(check.check_id for check in self.checks)
return ",".join(check_ids)
```

`ForbidVarsCheck(forbidden_names={"custom"})` and `ForbidVarsCheck()` both
produce the identical key `"forbid-vars"` — the cache key carries a check's
identity but not its configuration. Concretely: run once with default
forbidden names (caches "no violation" or specific violations for a file),
then rerun with `--forbid-vars-names=custom` (or a changed
`[tool.forbid-vars.autofix]` in `pyproject.toml`) on the same unchanged file
— the second run serves the first run's stale, wrong-config violations
instead of re-checking, with no indication anything was skipped.

This is the same subsystem and the same underlying principle as
[07-structural-cache-invalidation.md](07-structural-cache-invalidation.md) —
everything that affects a cached result must be part of its cache key —
just triggered by a check's runtime configuration instead of a source code
change.

## Decision (see docs/adr/0005-cache-key-source-hash-and-config-fingerprint.md)

Fold a fingerprint of each enabled check's constructor arguments into the
cache key, alongside the whole-tree source hash and the `check_id` list from
item 07. Exact fingerprinting approach (e.g. a stable repr of the arguments)
is an implementation detail to settle when this is built, not part of the
ADR decision itself.

## Priority

High — same priority as 07, part of the same fix.
