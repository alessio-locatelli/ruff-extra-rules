# Add opt-in checks without replacing the defaults

`unused-pytriage` is intentionally opt-in because it reports cleanup work rather than enforcing a source rule. A project that wants that audit on every normal run should not need to enumerate every default check in `select`, because that list would drift as checks are added or entry points change.

## Decision

Add `extend-select` to the global configuration and `--extend-select` to the CLI. The setting adds check ids to the normal selection:

- with neither selector, it adds to the checks whose `default_enabled` value is true;
- with `select`, it adds to that explicit set;
- `ignore` is applied after both selectors;
- command-line values replace the corresponding configuration-file value;
- an empty extension is a no-op.

`select` remains restrictive, so existing configurations retain their meaning. `extend-select = ["unused-pytriage"]` is the project-level way to enable TR8 alongside the normal rules.

When TR8 is selected, its audit also bypasses regular checks' candidate prefilters for files containing a `# pytriage` comment. A selected check that has no candidate on the comment's line must still contribute evidence that the suppression is unused; otherwise the prefilter would make the audit depend on unrelated source text.

## Consequences

The configuration surface gains one global key and one matching CLI flag. The published hooks continue to share one project-wide configuration; the main hook owns the normal checks, while the dedicated ty hook owns TR6 and TR8 when the audit is enabled. Enabling TR8 remains an explicit user choice and TR8 remains report-only.
