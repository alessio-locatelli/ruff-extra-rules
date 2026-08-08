# Ruff Extra Rules

Extra Python rule checks and fixups for pre-commit/prek, meant to run alongside ruff rather than replace it.

## Development Guidelines

### General

- You must follow SOLID, DRY principles, and maintain high-quality scalable and extendable architecture.
- You must not introduce workarounds, hacks, or kludges to accommodate poor or incompatible code.
- If existing code is flawed, incomplete, or poorly designed, you must refactor or fix it, even if this requires project-wide changes.
- You must not bypass proper design to "make things work."
- Don't assume unwritten rationale behind odd patterns. Before assuming a pattern here is deliberate, check whether it's covered by an ADR in `docs/adr/`. If it is not, treat it as an existing issue and report it to a user.
- [docs/behavioral_contract.md](docs/behavioral_contract.md) is the MUST/MUST-NOT checklist. Consult it — especially before touching caching, fixing, file discovery, or anything that runs under pre-commit/prek's own parallel worker processes — rather than re-deriving requirements it already states.
- Breaking changes are allowed and expected. Don't design backward-compatibility shims, deprecation warnings, or migration paths for this project's own hook ids/CLI surface.
- The repository contains multiple independent checks; each focuses on one task (e.g., only fixing function naming, or only fixing code comments).
- Checks must support being run via [prek](https://github.com/j178/prek) (a drop-in alternative to pre-commit).
- Performance is critical. For each relevant command, measure a cold run with its setup and cache state recorded, then immediately measure a warm run of that same command. If both the direct AST-check command and prek command are relevant, collect a separate cold/warm pair for each; record every duration in the commit body for baseline comparison.
- Use `@functools.cache` only when calls reuse equal, hashable arguments; results are deterministic and side-effect-free; and the process lifetime and input cardinality make unbounded retention safe. Use a bounded or per-run cache for many distinct inputs.
- Support only the current stable Python version (currently `>=3.14`). Support for older versions is best-effort only ("may still work, no guarantee") and must not bloat the code with compatibility branches — this limits ongoing maintenance effort.
- Assume every file these hooks process already passed `check-ast`, `ruff check --fix`, and `ruff format` (see `.pre-commit-config.yaml`) — i.e. it's syntactically valid, canonically-formatted Python. Don't add defensive handling for invalid syntax, non-Python input, or non-canonical formatting (e.g. unusual whitespace around a call's parentheses) that `ruff format` would already have normalized away.
- Develop and test against Linux (or WSL) only — don't add new Windows- or macOS-specific code paths or features. Where a stdlib API this codebase already depends on is genuinely POSIX-only (e.g. `fcntl`), guard its import/use so an unsupported platform degrades with one clear warning instead of hard-crashing; see `docs/adr/0020-behavioral-contract-audit-cross-platform-behavior.md`.
- Common functionality (e.g., caching, parsing, etc.) should be extracted into helper modules and reused.
- `except SomeError, OtherError:` (no parentheses) is valid Python 3.14 syntax — [PEP 758](https://peps.python.org/pep-0758/) — equivalent to `except (SomeError, OtherError):`. It is not Python 2's `except Type, name:` catch-and-bind form (that form was removed in Python 3.0). Do not "fix" it and do not re-investigate it as a bug. A vulture warning flagging this syntax as suspicious is a false positive from a linter that predates PEP 758 and can be ignored.

### Suggested Check Architecture

Hybrid pipeline:

1. If possible, filter candidate files quickly using `ripgrep`, `ast-grep`, or `git grep`.
2. Parse and process the files using a Python parser or faster alternatives (`tree-sitter`, `ast-grep`, native Rust).

See [docs/adding-a-check.md](docs/adding-a-check.md) for the full walkthrough of implementing and registering a new check.

### Writing tests

- Use `@pytest.mark.parametrize` when the same test logic should be run against multiple input/output cases. Prefer it over duplicating nearly identical test functions.
- Setup, teardown, or cleanup logic should be placed outside the test function itself. For example, a fixture can yield an object and perform cleanup.
- Tests should not duplicate the same code (e.g., `try`/`finally` blocks or inner functions). Extract and reuse such logic instead.
- Do not write tests for impossible scenarios solely to achieve 100% code coverage. If code is unused in production, delete it immediately—do not mask it with mocking or patching in tests.

### Docstrings and code comments

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

## User-facing prose (README, program output)

- **No internal implementation details.** Don't expose internal scoring/threshold numbers (e.g. "semantic value score ≤ 10", "score < 50") or other implementation-level mechanics in a README. A README is a short, high-level description for a regular user, not a spec for the internals — use a concrete illustrative example instead of a formula.
- User-facing prose (README.md, `--help` text, CLI docs) must describe _current_ behavior only, in short, high-level, user-friendly language.
- **No historical/postmortem framing.** Phrases like "the old default", "before this flag existed", "used to qualify for X" are meaningless to a reader who only has the current codebase — they imply a diff against a history the reader can't see and doesn't care about. Describe what the feature does today, full stop.
- Non-recoverable persistent failures (such as missing files, missing dependencies, permission or access errors, etc.) must not go unreported. At minimum, produce a visible error message so users can either take corrective action or report the issue.
- **Do not compete with official documentation:** Do not teach users how to install third-party tools, how to debug or configure their environment, etc. Use a short hint and a reference to the official resource.

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
uv run python -m pre_commit_hooks.ast_checks --select=meaningless-vars,validate-function-name src/
uv run python -m pre_commit_hooks.ast_checks --ignore=redundant-assignment --fix src/
```

## Development

### Lint

```bash
ruff check --fix .
ruff format .
uv run mypy src/ tests/
taplo fmt pyproject.toml
npm run format --silent
uv run -- python -m slotscheck src tests
```

If edited CI:

```bash
ZIZMOR_OFFLINE=true zizmor --fix=all --persona=auditor --collect=all .
```

### Test

```bash
uv run -- coverage run -m pytest -qq
uv run -- coverage report
strict-no-cover
```

### Committing to this repository

This repo runs its own checks against itself, which creates three gotchas that don't apply to consumers.

- **The pinned `ruff-extra-rules` hook lags the working tree.** `.pre-commit-config.yaml` also depends on this repo by tag (`rev:`), which `prek` installs as its own copy built from that tag — it never sees local changes. So it keeps CLI `args:` for anything the working tree has since renamed or moved into `pyproject.toml`, and that duplication is deliberate until a new tag is released and `rev:` is bumped. Don't delete those args to "deduplicate", and don't bump `rev:` yourself — that needs a real tagged release on the remote.
- **The `local-*-aggressive` hooks run live working-tree code, at `permissive` level, against `src/` and `tests/` on every commit.** They no longer pass `--fix` explicitly; it comes from `[tool.ruff-extra-rules] fix = true`. Widening any check's default detection can therefore fail your very next commit. It can also rewrite code you just wrote, mid-commit, but only through `local-ruff-extra-rules-aggressive` — `local-ruff-extra-rules-ty-aggressive` runs `redundant-type-conversion` alone, which reports violations and never fixes. Before widening a default, search `src/` and `tests/` for what it would newly catch and reconcile anything that has no auto-fix path.
- **`ruff check`/`ruff format` above use the standalone binary, not the pinned `ruff-pre-commit` rev.** When the two drift, the standalone one can make edits the pinned one rejects — including stripping `# noqa` comments the pinned version still wants. Compare `ruff --version` against the `rev:` under `astral-sh/ruff-pre-commit` in `.pre-commit-config.yaml`; if they differ, use `prek run ruff-check --files <paths>` instead and tell the user about the drift.

## Agent skills

### Issue tracker

Issues live in GitHub Issues for `alessio-locatelli/ruff-extra-rules`; use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context layout — `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
