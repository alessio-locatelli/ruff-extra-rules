# Structural cache invalidation (replace the hand-bumped CACHE_VERSION)

Status: Open — blocked on an ADR decision
Kind: Refactor / reliability

## Problem

`CacheManager.CACHE_VERSION` (`_cache.py:53`) is a single hand-maintained
global string that must be bumped manually whenever _any_ check's
detection/fix logic changes in a way that could make a previously-cached
result stale. Forgetting to bump it has already caused a real, since-fixed
bug (commit `0e3efba`, "rewrite stale version tag when invalidating a cache
blob"). There's no automated safeguard tying a check's own code to its
contribution to the cache key — correctness depends entirely on a developer
remembering.

## Proposed Fix

Needs a design decision before implementation — this is one of the top
ADR-backfill candidates raised in the 2026-07-16 architecture review.
Candidate designs to weigh in that discussion:

- (a) a `behavior_version` class attribute per check, folded into the cache
  key alongside `check_id`;
- (b) hash each enabled check module's source and fold that into the key;
- (c) keep the current single global version, but add a check (e.g. a local
  pre-commit/test step) that flags when check source changed without a
  version bump.

## Priority

High value given "performance is critical" is a stated project priority and
this has already caused a real bug, but explicitly do not implement until
the ADR is written.
