# Conservative reporting by default for meaningless-vars

## Context

TR1 (`meaningless-vars`) has always reported every `data`/`result` binding it finds, whether or not `plan_suggestions()` (see `docs/adr/0030-file-local-semantic-variable-rename-suggestions.md`) could propose a replacement name. On a codebase with a large, pre-existing backlog of these names, most reported violations carry no suggested rename, so the check becomes a wall of "use a more descriptive name" warnings a maintainer can't act on at a glance and can't mass-resolve with `--fix`. That discourages a first-time adopter running the check against a brownfield codebase, even though a maintainer starting from a clean codebase, or with only a handful of instances, would rather see everything the check can find.

## Decision

TR1 gains two reporting levels, selected by `--meaningless-vars-level {conservative,aggressive}` (default `conservative`). This mirrors the existing `--redundant-assignment-level` flag (`conservative`/`aggressive`, same default) so both checks share one vocabulary for "how eagerly does this check speak up."

In `conservative` mode, TR1 reports a binding only when `plan_suggestions()` produced a `RenameProposal` for it, at either confidence tier. A `SUGGESTION_ONLY` proposal counts as "having a suggestion" even though it is never auto-fixed — the check's own diagnostic message already presents it as a suggested rename, so gating on "has a suggestion" naturally includes both tiers, not just the auto-fixable one.

In `aggressive` mode, TR1 reports every meaningless-name binding regardless of whether a proposal exists, matching the check's original, only behavior.

`--fix` is unaffected by this flag in either mode: it has only ever applied to `AUTO_FIX`-confidence proposals, and continues to.

## Consequences

- The out-of-the-box experience on a codebase with many un-renameable `data`/`result` bindings is quieter: those bindings go unreported unless `--meaningless-vars-level=aggressive` is passed. This is a breaking change to TR1's default output.
- A codebase whose meaningless-name bindings mostly do have a real suggestion sees little practical difference between the two levels.
- A maintainer who wants full visibility, or is auditing a codebase before deciding whether to adopt the default meaningless-name list at all, opts back in with `--meaningless-vars-level=aggressive`.
