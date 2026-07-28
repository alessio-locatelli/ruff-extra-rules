# TRI006 unavailable/self-test messages: accept per-process repetition, drop dev-only wording

## Context

`docs/adr/0036-check-unavailable-error-isolated-per-check.md` made `CheckOrchestrator` record a `CheckUnavailableError` once per `check_id` and skip re-invoking that check for every later file in the same process, so `_diagnostics.report()` only ever prints one line per process. That guarantee held, but consumers still reported seeing TRI006's unavailable-`ty` message more than once during a single `prek run` or commit.

Confirmed empirically: neither `prek` nor pre-commit runs a non-`require_serial` hook as a single process. By default they partition the file list across multiple worker processes sized to CPU count, independent of argv-length limits — a throwaway repo with 12 files and a stand-in hook produced 3 separate subprocess invocations on an 8-core machine; adding `require_serial: true` to that hook collapsed it to exactly 1. `.pre-commit-hooks.yaml` does not set `require_serial`, and this codebase has no internal multiprocessing of its own, so every worker process gets its own fresh Python global state and independently discovers `ty` is missing — ADR-0036's per-process dedup is correct but was never sufficient, on its own, to bound the message to once per run.

Separately, the two messages `CheckUnavailableError` carries here (`_INSTALL_HINT`, `_SELF_TEST_FAILED_HINT` in `session.py`) had their own content problems: `_INSTALL_HINT` told every consumer to run `uvx --from ty ty --version` "to warm the uvx cache," advice that only makes sense for this repo's own maintainers benchmarking the linter, not an end user who just wants `ty` on `PATH`. `_SELF_TEST_FAILED_HINT` told a consumer to "try a different installed `ty` version" with no direction — unhelpful given `ty` is pre-1.0 and can change diagnostics either way between releases, and given this repo's own compatibility floor (`pyproject.toml`'s `ty>=X.Y.Z`) only advances when Dependabot's monthly-cadence bump lands, so a consumer's freshly-installed `ty` can already be ahead of what the running release last validated.

## Considered Options

For the repetition itself:

- **`require_serial: true`** on the `ruff-extra-rules` hook: would collapse every worker process into one, making ADR-0036's per-process guarantee also a per-run guarantee. Rejected: this hook entry point bundles every check in `ALL_CHECKS` (forbid-vars, validate-function-name, redundant-type-conversion, ...), not just TRI006, so this would remove prek/pre-commit's parallelism for the whole hook, on every commit, forever — not only on the runs where `ty` happens to be missing. Nothing in this codebase replaces that parallelism internally.
- **Cross-process shared state** (e.g. a sentinel/lockfile so worker processes agree to print only once): rejected. It would need real scoping and lifetime rules (per run? per commit?), has races between workers that start concurrently, and can go stale (a sentinel written before `ty` gets installed mid-session would keep suppressing the hint after the prerequisite is already fixed) — meaningful complexity for what is otherwise a cosmetic repeat.
- **Accept the repeat, bounded by worker count**: adopted.

For the message content, once the above put a floor under how "actionable" this text needs to be:

- **Keep `_SELF_TEST_FAILED_HINT`'s remediation direction-specific** ("try a different installed `ty` version" or similar): rejected as unhelpful given the direction genuinely can't be known from inside the check — the failure looks identical whether the consumer's `ty` is older or newer than what this release validated.
- **Detect and report the consumer's actual installed `ty` version** (via the LSP `initialize` response's `serverInfo`, not currently read, or shelling `ty --version`) to state the direction definitively: rejected as disproportionate — a real addition to the failure path for a message that already gives the consumer everything they need to check themselves.
- **State a floor version derived from `pyproject.toml` at message-construction time** (parsing the `ty>=X.Y.Z` pin live, e.g. via `packaging.requirements.Requirement`): rejected — `packaging` is not a direct dependency of this project today (only an undeclared transitive of `pytest`), and TOML-parsing plus requirement-parsing on a failure path this rarely hit isn't worth the code for one version string.
- **A hardcoded floor version constant, kept honest by a test**: adopted.

## Decision

`CheckOrchestrator`'s per-process dedup (ADR-0036) is unchanged. No cross-process coordination was added. A consumer may see the TRI006 unavailable/self-test message once per prek/pre-commit worker process for a given run — bounded by CPU count under default parallelism, or exactly once if the consumer's own hook config sets `require_serial: true` — rather than a hard guarantee of exactly once per run.

Both `CheckUnavailableError` messages in `session.py` were rewritten:

- `_INSTALL_HINT` drops the uvx-cache-warming instruction entirely; it now only points to `uv tool install ty` or adding `ty` as the consumer's own dev dependency.
- `_SELF_TEST_FAILED_HINT` no longer asserts a direction. It states the `ty` floor this release validated against (a new `_MIN_TY_VERSION` constant) and offers both possibilities: upgrade if the consumer's `ty` predates that floor, or pin to an older `ty` (or wait for a `ruff-extra-rules` update) if it doesn't.
- Both messages now mention `--ignore=redundant-type-conversion` as an explicit opt-out, so a consumer who doesn't want to deal with `ty` at all has a stated escape hatch instead of having to discover `--ignore` exists on their own.
- `_MIN_TY_VERSION` is a plain hardcoded string, not derived from `pyproject.toml` at runtime. `test_min_ty_version_matches_pyproject_pin` (`tests/redundant_type_conversion/test_session.py`) parses `pyproject.toml`'s `dependency-groups.dev` `ty>=X.Y.Z` pin with stdlib `tomllib` plus a small regex and asserts it matches the constant, so a future pin bump that forgets the constant fails CI instead of drifting silently.

## Consequences

- The unavailable/self-test message repeating across a single `prek run`/commit — bounded by CPU count, not file count — is expected behavior, not a bug to file against this repo; a future report of "it prints more than once" should be closed against this ADR unless the repeat scales with file count instead of worker count.
- Both messages are shorter and free of advice relevant only to this repo's own maintainers.
- `_SELF_TEST_FAILED_HINT` can only ever state both remediation directions, never diagnose which one actually applies, since no dynamic version detection was added — a consumer still has to run `ty --version` themselves to tell which side of `_MIN_TY_VERSION` they're on.
- `_MIN_TY_VERSION` duplicates `pyproject.toml`'s own `ty` pin; the guard test is the only thing preventing drift, so removing or skipping that test silently reopens the duplication risk.
- `docs/adr/0036-check-unavailable-error-isolated-per-check.md`'s "one-time failure message" consequence predates this ADR and is now known to mean "once per worker process," not "once per run" — amended there with a pointer to this document rather than rewritten in place.
