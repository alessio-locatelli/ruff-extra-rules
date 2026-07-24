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
- Develop and test against Linux (or WSL) only — don't add new Windows- or macOS-specific code paths or features. Where a stdlib API this codebase already depends on is genuinely POSIX-only (e.g. `fcntl`), guard its import/use so an unsupported platform degrades with one clear warning instead of hard-crashing; see `docs/adr/0020-behavioral-contract-audit-cross-platform-behavior.md`.
- Reuse existing shared code (`_cache.py`, `_prefilter.py`, `_scope.py`, etc.) rather than reimplementing it per check.

### Suggested Check Architecture

Hybrid pipeline:

1. If possible, filter candidate files quickly using `ripgrep`, `ast-grep`, or `git grep`.
2. Parse and process the files using a Python parser or faster alternatives (`tree-sitter`, `ast-grep`, native Rust).

See [docs/adding-a-check.md](docs/adding-a-check.md) for the full walkthrough of implementing and registering a new check.

## Docstrings and code comments

- Do not write docstrings.
- Do not write code comments.
- **No historical/postmortem framing:** Phrases such as "the old default", "before this flag existed", "used to qualify for X", or "this code replaced database X" belong in postmortems, ADRs, specifications, or git commit message bodies.
- Do not repeat in prose what is already expressed by tests. Unlike prose, tests are a more reliable contract that stays in sync with the code.
- You may add a concise docstring or code comment only when the information is not already documented elsewhere **and**:
  - A business or architecture decision cannot be derived from the code (e.g., `"""We use service X instead of Y because of rate limits."""`).
  - A non-obvious hack or pitfall exists that may look like a code problem if left unexplained (e.g., `# Temporarily reduce the batch size to work around the OOM in the cloud.`).
  - There is a need to reference an external resource (e.g., `Related issue <link>.` or `See ADR-0042`).
  - There is a need to explain **why** a non-obvious action is taken (e.g., "Early exit because all items were processed", "Used a real ID in a test because…").
- Never duplicate ADRs, specifications, or any other documentation in the code. If the code requires an explanation, add a reference (e.g., `# See ADR-0042`, `# See openspec/path-to-spec/`).
- If you delete something from the file, the "why?" prose belongs in the commit body or documentation (specifications, postmortems, ADRs) — not as inline prose about functionality that no longer exists.
- If a file is already bloated with prose that violates these rules, that is not an excuse to bypass them. Instead, signal that the code needs decluttering — retain any indispensable rationale as an ADR reference instead.
- Immediately delete any pre-existing stale comments or prose that violates these rules.

## README and user-facing docs

- User-facing prose (README.md, `--help` text, CLI docs) must describe _current_ behavior only, in short, high-level, user-friendly language.
- **No historical/postmortem framing.** Phrases like "the old default", "before this flag existed", "used to qualify for X" are meaningless to a reader who only has the current codebase — they imply a diff against a history the reader can't see and doesn't care about. Describe what the feature does today, full stop.
- **No internal implementation details.** Don't expose internal scoring/threshold numbers (e.g. "semantic value score ≤ 10", "score < 50") or other implementation-level mechanics in a README. A README is a short, high-level description for a regular user, not a spec for the internals — use a concrete illustrative example instead of a formula.

## ADRs

An ADR records a durable architectural decision, not the history of the investigation that led to it: the problem and context, the decision, the important alternatives/trade-offs, the consequences, and any intentional limitation a future maintainer needs to know. Keep it at that level. Do not fold in: a chronological narrative of review rounds or who/what found which issue; every individual bug reproduction (one or two representative examples is enough — prefer the general rule the examples taught over the examples themselves); regression-test names (say what behavior is covered, not which test covers it); or an inventory of private helper functions (explain why the architecture needs the concept, not which function implements it).

When an audit or investigation produces substantial evidence worth keeping, put the detailed findings in `docs/audits/` and have the ADR link to it rather than duplicating it. Before adding detail to an ADR, ask: would this still be useful if the implementation were rewritten but the decision stayed the same? If not, it belongs in `docs/audits/`, the tests, or the commit/PR history instead.

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

### Lint

```bash
ruff check --fix .
ruff format .
uv run mypy src/ tests/
npx prettier@latest . --write --cache
taplo fmt pyproject.toml
uv run -- python -m slotscheck src tests
```

### Test

```bash
uv run -- coverage run -m pytest
uv run -- coverage report
strict-no-cover
```

## Agent skills

### Issue tracker

Issues live in GitHub Issues for `alessio-locatelli/ruff-extra-rules`; use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context layout — `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
