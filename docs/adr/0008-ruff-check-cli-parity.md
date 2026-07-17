# Redesign the check CLI as a `ruff check` clone

`.pre-commit-config.yaml` currently exposes two hook ids backed by the same `ast_checks` module: `ast-checks` (`args: [--disable=misplaced-comment]`, report-only) and `misplaced-comment` (`args: [--enable=misplaced-comment, --fix]`). Nothing recorded why two invocations of the same module exist, or why one disables exactly the check the other enables — a fresh reader has to cross-reference both hook definitions and a YAML comment to reconstruct the intent (TRI00x renames should get human review before landing; misplaced-comment's fix is a purely mechanical text move, safe to always auto-apply).

We're collapsing this into a single hook, renamed `ruff-extra-rules` to match this repo's own `ruff-check`/`ruff-format` hooks already in `.pre-commit-config.yaml`, with `--fix` as one global flag that applies whatever each check's own internal fix logic already considers safe (`validate-function-name`'s complexity gate, `forbid-vars`' scope-safe renaming, `redundant-assignment`'s conservative fixable check) — mirroring `ruff check [--fix]` exactly, rather than reinventing our own flag conventions. `--enable`/`--disable` are renamed to `--select`/`--ignore` to match ruff's own names, and their combination logic is fixed to compose the way ruff's do: previously, passing `--enable` at all caused `--disable` to be silently ignored entirely (`load_checks()`'s `if enabled is not None: ... elif disabled is not None: ...`), so `--select`+`--ignore` together didn't behave like real `--select`+`--ignore`. Only `--fix`, `--select`, and `--ignore` are in scope; file-based configuration and ruff's other `check` flags (`--diff`, `--show-fixes`, `--statistics`, `--unsafe-fixes`, `--exit-zero`, etc.) are deliberately deferred to avoid scope creep.

Only the hook id and the CLI's `prog=` string are renamed. The Python package stays `pre_commit_hooks.ast_checks` — that name is accurate implementation detail with no public surface, and renaming it would mechanically touch ~30 files (every test, ADR, README, `scripts/benchmark.py`) for no behavioral change.

## Considered Options

- **Keep the two-hook split, just rename the ids more clearly** (e.g. `ast-checks-report` / `ast-checks-fix`): rejected — still requires a reader to reconstruct _why_ the same module runs twice, whereas collapsing to one flag removes the need for the split to exist at all.
- **Full package rename (`ast_checks` → `ruff_extra_rules`) to match the new hook id**: deferred — no behavioral value, purely cosmetic, and would touch ~30 files.
- **Collapse to one hook with a single `--fix` flag, rename `--enable`/`--disable` to `--select`/`--ignore`**: adopted.

## Consequences

- `forbid-vars`, `validate-function-name`, and `redundant-assignment` renames now auto-apply under `--fix` in this repo's own `src/` dogfooding hook, with no separate report-only gate forcing a human to review the suggested name before it lands. Each check already gates its own fix safety internally (complexity/collision checks), so this trades a human judgment call on naming quality for less friction — a deliberate choice, not a safety regression.
- `--list-checks` and `--exclude` are pre-existing and unaffected (`--exclude` already matches ruff's own flag name); they are not part of this redesign's scope.
- `docs/work-items/10-lint-tests-directory.md` (extending self-checking to `tests/`) is put on hold until this redesign lands, since it would otherwise be scoped against a CLI surface that's about to change.
- Tracked as `docs/work-items/11-ruff-check-cli-parity.md`.
