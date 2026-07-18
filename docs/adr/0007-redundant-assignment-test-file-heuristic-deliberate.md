# redundant_assignment's test-file path heuristic is a deliberate, scoped convention

`redundant_assignment/semantic.py:_is_test_file()` (lines 11-30) relaxes TRI005's semantic-value threshold when a file's path contains `tests`/`test` or its filename matches `test_*`/`*_test.py`. Unlike the structurally similar `VariableTracker` scope-tracking decision (`docs/adr/0002-redundant-assignment-scope-tracker-not-unified.md`), this heuristic had no ADR recording it as intentional — it read as an implicit, undocumented coupling between TRI005's output and a file's location in the tree, and `README.md` never mentions it exists.

This project's own `CLAUDE.md` states it is a personal hobby project not used by anyone else, and explicitly rules out designing for hypothetical future requirements. `_is_test_file()`'s hardcoded conventions happen to match this repo's own layout (`tests/`, `test_*.py`); a hypothetical outside consumer with a different layout (`spec/`, `__tests__/`) would get silently different behavior with no way to configure it — but no such consumer currently exists.

## Considered Options

- **Make the test-directory convention configurable, e.g. deriving it from `[tool.pytest.ini_options] testpaths` in `pyproject.toml`** (this repo already sets `testpaths = ["tests"]`, and already reads `pyproject.toml` via `tomllib` elsewhere, for `forbid-vars`' autofix config): rejected for now — there is no current consumer whose layout differs from the hardcoded convention, and building configurability for a hypothetical one contradicts this project's own stated scope. Revisit if this project ever gains outside consumers.
- **Remove the heuristic entirely, rely on inline `# pytriage: ignore=TRI005`**: rejected — the heuristic exists because test code idiomatically uses single-use, low-semantic-value variables (`result = f(); assert result == x`) far more than production code does; removing it would just push the same suppressions onto every test file individually, which is worse for the maintainer, not better.
- **Keep the heuristic as-is, record it as deliberate**: adopted.

## Consequences

- No code change to `_is_test_file()`.
- `README.md`'s TRI005 section gains an explicit line documenting the test-directory relaxation — its absence made this easy to forget, since nothing in the user-facing docs currently mentions it.
- A related, separate idea is not part of this decision: this repo's own self-dogfooding config (`.pre-commit-config.yaml`) currently excludes `tests/` entirely (`files: ^src/`) from every check, not just TRI005. Enabling `ast-checks` against `tests/` in this repo's own pre-commit config is a distinct future feature — tracked separately in `docs/work-items/archived/10-lint-tests-directory.md` — that would need to reconcile with `tests/fixtures/` containing deliberately "bad" example code and the test suite's own idiomatic use of forbidden names. (Resolved: see `docs/adr/0009-lint-tests-directory.md`.)
- Tracked as `docs/work-items/archived/08-document-is-test-file-heuristic.md`.
