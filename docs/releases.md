# Releases

A release is a git tag (`v0.1.0`), a [GitHub release](https://github.com/alessio-locatelli/ruff-extra-rules/releases) carrying that version's notes, and a section in the [changelog](../CHANGELOG.md). Pin a tag in your `.pre-commit-config.yaml` and read the notes before moving to a newer one.

## What the version number tells you

Versions are `0.MINOR.PATCH`.

- **MINOR** — a run can come out differently on code you have not touched: a new check, a check that reports something it used to miss, a renamed or removed hook id, flag, configuration key or check id, or a different edit from `--fix`. A bug fix counts: what decides this is what your run does afterwards, not why it changed.
- **PATCH** — a run that was already passing comes out the same: a crash fixed, a false report dropped, performance, documentation.

Anything that can turn a green run red, or rewrite your code differently, gets a MINOR release. There are no compatibility shims and no deprecation period, so the release notes are the migration path — every such change is written up before it ships. The `--fix` reporting change in [0.0.50](../CHANGELOG.md) is what one looks like.

## What the version number covers

- The hook ids in `.pre-commit-hooks.yaml`.
- The flags of `python -m pre_commit_hooks.ast_checks` and the `[tool.ruff-extra-rules]` keys in your `pyproject.toml`.
- Check ids, their `TR` codes, and the `# pytriage:` suppression comment.
- What `--fix` rewrites, the outcome labels a run prints (`[FIXED]`, `[FIXABLE]`, and the rest), and the exit codes.

It does not cover the exact wording of a message, importing `pre_commit_hooks` from your own code (this is a hook, not a library), or anything about the cache.

## Upgrading

Move `rev:` to the newer tag and run the hooks over your whole project once (`prek run --all-files`) rather than waiting for the next commit to touch a file. A MINOR upgrade with `fix = true` can rewrite code you did not stage, so let it run on a clean working tree and review the diff.
