# Report a violation another check's fix removed as indirectly resolved

## Context

`docs/behavioral_contract.md` ch. 1 requires distinguishing "a diagnostic that was fixed" from "a diagnostic that disappeared as a side effect of another fix". `docs/audits/0002-behavioral-contract-audit-fix-engine.md` judged this not worth closing for six fixed, first-party checks run in a fixed order, since nothing false was ever printed: `CheckOrchestrator._apply_fixes` recomputes each check's violations immediately before that check's own `fix()` call, and `_refresh_stale_positions` replaces every still-open entry with a fresh `check()` result afterwards, so a violation another check's fix already removed simply fell out of both lists.

Issue [#138](https://github.com/alessio-locatelli/ruff-extra-rules/issues/138) revisits that call: silently dropping the entry is indistinguishable from never having reported it, so a `--fix` run's output no longer accounts for every violation the same run's non-fix output would have shown. The larger the fixer set grows, the more that gap obscures what a run actually did.

The obstacle is identity. A violation has no stable identifier across a fix: `Violation` objects are replaced wholesale rather than updated, positions shift when a fix adds or removes lines, and message text can change for the very same violation (a rename applied by one check can appear inside another check's message). `_refresh_stale_positions` already documents why message matching is not a safe basis for deciding that two entries are "the same violation".

## Considered Options

- **Leave the entry absent, as before**: rejected by the issue. It is the one outcome that cannot be told apart from "this was never a violation".
- **Match each initial violation to a final one by message text**: rejected as the deciding mechanism. A fix that renames a symbol changes the text of another check's message for the unchanged violation, which would report that violation twice — once as its current entry, once as a fabricated indirect resolution.
- **Give every violation a stable identity (e.g. an anchor into surrounding source) and track it across fixes**: rejected for this decision. It would have to survive arbitrary edits by an arbitrary fixer to be worth more than counting, which is a much larger change than the outcome it would serve, and every check would have to participate in it.
- **Decide by counting each check's entries, and use message matching only to pick which of the missing ones to name**: adopted.

## Decision

`_apply_fixes` keeps the violations it was handed, grouped by `check_id`, before any fix runs — including the non-fixable ones, since ch. 1 draws no line there and a check with no autofix at all can just as easily have its finding removed by somebody else's fix. After a fix of this run's own is known to have changed the file, `_record_indirect_resolutions` compares that snapshot against the final list. For each check, the initial entries that are no longer in the list are weighed against the entries that replaced them; any surplus is that check's count of violations nothing in this run can account for, and exactly that many are marked via `mark_resolved_indirectly()` and put back into the report. Message matching only orders the candidates within a check, so a violation whose message merely drifted keeps its single current entry rather than being reported twice.

Diagnostics report the outcome as `[RESOLVED INDIRECTLY]`, distinct from `[FIXED]` (this check's own fix resolved it) and from the `[FIXABLE]`/"Run with --fix" hint, since there is nothing left for the user to run. It counts as a violation for the exit code, exactly as `[FIXED]` does: the file changed, so the working tree still needs review.

An outcome a check's own `fix()` already decided always wins. A rejected, errored, failed, or aborted violation is left exactly as it was — those report something the user may need to act on, and `_refresh_stale_positions` already refuses to touch a `check_id` holding one, so no such entry can go missing in the first place.

A run that detected an external edit (`ConcurrentModificationError`, ADR-0042) attributes nothing at all, for any check. That case also sets "the file changed this run", because the edit shifts positions the same way a fix does — but the writer was somebody else, and the same edit that aborted a fix can equally be what removed another check's violation. There is no way to tell that apart from a fix of this run's own having removed it, and naming the wrong culprit is worse than the absence this decision otherwise exists to close.

## Consequences

- Every violation a run reports before fixing has one outcome in that run's report: fixed, indirectly resolved, rejected, errored, failed, aborted, or still open. A `--fix` run's report no longer shrinks silently relative to the same run's own findings.
- The classification is by count, not by identity, so it is accurate about _how many_ of a check's violations were resolved by another fix and only best-effort about _which_. When a check's messages are identical or drift, the named violation can be the wrong one of that check's own set; the count, and so the number of report lines, stays correct.
- Attribution is only as good as its gate: a violation that disappears without a fix of this run's own changing the file — because no fix landed at all, or because an external edit was detected — is still dropped rather than attributed to a fix that did not happen. That is the pre-existing behavior, kept deliberately for the cases this decision cannot speak to truthfully.
- A check running before the one whose violation it removes produces an indirect resolution; the same pair in the opposite order produces an ordinary `[FIXED]`. Both are truthful for the order they ran in, and `ALL_CHECKS` fixes that order, so a given input keeps producing a given report.
- No `ASTCheck` protocol change: checks are unaware of this, and a new check gets the behavior without opting in.
- `docs/audits/0002-behavioral-contract-audit-fix-engine.md`'s "not applicable" finding for this ch. 1 item is superseded; the audit report is annotated rather than rewritten, matching the precedent set for `docs/adr/0042-abort-fixes-on-concurrent-source-modification.md`.
