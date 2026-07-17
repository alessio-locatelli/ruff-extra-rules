# Structural cache invalidation (replace the hand-bumped CACHE_VERSION)

Status: Open — resolved by docs/adr/0005-cache-key-source-hash-and-config-fingerprint.md, not yet implemented
Kind: Refactor / reliability

## Problem

`CacheManager.CACHE_VERSION` (`_cache.py:53`) is a single hand-maintained
global string that must be bumped manually whenever any check's
detection/fix logic changes in a way that could make a previously-cached
result stale. Forgetting to bump it has already caused a real, since-fixed
bug (commit `0e3efba`, "rewrite stale version tag when invalidating a cache
blob"). There's no automated safeguard tying a check's own code to its
contribution to the cache key — correctness depends entirely on a developer
remembering.

## Decision (see docs/adr/0005-cache-key-source-hash-and-config-fingerprint.md)

Replace `CACHE_VERSION` entirely with a cache key built from three parts:
the sorted `check_id` list (unchanged), a fingerprint of each enabled
check's constructor arguments (see
[09-cache-key-missing-check-config.md](09-cache-key-missing-check-config.md)),
and a SHA-1 hash of the concatenated source of every `.py` file under
`src/pre_commit_hooks/`. The tree hash is recomputed fresh on every process
invocation, not cached to disk itself.

## Priority

High value given "performance is critical" is a stated project priority and
this has already caused a real bug. Ready to implement — no longer blocked.
