# Redesign ast-checks/misplaced-comment hooks as a `ruff check` clone

Status: Open — resolved by docs/adr/0008-ruff-check-cli-parity.md, not yet implemented
Kind: Feature

## Problem

`.pre-commit-config.yaml`'s local hooks currently split one module (`ast_checks`) across two hook ids (`ast-checks`, `misplaced-comment`) using a `--disable=misplaced-comment` / `--enable=misplaced-comment --fix` pairing that reads as meaningless without cross-referencing both hook definitions and a YAML comment. Separately, `load_checks()` has a latent bug: passing `--enable` at all causes `--disable` to be silently ignored (`if enabled is not None: ... elif disabled is not None: ...`), so the two can't currently be combined the way `ruff check --select`/`--ignore` can.

## Decision (see docs/adr/0008-ruff-check-cli-parity.md)

Collapse to a single hook, renamed `ruff-extra-rules`, mirroring this repo's own `ruff-check`/`ruff-format` hooks. `--fix` becomes one global flag applying whatever each check's own internal fix logic already considers safe. `--enable`/`--disable` are renamed to `--select`/`--ignore` and fixed to compose like ruff's do.

## Proposed Fix

- Rename the `ast-checks` argparse `prog=` and the `.pre-commit-hooks.yaml`/`.pre-commit-config.yaml` hook id to `ruff-extra-rules`. The Python package stays `pre_commit_hooks.ast_checks` (implementation detail, not renamed — see ADR for why).
- Collapse `.pre-commit-hooks.yaml`'s two hook definitions (`ast-checks`, `misplaced-comment`) into one `ruff-extra-rules` entry.
- Update `.pre-commit-config.yaml`'s local hooks section to the single collapsed hook, with `args: [--fix]` against `files: ^src/` (matching the existing `ruff-check` hook's own `args: [--fix]` convention in the same file).
- Rename the CLI's `--enable`/`--disable` flags to `--select`/`--ignore` (`ast_checks/__init__.py`'s `argparse` setup, `main()`'s validation/parsing block).
- Fix `load_checks()`'s combination logic so `--select`/`--ignore` (formerly `enabled`/`disabled`) compose instead of the current `elif` making `--disable` a no-op whenever `--enable` is also passed.
- Update `README.md` and `docs/adding-a-check.md` for the new hook id and flag names.
- Explicitly out of scope for this item: file-based configuration (a `pyproject.toml`/`ruff.toml`-equivalent settings block for these checks) and ruff's other `check` flags (`--diff`, `--show-fixes`, `--statistics`, `--unsafe-fixes`, `--exit-zero`, etc.). `--list-checks` and `--exclude` are pre-existing and untouched by this item.
- `CONTEXT.md`'s "Hook" glossary entry currently describes the two-hook architecture by name (`ast-checks`, `misplaced-comment`) and must be updated to describe the single collapsed hook once this lands.

## Priority

Not yet prioritized against other open work.
