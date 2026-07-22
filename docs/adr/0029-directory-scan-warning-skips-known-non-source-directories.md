# Gitignored-directory warning skips well-known non-source directory names

ADR 0028's `_warn_about_ignored_python_files()` warns about any gitignored directory a directory-argument scan skips, since it can't cheaply confirm the directory actually contains a `.py` file. In practice this fired on almost every run rather than the occasional case ADR 0028 anticipated: `__pycache__/`, `.venv/`, `build/`, `dist/`, `*.egg-info/`, and the rest of this project's own `.gitignore` are created by this project's own routine `mypy`/`pytest`/`build`/`uv sync` commands, none of them ever contain hand-written source, and "consistent with `ruff check`" was never actually documented for this warning — only `--select`/`--ignore`/`--fix` CLI parity is (ADR 0008).

## Decision

Before reporting an ignored directory entry, `_is_known_non_source_directory()` checks its basename against a hardcoded set drawn from this project's own `.gitignore` (`__pycache__`, `.venv`, `build`, `dist`, `.pytest_cache`, etc.) plus the `*.egg-info` pattern, and drops a match from the warning silently. A directly-ignored `.py` file is unaffected — it's still always reported, since that's the case a false-clean result actually matters for. An ignored directory with any other name is still reported, unconfirmed, exactly as ADR 0028 already accepted.

## Consequences

- An ignored directory that happens to share one of these well-known names but genuinely contains hand-written source no longer triggers the warning. Accepted: these names are conventionally never used for anything but packaging/tooling output, and the warning was already advisory-only — it never affects what's actually linted, only whether a diagnostic prints.
- The denylist is a fixed set, not derived from the repository's actual `.gitignore` contents at runtime; a project that gitignores a differently-named build/cache directory still gets warned about it. Parsing `.gitignore` semantics to derive this generically was considered and rejected as disproportionate engineering weight for a purely supplementary diagnostic (the same proportionality argument ADR 0028 already made about not recursing into ignored directories).
