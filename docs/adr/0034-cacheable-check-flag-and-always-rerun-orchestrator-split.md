# A `cacheable` check flag, and an always-rerun split in `CheckOrchestrator`

**Update, ADR-0044**: the "shared cache-version fingerprint" this ADR describes — the cacheable group's own check IDs and configuration — now moves into `CheckOrchestrator._generate_hook_name()` instead; `cache_version` (`_generate_cache_version()`) no longer includes it. The cacheable/always-rerun split itself, and every other decision below, is unaffected. See ADR-0044.

## Context

Every check's `check()` contract has always been "look only at the single file you're given" (see `docs/adding-a-check.md`'s "Incremental-analysis limitations" section) — a cache hit for an unchanged file reproduces exactly what a full re-run of that file would produce, because nothing about the result can depend on any other file.

`redundant-type-conversion` (TR6) breaks that assumption on purpose: its whole value is catching a redundant conversion at a call site whose parameter type is declared in a different file (see `docs/adr/0035-redundant-type-conversion-ty-lsp-detection.md`). That means its result for `caller.py` can change when only `callee.py` changes, which `caller.py`'s own content-hash cache key has no way to invalidate on — a normal `git commit` that only touches `callee.py` never re-examines `caller.py` at all, so a stale "no violations" result for it would keep being served indefinitely.

`CheckOrchestrator` caches one combined violation list per file, covering every enabled check together. A check that simply opted out of caching for itself would silently disable caching for every _other_, unrelated check sharing that file, defeating the point of adding a cross-file-aware check without also regressing every existing one's performance.

## Considered Options

- **Extend the cache key with a hash of each file's actual import closure**: rejected. Computing "the closure" correctly means resolving Python's own dynamic import machinery (conditional imports, `importlib`, re-exports, `TYPE_CHECKING`-only imports that still affect declared types, ...) — getting this wrong in either direction reintroduces the same class of stale-cache bug `docs/adr/0005-cache-key-source-hash-and-config-fingerprint.md` was written to eliminate the root cause of, just scoped to imports instead of the package's own source.
- **Exempt the whole check from caching by disabling `CheckOrchestrator`'s cache outright**: rejected — regresses every check's caching performance the moment TR6 is enabled alongside them, which is the exact problem this decision needs to avoid.
- **A `cacheable` flag on `ASTCheck`, plus an orchestrator-level split between a cacheable group and an always-rerun group**: adopted (below).

## Decision

`ASTCheck` gains a `cacheable: bool` property, defaulting to `True` on `BaseCheck` so every existing check needs no change. A check overrides it to `False` only when its own result for one file can depend on another file's current content.

For each file, `CheckOrchestrator` partitions its applicable checks into a cacheable group and an always-rerun group:

- The cacheable group keeps today's behavior exactly: a cache hit serves every cacheable check's stored violations without re-parsing or re-running anything; a cache miss runs the cacheable group fresh and writes only its own violations back to the cache.
- The always-rerun group is re-run on every single call, regardless of what the cache holds for that file, and its violations are never written to the cache.
- Both groups' violations are merged into the same per-file result the rest of the pipeline (fix mode, reporting) already expects.

The shared cache-version fingerprint (`CheckOrchestrator._generate_cache_key()`) is derived only from the cacheable group's own check IDs and configuration. An always-rerun check's presence, or a change to its own configuration (e.g. `--redundant-type-conversion-level`), never bumps the fingerprint — its results are never cached in the first place, so there is nothing for that fingerprint to protect.

## Consequences

- Enabling, disabling, or reconfiguring an always-rerun check never forces an unrelated cacheable check to recompute a file it would otherwise have served from cache.
- An always-rerun check pays its own full analysis cost on every file, every run, with no caching benefit of its own — an explicit, accepted trade-off for the class of check that needs it, not a regression for any check that doesn't.
- A check that raises while running is still recorded as a `rule_failures` entry the same way regardless of which group it's in; only the cacheable group's crash blocks caching that file's cacheable results, matching the pre-existing "never cache an incomplete result" rule.
- Any future check whose result can depend on state outside the single file it's given must set `cacheable = False` rather than reusing the shared cache incorrectly — this is now a documented, checked extension point instead of an implicit assumption a new check could silently violate.
