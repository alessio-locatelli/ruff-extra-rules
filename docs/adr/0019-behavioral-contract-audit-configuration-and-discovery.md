# Behavioral contract audit: configuration & environment/project-root discovery (ch. 17, 28)

`docs/behavioral_contract.md` chapters 17 (Configuration) and 28 (Configuration and Environment Discovery) were audited against `_cli.py`'s CLI argument handling, `load_checks()`/`_generate_cache_key()`, and `CacheManager`. Both chapters are written for a tool with a config-file format and a project-root-discovery step; this project has neither (no `tomllib`, no `os.environ`/`os.getenv` reads, no upward directory walk anywhere in `src/`) — every check's configuration comes exclusively from CLI arguments. Full findings are in `docs/audits/0009-behavioral-contract-audit-configuration-and-discovery.md`.

## Decision

`--select`/`--ignore` parsing must tolerate the same malformed input `--exclude` already tolerates, and an error must name which flag it came from. A trailing or doubled comma (e.g. `--select=meaningless-vars,`) produced a spurious `Error: Unknown checks:` with nothing named, because the comma-split kept the empty string; blank tokens are now filtered the same way `--exclude` already filters them, and a value that names no real check falls through to the existing `Error: No checks enabled` message. The "unknown check" error itself now names the offending flag (`Error: Unknown checks in --select: X`) instead of leaving `--select` and `--ignore` indistinguishable.

The cache directory's location — resolved relative to the process's current working directory, with no independent project-root discovery — was already correct (matching `mypy`'s own `.mypy_cache` convention, not `ruff`'s project-root-anchored `.ruff_cache`) but undocumented. `prek`/`pre-commit` already fix cwd at the repository root for this hook's one supported production path, and the direct-CLI dev workflow in `AGENTS.md` is written to be run from the repo root too, so this was a documentation gap, not a behavioral one. README's `## Configuration` section now states the convention explicitly.

## Consequences

- `_cli.py` filters blank comma-separated tokens from `--select`/`--ignore`, matching `--exclude`'s pre-existing behavior; both "unknown check" error paths are now one loop instead of two near-duplicate blocks.
- README documents that `.cache/pre_commit_hooks/` is resolved relative to cwd, not a discovered project root.
- No config-file parsing, environment-variable precedence, or project-root discovery exists anywhere in this pipeline, so most of chapters 17/28 don't apply — confirmed by grep, not assumed.
