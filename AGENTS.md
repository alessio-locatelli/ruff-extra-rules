# Ruff Extra Rules

Extra Python rule checks and fixups for pre-commit/prek, meant to run alongside ruff rather than replace it.

## Project Context

- Personal hobby project, maintained solo by Alessio through coding agents — not used by anyone else.
- Originally built in "vibe-coding" mode: none of the existing architecture or design decisions were deliberate choices. The messiness is inherited, not intentional — don't assume unwritten rationale behind odd patterns.
- Before assuming a pattern here is deliberate, check whether it's covered by an ADR in `docs/adr/`. If it isn't, treat it as accretion, not intent.
- Breaking changes are allowed and expected. Don't design backward-compatibility shims, deprecation warnings, or migration paths for this project's own hook ids/CLI surface.
- No hosted CI exists — this is a single-maintainer local repo, and the full command sequence under [Development](#development) is run locally before every commit instead. Don't add a CI badge or workflow implying automated checks run unless a real CI workflow is actually added.

## Development Guidelines

- The repository contains multiple independent checks; each focuses on one task (e.g., only fixing function naming, or only fixing code comments).
- Checks must support being run via [prek](https://github.com/j178/prek) (a drop-in alternative to pre-commit).
- Performance is critical.
- Support only the current stable Python version (currently `>=3.14`). Support for older versions is best-effort only ("may still work, no guarantee") and must not bloat the code with compatibility branches — this limits ongoing maintenance effort.
- `except SomeError, OtherError:` (no parentheses) is valid Python 3.14 syntax — [PEP 758](https://peps.python.org/pep-0758/) — equivalent to `except (SomeError, OtherError):`. It is not Python 2's `except Type, name:` catch-and-bind form (that form was removed in Python 3.0). Do not "fix" it and do not re-investigate it as a bug. A vulture warning flagging this syntax as suspicious is a false positive from a linter that predates PEP 758 and can be ignored.
- Assume every file these hooks process already passed `check-ast` and `ruff` (see `.pre-commit-config.yaml`) — i.e. it's syntactically valid Python. Don't add defensive handling for invalid syntax or non-Python input.
- Assume Linux (or WSL) only. Don't add if/else branches to support Windows or macOS.
- Reuse existing shared code (`_cache.py`, `_prefilter.py`, `_scope.py`, etc.) rather than reimplementing it per check.

### Suggested Check Architecture

Hybrid pipeline:

1. If possible, filter candidate files quickly using `ripgrep`, `ast-grep`, or `git grep`.
2. Parse and process the files using a Python parser or faster alternatives (`tree-sitter`, `ast-grep`, native Rust).

See [docs/adding-a-check.md](docs/adding-a-check.md) for the full walkthrough of implementing and registering a new check.

## Docstrings and code comments

- Do **not** add docstrings.
- Do **not** add code comments.
- You may add a concise docstring or code comment **only with**:
  - Business or architecture decisions that cannot be derived from the code (e.g., `"""We use service X instead of Y because of rate limits."""`).
  - Non‑obvious hacks or pitfalls that may look like a code problem if not explained (e.g., `# Temporarily reduce the batch size to work around the OOM in the cloud.`).
  - A need to reference an external resource (e.g., `Related issue <link>.` or `See ADR-0042`).
  - A need to explain **why** a non-obvious action is taken (e.g., "Early exit because all items were processed", "Used a real ID in a test because …").

## Commands

### Setup

```bash
uv sync         # creates .venv, installs dependencies
prek install    # installs this repo's own hooks (dogfooding); prek is a standalone binary, not a uv dependency
```

### Python package and project manager

Use [`uv`](https://docs.astral.sh/uv/).

### Running checks directly (no prek/pre-commit needed)

```bash
uv run python -m pre_commit_hooks.ast_checks --list-checks
uv run python -m pre_commit_hooks.ast_checks --select=forbid-vars,validate-function-name src/
uv run python -m pre_commit_hooks.ast_checks --ignore=redundant-assignment --fix src/
```

## Development

Run before committing or after making code changes:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src/ tests/
npx prettier . --write --cache
taplo fmt pyproject.toml
uv run coverage run -m pytest
uv run coverage report
uv run strict-no-cover
```

## Agent skills

### Issue tracker

Issues live in GitHub Issues for `alessio-locatelli/ruff-extra-rules`; use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context layout — `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
