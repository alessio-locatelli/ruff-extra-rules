# Declare which hooks a project runs to catch configuration no hook applies

ADR-0045 makes each published hook accept and ignore settings for checks it can't run, so one shared configuration works for both hooks. The cost is that a project which configures `redundant-type-conversion` or `redundant-dict-get` and enables only `ruff-extra-rules` gets no signal: those settings never take effect, and every run exits 0.

Neither hook can detect this by itself. A project running both hooks may name the ty hook's checks in `select` on purpose, so the main hook can't treat that as an error or warn on every run. A hook also can't reliably find out whether its sibling runs, because a project may invoke it from `.pre-commit-config.yaml`, `prek.toml`, a wrapper script, or CI.

## Decision

Add an optional global `hooks` key to `[tool.ruff-extra-rules]`, which lists the published hook ids the project runs. If `hooks` is absent, ADR-0045's behavior is unchanged.

If `hooks` is set, the run exits 2 with a configuration error when it finds a check that is named but that no listed hook runs. A check counts as named when it appears in `select` or `extend-select`, when it has its own sub-table, or when it is passed through the matching command-line selector or per-check option flag. The message names the check, the setting and its source, and the hooks that own the check.

`ignore` and `per-file-ignores` are not checked against `hooks`. Turning off a check that never runs has no effect, so it can't hide a dead configuration. The same applies to checks that run only because they are enabled by default: the project never asked for them.

The check depends only on the configuration and the command line, never on which entry point is reading them. So `ruff-extra-rules`, `ruff-extra-rules-ty`, and `python -m pre_commit_hooks.ast_checks` all accept or reject the same file the same way. This follows ADR-0045's rule that a shared file is validated identically by every hook.

`hooks` has no command-line flag. It describes the whole project, not one invocation. A flag would live in a single hook's `args:` and describe only that hook, and that per-hook form is the alternative this decision rejects.

## Considered Options

- **A hook-specific command-line flag that reports checks the current hook can't run**: rejected. It can't be set in `pyproject.toml` without firing in every project that runs both hooks. It would be the only setting with no file equivalent. And if a project running both hooks adds it to the main hook's `args:`, that project would get false errors.
- **Always warn from the main hook when a ty-hook check is named**: rejected. A correct setup that runs both hooks would see the warning on every run.
- **Detect whether the sibling hook runs**, for example by reading `.pre-commit-config.yaml` or leaving a marker in the cache: rejected. Hooks can be invoked from too many places to detect reliably. A marker would also give the wrong answer on a fresh clone and on every cold CI cache.

## Consequences

- The signal is opt-in. A project has to know to set `hooks`, and if `hooks` lists a hook the project doesn't actually run, the check passes even though that hook's settings never take effect.
- Which hook owns which check is now part of the configuration contract. Moving a check from one hook to the other can break a configuration that sets `hooks`, so that move needs a changelog entry.
- A repository whose pinned hook release predates `hooks` rejects the key as an unknown field. A project can't set it until every pinned hook release reads it.
