"""The linting application itself: running the enabled checks over a set of
files, caching their results, and applying fixes.
"""

from __future__ import annotations

import ast
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pre_commit_hooks._cache import CacheManager
from pre_commit_hooks._prefilter import batch_filter_files

from . import ALL_CHECKS
from ._base import (
    ASTCheck,
    FixValidationError,
    Violation,
    is_fix_errored,
    is_fix_failed,
    is_fix_rejected,
    is_fixed,
    mark_fix_errored,
    mark_fix_rejected,
    mark_fixed,
    read_source_with_encoding,
)

logger = logging.getLogger("ast_checks")

# src/pre_commit_hooks/ — the tree CacheManager.compute_tree_hash() hashes to
# invalidate every cached result whenever any check's own code, or shared
# code it depends on, changes.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Matches a pre-fix Violation against a fresh check() re-run's own new
# Violation objects, which can never share object identity with it.
type ViolationKey = tuple[int, int, str]  # (line, col, message)


def _fingerprint_default(value: object) -> object:
    """`json.dumps(..., default=...)` handler for the value shapes a check's
    own `vars()` can contain but that `json` can't natively serialize: a
    `set`'s iteration order depends on PYTHONHASHSEED (randomized per
    process by default), so it's sorted first rather than dumped as-is —
    otherwise the same config would fingerprint differently across process
    runs, making the cache key (and so the cache itself) useless. Anything
    else falls back to repr() rather than raising, since vars() can pick up
    values this generic and unopinionated (e.g. a test's monkeypatched
    instance attribute) that were never meant to be "config" in the first
    place — the fingerprint just needs to not crash construction, not be
    meaningful for every possible value a check instance could ever hold.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return repr(value)


def _fingerprint_check(check: ASTCheck) -> str:
    """Stable fingerprint of a check instance's own state — effectively its
    constructor arguments, so two instances of the same check with different
    configuration wouldn't share a cache entry. Checks with no `__init__`
    override (most of them) have no instance attributes at all, so this is
    deliberately a generic `vars()` dump rather than something every check
    must opt into.
    """
    return json.dumps(vars(check), default=_fingerprint_default, sort_keys=True)


class CheckOrchestrator:
    """Orchestrates running multiple AST checks on Python files.

    This class manages the workflow of:
    1. Pre-filtering files based on aggregated patterns
    2. Caching check results
    3. Parsing files once and running all checks
    4. Applying fixes when requested
    5. Reporting violations
    """

    def __init__(
        self,
        checks: list[ASTCheck],
        *,
        fix_mode: bool = False,
    ) -> None:
        self.checks = checks
        self.fix_mode = fix_mode
        self.cache = CacheManager(hook_name="ruff-extra-rules", cache_version=self._generate_cache_key())
        # Populated by process_files() with every candidate file _check_file()
        # returned None for (couldn't be read/decoded or failed to parse) —
        # reset at the start of each call, so main() can report them instead
        # of letting them vanish silently from all_violations with no trace.
        self.unprocessable_files: list[str] = []
        # (filepath, check_id) pairs where a check's own check() or fix()
        # raised unexpectedly (in _check_file or _apply_fixes respectively)
        # — reset at the start of each process_files() call, same as
        # unprocessable_files. Without this, a check that crashes on every
        # file it sees would make the whole run look clean (zero
        # violations, exit code 0) whenever no other check reports
        # anything for the same files; and a fix() that raises after
        # already resolving every violation it was given would otherwise
        # leave no trace at all once every one of them gets marked fixed.
        self.rule_failures: list[tuple[str, str]] = []

    def process_files(self, filepaths: list[str]) -> dict[str, list[Violation]]:
        """A file that couldn't be read or parsed has no entry in the
        returned dict (indistinguishable from "processed, zero
        violations") — check `self.unprocessable_files` for those. A file
        where one check crashed while others ran fine can still have an
        entry here (the other checks' violations), but its results are
        incomplete — check `self.rule_failures` for those.
        """
        self.unprocessable_files = []
        self.rule_failures = []

        if not filepaths:
            return {}

        patterns = []
        for check in self.checks:
            check_patterns = check.get_prefilter_pattern()
            if check_patterns:
                patterns.extend(check_patterns)

        # OR logic: a file is a candidate if it contains ANY pattern. No
        # patterns means every file needs to be checked.
        candidate_files = batch_filter_files(filepaths, patterns) if patterns else filepaths

        if not candidate_files:
            return {}

        # self.cache's own cache_version (set at construction from
        # _generate_cache_key()) already gates staleness — no separate
        # per-file cache_key needed here.
        all_violations: dict[str, list[Violation]] = {}

        for filepath_str in candidate_files:
            filepath = Path(filepath_str)

            # Skip the cache in fix mode, since the file will be modified.
            cached_violations: list[Violation] | None = None
            if not self.fix_mode:
                cached_violations = self._get_cached_violations(filepath)

            violations: list[Violation] | None
            if cached_violations is not None:
                violations = cached_violations
            else:
                rule_failures_before = len(self.rule_failures)
                violations = self._check_file(filepath)
                had_rule_failure = len(self.rule_failures) > rule_failures_before

                if violations is None:
                    # Unreadable, undecodable, or unparseable — _check_file
                    # already logged the specific cause.
                    self.unprocessable_files.append(filepath_str)
                elif not self.fix_mode and not had_rule_failure:
                    # Cache results (only if not in fix mode). Never cache a
                    # result collected while one of this file's checks
                    # crashed — it's known incomplete, and caching it would
                    # let a future cache-hit run silently keep treating the
                    # crash as "clean" until the tree hash changes.
                    self._cache_violations(filepath, violations)

            if violations is not None and violations:
                all_violations[filepath_str] = violations

        return all_violations

    def _generate_cache_key(self) -> str:
        """Cache key from the enabled checks, their own config, this
        package's own source, and the running interpreter's own version —
        replaces a hand-maintained CACHE_VERSION constant that a developer
        had to remember to bump whenever any check's behavior changed (a
        real bug, commit 0e3efba, already came from forgetting to). Any of
        the four changing invalidates every cached result for every check —
        deliberately coarse-grained in exchange for never missing a real
        change again.

        The interpreter version is included because every check's results
        come from `ast.parse()`'s output, and that output isn't guaranteed
        identical for the same source text across Python minor versions
        (grammar and AST-shape changes between releases) — so a `.cache`
        directory shared across an interpreter upgrade must not silently
        reuse results computed under the old one. Only major.minor is used:
        bugfix releases don't change the grammar, and including the full
        version (e.g. build metadata) would invalidate the cache on every
        patch release for no behavioral reason.
        """
        check_ids = sorted(check.check_id for check in self.checks)
        fingerprints = sorted(f"{check.check_id}={_fingerprint_check(check)}" for check in self.checks)
        tree_hash = CacheManager.compute_tree_hash(_PACKAGE_ROOT)
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        return "|".join([",".join(check_ids), ",".join(fingerprints), tree_hash, python_version])

    def _get_cached_violations(self, filepath: Path) -> list[Violation] | None:
        try:
            # self.cache's own cache_version already rejects a stale entry
            # (enabled checks, their config, or this package's own source
            # changed since it was written) before this ever sees it.
            cached = self.cache.get_cached_result(filepath, "ruff-extra-rules")
            if cached is None:
                return None

            violations = [
                Violation(
                    check_id=v_dict["check_id"],
                    error_code=v_dict["error_code"],
                    line=v_dict["line"],
                    col=v_dict["col"],
                    message=v_dict["message"],
                    fixable=v_dict["fixable"],
                    fix_data=v_dict.get("fix_data"),
                )
                for v_dict in cached.get("violations", [])
            ]
        except (KeyError, TypeError, ValueError) as error:
            logger.debug("Cache deserialization failed: %s", repr(error))
            return None
        else:
            return violations

    def _cache_violations(self, filepath: Path, violations: list[Violation]) -> None:
        try:
            serialized = [
                {
                    "check_id": v.check_id,
                    "error_code": v.error_code,
                    "line": v.line,
                    "col": v.col,
                    "message": v.message,
                    "fixable": v.fixable,
                    # Note: fix_data is NOT cached as it may contain AST nodes
                }
                for v in violations
            ]

            self.cache.set_cached_result(filepath, "ruff-extra-rules", {"violations": serialized})
        except (TypeError, ValueError) as error:
            logger.warning("Cache serialization failed: %s", repr(error))

    def _read_source(self, filepath: Path) -> tuple[str, str] | None:
        """Thin error-handling wrapper around read_source_with_encoding: logs
        and returns None on any failure instead of raising, since every
        caller here treats "file couldn't be processed" the same way.

        Debug-only logging: every caller already turns a None return into
        its own clean, user-facing diagnostic (_check_file's own caller
        reports it via unprocessable_files; _apply_fixes's own caller
        reports it via rule_failures) — an ERROR-level .exception() call
        here would just leak a redundant raw traceback onto the user's
        stderr by default (nothing in this codebase configures logging, so
        Python's own lastResort handler prints WARNING+ straight to
        stderr).
        """
        try:
            return read_source_with_encoding(filepath)
        except OSError:
            logger.debug("Failed to read %s", filepath, exc_info=True)
            return None
        except SyntaxError:
            logger.debug("Failed to detect encoding for %s", filepath, exc_info=True)
            return None
        except UnicodeDecodeError, LookupError:
            logger.debug("Failed to decode %s", filepath, exc_info=True)
            return None

    def _check_file(self, filepath: Path) -> list[Violation] | None:
        read_result = self._read_source(filepath)
        if read_result is None:
            return None
        source, _encoding = read_result

        try:
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            # Debug-only: the caller reports this via unprocessable_files —
            # see _read_source's own docstring for why ERROR-level
            # .exception() logging here would just be redundant noise.
            logger.debug("Failed to parse %s", filepath, exc_info=True)
            return None

        all_violations: list[Violation] = []
        for check in self.checks:
            try:
                violations = check.check(filepath, tree, source)
                all_violations.extend(violations)
            except Exception:  # noqa: BLE001 -- caught, isolated (ch. 5), and logged below; not swallowed
                # Debug-only: reported cleanly via rule_failures below — see
                # _read_source's own docstring for why ERROR-level
                # .exception() logging here would just be redundant noise.
                logger.debug("Check %s failed on %s", check.check_id, filepath, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))

        if self.fix_mode and all_violations:
            self._apply_fixes(filepath, all_violations)

        return all_violations

    def _apply_fixes(
        self,
        filepath: Path,
        violations: list[Violation],
    ) -> None:
        """`violations` holds all violations found in the file so far this
        run, and is mutated in place: each fixable check's own stale entries
        (collected once, before any fix ran) are replaced with a freshly
        recomputed list, each marked fixed/rejected/errored/left alone
        against the file's actual post-fix state. Matching a stale entry
        back to "is this the same violation, now fixed" by identity isn't
        reliable — an earlier check's own fix can shift line/col numbers,
        and two distinct violations can share an identical message (e.g. a
        same-named free function and method both suggesting the same
        rename) — so the stale entries for this check_id are discarded
        outright rather than matched.
        """
        fixable_check_ids = {v.check_id for v in violations if v.fixable}

        # Whether any check's fix() actually resolved at least one violation
        # this call — the only case where a later check's own recompute (or
        # a non-participating check's stale entries) can possibly be
        # pointing at shifted line numbers, so the final pass below is worth
        # its own extra read+parse+recheck.
        file_changed = False

        for check in self.checks:
            if check.check_id not in fixable_check_ids:
                continue
            try:
                # Re-read source in case a previous check's fix in this same
                # loop already modified the file
                read_result = self._read_source(filepath)
                if read_result is None:
                    # The file was readable moments ago (this run's own
                    # initial check pass succeeded on it) — a failure here
                    # means something changed concurrently, or an earlier
                    # check's own fix in this same loop left it in a bad
                    # state. Without a rule_failure + marking, this check's
                    # violations would silently keep their stale pre-fix
                    # snapshot and be reported as ordinary [FIXABLE], as if
                    # --fix had never even been attempted for them.
                    self.rule_failures.append((str(filepath), check.check_id))
                    for v in violations:
                        if v.check_id == check.check_id and v.fixable:
                            mark_fix_errored(v)
                    continue
                current_source, encoding = read_result
                current_tree = ast.parse(current_source, filename=str(filepath))

                # Recompute violations against the current file state rather
                # than reusing the stale ones collected before any fixes ran:
                # an earlier check's fix can shift line/col numbers (removing
                # or inserting lines), which would otherwise make this
                # check's fix() edit the wrong location.
                fresh_violations = [v for v in check.check(filepath, current_tree, current_source) if v.fixable]
                if not fresh_violations:
                    continue

                try:
                    check.fix(filepath, fresh_violations, current_source, current_tree, encoding)
                except FixValidationError:
                    # atomic_write_text() refused to write — the file is
                    # untouched, so every violation this check just tried to
                    # fix is still exactly as it was. This is a bug in the
                    # check's fix logic, not an expected outcome. Debug-only
                    # — mark_fix_rejected() below already reports this
                    # cleanly as [FIX REJECTED]; see _read_source's own
                    # docstring for why ERROR-level .exception() logging
                    # here would just be redundant noise.
                    logger.debug(
                        "Fix for %s produced invalid syntax on %s; the file was left untouched.",
                        check.check_id,
                        filepath,
                        exc_info=True,
                    )
                    for v in fresh_violations:
                        mark_fix_rejected(v)
                except Exception:  # noqa: BLE001 -- caught, isolated (ch. 5), and logged below; not swallowed
                    # fix() itself raised — a bug in the check's own fix
                    # logic, distinct from FixValidationError (which means
                    # fix() ran to completion but atomic_write_text()
                    # rejected its output). Caught here, specifically around
                    # the fix() call, rather than only by this method's
                    # outer except Exception below: that outer handler also
                    # covers benign races (e.g. the file disappearing before
                    # a re-read), which must not be reported as a fix bug.
                    #
                    # A check that writes more than once per fix() call
                    # (looping over violations individually, like
                    # validate_function_name) can have already committed some
                    # of fresh_violations before this exception interrupted a
                    # later one — re-check against the file's real state
                    # rather than assuming every violation in this batch is
                    # still broken, the same way the success path below
                    # already must (a bool return isn't precise enough
                    # either).
                    # Debug-only — rule_failures/mark_fix_errored() below
                    # already report this cleanly; see _read_source's own
                    # docstring for why ERROR-level .exception() logging
                    # here would just be redundant noise.
                    logger.debug(
                        "Fix for %s raised an unexpected exception on %s.",
                        check.check_id,
                        filepath,
                        exc_info=True,
                    )
                    # Always recorded, even if every fresh_violations entry
                    # turns out resolved below (e.g. fix() committed its
                    # edits, then raised afterwards during unrelated
                    # cleanup): an exception genuinely happened here, and
                    # that must never become invisible to the user just
                    # because nothing is left to mark [FIX ERRORED].
                    self.rule_failures.append((str(filepath), check.check_id))
                    still_present = self._mark_resolved_and_get_still_present(filepath, check, fresh_violations)
                    if len(still_present) < len(fresh_violations):
                        file_changed = True
                    for v in fresh_violations:
                        if (v.line, v.col, v.message) in still_present:
                            mark_fix_errored(v)
                        # else: already resolved (mark_fixed() already called
                        # by the re-check above) before fix() raised.
                else:
                    # A check's own bool return isn't precise enough to know
                    # which violations were actually resolved: a
                    # per-violation guard (e.g. validate_function_name's
                    # should_autofix) can skip some violations while fixing
                    # others in the same call. Re-check against the file's
                    # real post-fix state instead of trusting the return
                    # value.
                    still_present = self._mark_resolved_and_get_still_present(filepath, check, fresh_violations)
                    if len(still_present) < len(fresh_violations):
                        file_changed = True
                    # else: still present — either rejected (already marked
                    # via mark_fix_rejected() inside a multi-write check's
                    # own per-violation loop) or left alone by a
                    # per-violation guard; either way, not fixed.

                # fresh_violations replaces this check_id's stale entries
                # wholesale: its positions are accurate as of just before
                # this fix() call, strictly more current than the very
                # first, pre-any-fix snapshot in `violations`.
                violations[:] = [v for v in violations if v.check_id != check.check_id or not v.fixable]
                violations.extend(fresh_violations)
            except Exception:  # noqa: BLE001 -- caught, isolated (ch. 5), and logged below; not swallowed
                # Anything not already handled above: e.g. the re-parse or
                # the fresh_violations recompute itself raising. Isolated
                # per-check like every other failure here (ch. 5), but must
                # still be surfaced — without a rule_failure + marking, this
                # check's violations keep their stale pre-fix snapshot and
                # get reported as ordinary [FIXABLE], as if --fix had never
                # even been attempted for them. Debug-only — rule_failures/
                # mark_fix_errored() below already report this cleanly; see
                # _read_source's own docstring for why ERROR-level
                # .exception() logging here would just be redundant noise.
                logger.debug("Fix failed for %s on %s", check.check_id, filepath, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))
                for v in violations:
                    if v.check_id == check.check_id and v.fixable:
                        mark_fix_errored(v)

        if file_changed:
            self._refresh_stale_positions(filepath, violations)

    def _refresh_stale_positions(
        self,
        filepath: Path,
        violations: list[Violation],
    ) -> None:
        """Re-check `filepath`'s final on-disk state and refresh the
        position of every still-*open* violation (no fixed/rejected/
        errored/failed outcome yet this call) — covers both a check that
        never got as far as calling its own `fix()` this run (e.g. a check
        that's never fixable at all, like redundant-super-init) *and* a
        check that did run but left some of its own violations open (e.g.
        `validate-function-name`'s `should_autofix` guard skipping a method
        while renaming a different, unrelated function in the same `fix()`
        call — the per-check loop above only recomputes that check's own
        positions once, immediately before its own `fix()` call, not again
        afterward). Either way, if some *other* check's fix in the same run
        removed or inserted lines after that point, the still-open
        violation's position silently points at the wrong place — ch. 7:
        "MUST report line and column information accurately when
        available". Only called when `_apply_fixes` already confirmed the
        file's content actually changed this call.

        A violation already marked fixed this call is left completely
        untouched rather than recomputed: it's genuinely gone from the file,
        so a fresh `check()` call would never find it again (silently
        losing its `[FIXED]` confirmation). A check_id with any
        rejected/errored/failed entry is skipped *entirely* this pass,
        including its own still-open entries (if any): a fresh `check()`
        call would rediscover the still-present rejected/errored/failed
        violation too, and there's no reliable way to tell that rediscovery
        apart from a different, unrelated violation that merely happens to
        share the same message text (e.g. two identically-named functions
        in different scopes) without a stable per-violation identity this
        codebase doesn't have — silently dropping a real, unrelated
        violation would be worse than leaving its position stale (ch. 34:
        "MUST prefer a visible failure over a silent incorrect result").

        `violations` is the same list `_apply_fixes` mutates in place.
        """
        final_read = self._read_source(filepath)
        if final_read is None:
            return
        final_source, _final_encoding = final_read
        try:
            final_tree = ast.parse(final_source, filename=str(filepath))
        except SyntaxError:
            return

        for check in self.checks:
            check_entries = [v for v in violations if v.check_id == check.check_id]
            if not check_entries or any(
                is_fix_rejected(v) or is_fix_errored(v) or is_fix_failed(v) for v in check_entries
            ):
                continue

            stale = [v for v in check_entries if not is_fixed(v)]
            if not stale:
                continue

            try:
                fresh = check.check(filepath, final_tree, final_source)
            except Exception:  # noqa: BLE001 -- caught, isolated (ch. 5), and logged below; not swallowed
                # Debug-only: reported cleanly via rule_failures below — see
                # _read_source's own docstring for why ERROR-level
                # .exception() logging here would just be redundant noise.
                logger.debug("Check %s failed on %s", check.check_id, filepath, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))
                continue

            stale_ids = {id(v) for v in stale}
            violations[:] = [v for v in violations if id(v) not in stale_ids]
            violations.extend(fresh)

    def _mark_resolved_and_get_still_present(
        self,
        filepath: Path,
        check: ASTCheck,
        fresh_violations: list[Violation],
    ) -> set[ViolationKey]:
        """Re-check `filepath` against its actual current on-disk content
        and call `mark_fixed()` on every violation in `fresh_violations`
        that's no longer present there — regardless of whether `check.fix()`
        returned normally or raised partway through. A check that writes
        more than once per `fix()` call (looping over violations
        individually, like `validate_function_name`) can have already
        committed some violations before a later one failed or raised;
        matching by `ViolationKey` against the file's real state, rather
        than trusting a bool return or "fix() didn't raise", is what
        catches that.

        Returns the keys of `fresh_violations` still present, so a caller
        with more context (e.g. "fix() itself raised for this check") can
        mark those specifically, distinct from the ones already resolved by
        this call. If the file couldn't be re-read (e.g. deleted
        concurrently), conservatively returns every key unresolved —
        nothing is marked fixed on an unverifiable outcome.
        """
        post_read_result = self._read_source(filepath)
        if post_read_result is None:
            return {(v.line, v.col, v.message) for v in fresh_violations}

        post_source, _post_encoding = post_read_result
        post_tree = ast.parse(post_source, filename=str(filepath))
        still_present: set[ViolationKey] = {
            (v.line, v.col, v.message) for v in check.check(filepath, post_tree, post_source) if v.fixable
        }
        for v in fresh_violations:
            if (v.line, v.col, v.message) not in still_present:
                mark_fixed(v)
        return still_present


def load_checks(
    select: set[str] | None = None,
    ignore: set[str] | None = None,
    check_args: dict[str, Any] | None = None,
) -> list[ASTCheck]:
    """Mirrors `ruff check --select`/`--ignore`: `select` narrows the
    candidate set (None = all checks), and `ignore` always subtracts from
    whatever that candidate set is, whether or not `select` was given.
    """
    if check_args is None:
        check_args = {}

    checks: list[ASTCheck] = []

    for check_class in ALL_CHECKS:
        try:
            check = check_class()
        except Exception:
            logger.exception("Failed to load check %s", check_class.__name__)
            continue

        check_id = check.check_id

        if select is not None and check_id not in select:
            continue
        if ignore is not None and check_id in ignore:
            continue

        # Re-instantiate with check-specific arguments, if any were given.
        args = check_args.get(check_id, {})
        if args:
            try:
                check = check_class(**args)
            except Exception:
                logger.exception("Failed to load check %s", check_id)
                continue

        checks.append(check)

    return checks
