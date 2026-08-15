"""The linting application itself: running the enabled checks over a set of
files, caching their results, and applying fixes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pre_commit_hooks._cache import CacheManager
from pre_commit_hooks._filelock import locked, locking_is_available
from pre_commit_hooks._prefilter import batch_filter_files

from . import ALL_CHECKS
from ._base import (
    ASTCheck,
    CheckUnavailableError,
    ConcurrentModificationError,
    FixValidationError,
    Violation,
    is_fix_aborted,
    is_fix_errored,
    is_fix_failed,
    is_fix_rejected,
    is_fixed,
    mark_fix_aborted,
    mark_fix_errored,
    mark_fix_rejected,
    mark_fixed,
    read_source_with_encoding,
)
from ._per_file_ignores import PerFileIgnoreList

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("ast_checks")

# src/pre_commit_hooks/ — the tree CacheManager.compute_tree_hash() hashes to
# invalidate every cached result whenever any check's own code, or shared
# code it depends on, changes.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Matches a pre-fix Violation against a fresh check() re-run's own new
# Violation objects, which can never share object identity with it.
type ViolationKey = tuple[int, int, str]  # (line, col, message)

_FIX_LOCK_TIMEOUT_SECONDS = 30.0
_FIX_LOCK_POLL_INTERVAL_SECONDS = 0.05


def _fix_lock_path(filepath: Path) -> Path:
    file_hash = hashlib.sha1(str(filepath.resolve()).encode(), usedforsecurity=False).hexdigest()
    lock_dir = Path(tempfile.gettempdir()).resolve() / "ruff-extra-rules" / "fix-locks" / file_hash[:2]
    return lock_dir / f"{file_hash}.lock"


def _has_terminal_fix_state(violation: Violation) -> bool:
    """Whether `violation` already carries a rejected/errored/failed/aborted
    outcome from a check's own per-violation `fix()` handling this run --
    i.e. the outcome is already decided and must never be second-guessed by
    a later, broader recheck (e.g. relabeled `[FIXED]` just because it's no
    longer detected). Deliberately excludes `is_fixed()`: a fixed violation
    is a normal, non-terminal-in-this-sense outcome each call site here
    already handles on its own.
    """
    return (
        is_fix_rejected(violation) or is_fix_errored(violation) or is_fix_failed(violation) or is_fix_aborted(violation)
    )


def _replace_check_violations(
    all_violations: dict[str, list[Violation]], key: str, check_id: str, new_violations: list[Violation]
) -> None:
    """Replaces whatever `check_id` previously reported for `key` in `all_violations` with
    `new_violations`, preserving any other check's own violations for that same file (ADR-0041: a
    drained candidate is re-checked with only the one check that flagged it, never every check, so a
    stale result must be replaced rather than appended to -- otherwise an unchanged violation would be
    reported twice, and one that's now clean would still show its own, no-longer-true earlier report).
    """
    kept = [violation for violation in all_violations.get(key, []) if violation.check_id != check_id]
    combined = kept + new_violations
    if combined:
        all_violations[key] = combined
    else:
        all_violations.pop(key, None)


def _fingerprint_default(value: object) -> object:
    """`json.dumps(..., default=...)` handler for the value shapes a check's
    own instance state (see `_instance_state`) can contain but that `json`
    can't natively serialize: a `set`'s iteration order depends on
    PYTHONHASHSEED (randomized per process by default), so it's sorted
    first rather than dumped as-is — otherwise the same config would
    fingerprint differently across process runs, making the cache key (and
    so the cache itself) useless. Anything else falls back to repr() rather
    than raising, since instance state can pick up values this generic and
    unopinionated (e.g. a test's monkeypatched instance attribute) that
    were never meant to be "config" in the first place — the fingerprint
    just needs to not crash construction, not be meaningful for every
    possible value a check instance could ever hold.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return repr(value)


def _instance_state(check: ASTCheck) -> dict[str, object]:
    """Every attribute making up `check`'s own instance state — effectively
    its constructor arguments, so two instances of the same check with
    different configuration wouldn't share a cache entry. Checks with no
    `__init__` override (most of them) have no instance state at all, so
    this deliberately walks both `__slots__` (across the whole MRO, since a
    subclass's own `__slots__` doesn't include an ancestor's) and any
    `__dict__` a check might still have, rather than something every check
    must opt into.
    """
    state: dict[str, object] = {}
    for cls in type(check).__mro__:
        for slot in cls.__dict__.get("__slots__", ()):
            state[slot] = getattr(check, slot)
    instance_dict = getattr(check, "__dict__", None)
    if instance_dict:
        state.update(instance_dict)
    return state


def _fingerprint_check(check: ASTCheck) -> str:
    """Stable fingerprint of a check instance's own state (see
    `_instance_state`).
    """
    return json.dumps(_instance_state(check), default=_fingerprint_default, sort_keys=True)


@dataclass(frozen=True, slots=True)
class _FileChecks:
    """One file's share of a run: the checks to run against it, the checks
    `per-file-ignores` switched off for it that must still be handed its
    content (see `ASTCheck.tracks_direct_inputs`), and the cache identity of
    what running that set produces. See
    `docs/adr/0049-per-file-ignores.md`.
    """

    run: tuple[ASTCheck, ...]
    record_only: tuple[ASTCheck, ...]
    hook_name: str


class CheckOrchestrator:
    """Orchestrates running multiple AST checks on Python files.

    This class manages the workflow of:
    1. Pre-filtering files per-check, against each check's own pattern
    2. Caching check results
    3. Parsing files once and running each file's applicable checks
    4. Applying fixes when requested
    5. Reporting violations
    """

    __slots__ = (
        "_cacheable_check_ids",
        "_cacheable_fingerprints",
        "_check_ids",
        "_fix_changed_file",
        "_hook_names",
        "_per_file_ignores",
        "_unavailable_check_ids",
        "cache",
        "checks",
        "fix_mode",
        "rule_failures",
        "unavailable_checks",
        "unprocessable_files",
    )

    def __init__(
        self,
        checks: list[ASTCheck],
        *,
        fix_mode: bool = False,
        cache_dir: Path | None = None,
        per_file_ignores: PerFileIgnoreList | None = None,
    ) -> None:
        self.checks = checks
        self.fix_mode = fix_mode
        self._per_file_ignores = per_file_ignores or PerFileIgnoreList()
        self._cacheable_fingerprints = {
            check.check_id: f"{check.check_id}={_fingerprint_check(check)}" for check in checks if check.cacheable
        }
        self._check_ids = frozenset(check.check_id for check in checks)
        self._cacheable_check_ids = frozenset(self._cacheable_fingerprints)
        self._hook_names: dict[frozenset[str], str] = {}
        self.cache = CacheManager(
            cache_dir=cache_dir,
            hook_name=self._hook_name_for(self._cacheable_check_ids),
            cache_version=self._generate_cache_version(),
        )
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
        # (check_id, message) pairs, one per check_id, where a check raised
        # CheckUnavailableError -- see that exception's own docstring for
        # why this is recorded once here instead of aborting the whole run.
        # Reset at the start of each process_files() call, same as the two
        # above.
        self.unavailable_checks: list[tuple[str, str]] = []
        # check_ids already recorded in unavailable_checks this run -- once
        # a check_id lands here, _check_file skips calling that check
        # entirely, rather than paying its own failure cost again (and
        # recording a duplicate entry) on every remaining file.
        self._unavailable_check_ids: set[str] = set()
        # Set by _apply_fixes() for the file it was just called on -- read
        # back by _process_single_file() right after its own full-checks
        # _check_file() call returns, to decide whether that file's result
        # is safe to cache (see _apply_fixes' own docstring for why a
        # changed file's cacheable results aren't).
        self._fix_changed_file = False

    def process_files(self, filepaths: list[str]) -> dict[str, list[Violation]]:
        """A file that couldn't be read or parsed has no entry in the
        returned dict (indistinguishable from "processed, zero
        violations") — check `self.unprocessable_files` for those. A file
        where one check crashed while others ran fine can still have an
        entry here (the other checks' violations), but its results are
        incomplete — check `self.rule_failures` for those. A check that
        raised `CheckUnavailableError` contributes no violations for the
        rest of this run, for any file — check `self.unavailable_checks`
        for those; every other check's results are unaffected.
        """
        self.unprocessable_files = []
        self.rule_failures = []
        self.unavailable_checks = []
        self._unavailable_check_ids = set()

        if not filepaths:
            return {}

        checks_by_file = self._checks_by_file(filepaths)

        if not checks_by_file:
            return {}

        # self.cache's own cache_version and hook_name (set at construction
        # from _generate_cache_version()/_generate_hook_name()) already gate
        # staleness and config identity — no separate per-file cache_key
        # needed here.
        all_violations: dict[str, list[Violation]] = {}
        # Lets a reconciled file that is also a direct input retain the caller's original key.
        resolved_to_key: dict[Path, str] = {}
        for filepath_str in checks_by_file:
            try:
                resolved_to_key[Path(filepath_str).resolve()] = filepath_str
            except OSError:
                logger.debug("Could not resolve input path %s", filepath_str, exc_info=True)

        for filepath_str, file_checks in checks_by_file.items():
            filepath = Path(filepath_str)
            violations = self._process_single_file(filepath, file_checks)

            if violations is None:
                # Unreadable, undecodable, or unparseable — _check_file
                # already logged the specific cause.
                self.unprocessable_files.append(filepath_str)
            elif violations:
                all_violations[filepath_str] = violations

        self._reconcile_direct_inputs(list(resolved_to_key), all_violations, resolved_to_key)

        return all_violations

    def _record_unavailable_check(self, check: ASTCheck, error: CheckUnavailableError) -> None:
        self._unavailable_check_ids.add(check.check_id)
        self.unavailable_checks.append((check.check_id, str(error)))

    def _reconcile_direct_inputs(
        self,
        direct_inputs: list[Path],
        all_violations: dict[str, list[Violation]],
        resolved_to_key: dict[Path, str],
    ) -> None:
        for check in self.checks:
            if check.check_id in self._unavailable_check_ids:
                continue
            try:
                extra_files = check.reconcile_direct_inputs(direct_inputs)
            except CheckUnavailableError as error:
                logger.debug("Check %s is unavailable: %s", check.check_id, error, exc_info=True)
                self._record_unavailable_check(check, error)
                continue
            except Exception:
                logger.debug("Check %s failed to reconcile cross-file candidates", check.check_id, exc_info=True)
                if direct_inputs:
                    self.rule_failures.append((str(direct_inputs[0]), check.check_id))
                continue
            for extra_file in sorted(extra_files):
                try:
                    extra_resolved = extra_file.resolve()
                except OSError:
                    logger.debug("Check %s returned an unresolvable path %s", check.check_id, extra_file, exc_info=True)
                    continue
                if check.check_id in self._per_file_ignores.ignored_check_ids(extra_resolved):
                    continue
                violations = self._check_derived_file(extra_resolved, [check])
                extra_file_str = resolved_to_key.get(extra_resolved, str(extra_resolved))
                if violations is None:
                    self.unprocessable_files.append(extra_file_str)
                    _replace_check_violations(all_violations, extra_file_str, check.check_id, [])
                else:
                    _replace_check_violations(all_violations, extra_file_str, check.check_id, violations)

    def _process_single_file(self, filepath: Path, file_checks: _FileChecks) -> list[Violation] | None:
        """Runs `file_checks.run` against a single file, honoring each
        check's own `cacheable` flag: a cacheable check's violations may
        come from (and be written to) the shared per-file cache; a
        non-cacheable check (see `ASTCheck.cacheable`) is always re-run
        fresh, on every call, regardless of what the cache holds for this
        file.

        `file_checks.record_only` rides along with whichever pass below
        already reads the file, since a check switched off for it by
        `per-file-ignores` still has to be handed its content (ADR-0049).
        Only a clean cache hit with nothing to re-run pays for its own read,
        being the one path that would otherwise never open the file.

        Returns `None` if the file couldn't be read/parsed (mirrors
        `_check_file`'s own contract).
        """
        record_only = file_checks.record_only
        checks = file_checks.run
        if not checks:
            # Nothing left to run, but reading and parsing the file is what
            # turns an unreadable or unparseable input into a report rather
            # than a silent skip (ch. 13).
            return self._check_file(filepath, (), record_only=record_only)

        hook_name = file_checks.hook_name
        cacheable_checks = [check for check in checks if check.cacheable]
        always_rerun_checks = [check for check in checks if not check.cacheable]

        cached_violations: list[Violation] | None = None
        if cacheable_checks:
            cached_violations = self._get_cached_violations(filepath, hook_name)

        # A cache hit is only trustworthy in fix mode when it reports zero
        # violations: fix_data is never cached (see _cache_violations), so
        # a hit that shows a violation can't be used to actually fix it --
        # that case falls through to the full recompute-and-fix path below,
        # same as a genuine cache miss. See ADR-0044.
        if cached_violations is not None and (not self.fix_mode or not cached_violations):
            if not always_rerun_checks:
                # The one path that returns without opening the file, so a
                # check owed its content gets a pass of its own here.
                if record_only and not self._record_direct_inputs(filepath, record_only):
                    return None
                return cached_violations
            # The cacheable group's cache entry is still valid, but a
            # non-cacheable check must run fresh against this file's
            # current, real content every single call — its own result is
            # never read from or written to the cache. In fix mode, this
            # already fixes any violation it finds: _check_file_locked
            # calls _apply_fixes with exactly the violations this call
            # produced, so it never touches the (already-clean) cacheable
            # group.
            self._fix_changed_file = False
            fresh = self._check_file(filepath, always_rerun_checks, record_only=record_only)
            if fresh is None:
                return None
            if not self._fix_changed_file:
                return cached_violations + fresh
            # The always-rerun group's own fix changed the file, so the
            # cached "clean" cacheable-group result is no longer known to
            # be accurate for the file's new content. Re-verify (and fix,
            # if needed) just the cacheable group fresh against that new
            # content, merging in `fresh` as-is rather than re-running the
            # already-fixed always-rerun group a second time: a second
            # check() there would find it clean and silently lose its own
            # [FIXED] outcome, since the violation is already gone.
            # cacheable_checks is guaranteed non-empty here: cached_violations
            # is only ever not None when it was, since that's the only branch
            # that sets it.
            return self._check_and_cache(filepath, cacheable_checks, hook_name, extra_violations=fresh)

        return self._check_and_cache(filepath, checks, hook_name, record_only=record_only)

    def _check_and_cache(
        self,
        filepath: Path,
        checks: Sequence[ASTCheck],
        hook_name: str,
        *,
        extra_violations: list[Violation] | None = None,
        record_only: Sequence[ASTCheck] = (),
    ) -> list[Violation] | None:
        """Runs `checks` fresh against `filepath` (fixing, in fix mode),
        then caches whatever cacheable subset of `checks` is complete and
        accurate this run. `extra_violations`, if given, is merged into the
        return value without being cached itself — e.g. an always-rerun
        group's own already-resolved result from an earlier pass this same
        `_process_single_file()` call, which must still be reported even
        though it's never eligible for caching either way (ADR-0034).

        Returns `None` if the file couldn't be read/parsed (mirrors
        `_check_file`'s own contract).
        """
        rule_failures_before = len(self.rule_failures)
        self._fix_changed_file = False
        violations = self._check_file(filepath, checks, record_only=record_only)
        new_failure_ids = {check_id for _fp, check_id in self.rule_failures[rule_failures_before:]}

        if violations is None:
            return None

        cacheable_ids = {check.check_id for check in checks if check.cacheable}
        # incomplete_ids covers a cacheable check that crashed this file
        # (new_failure_ids), one that's globally unavailable this run
        # (_unavailable_check_ids, e.g. a missing prerequisite), and — in
        # fix mode — one that hit a rejected/errored/aborted fix outcome
        # this run (terminal_negative_ids). All three mean this check's own
        # results for this file are missing or unverified: caching the rest
        # of the group as if it were complete would let a future cache hit
        # keep serving that gap, or serve stale positions _refresh_stale_
        # positions() deliberately left unrefreshed for the same check_id
        # (see its own docstring), until the file or cache version changes.
        terminal_negative_ids = {v.check_id for v in violations if _has_terminal_fix_state(v)}
        incomplete_ids = new_failure_ids | self._unavailable_check_ids | terminal_negative_ids
        # self._fix_changed_file (set by _apply_fixes, see its own
        # docstring): a check with zero violations here is never
        # re-verified against the file's final content once some other
        # check's fix actually changed it, so nothing in this group can be
        # cached as complete and accurate this run — it converges to a
        # cache hit on its own next (unchanged) run instead.
        if cacheable_ids and not (incomplete_ids & cacheable_ids) and not self._fix_changed_file:
            # A violation marked fixed no longer exists in the file's
            # current content, so it must never be cached as still present.
            cacheable_violations = [v for v in violations if v.check_id in cacheable_ids and not is_fixed(v)]
            self._cache_violations(filepath, hook_name, cacheable_violations)

        return violations + extra_violations if extra_violations else violations

    def _checks_by_file(self, filepaths: list[str]) -> dict[str, _FileChecks]:
        """Applies each check's own prefilter pattern independently, rather
        than combining every enabled check's pattern into one OR'd filter --
        the combined filter dropped a file for every check whenever it
        matched none of them, even a check whose own get_prefilter_pattern()
        returns None specifically to see every file. Preserves `self.checks`
        order per file, since `_apply_fixes` depends on it.

        A check's prefilter is never asked about a file `per-file-ignores`
        switched it off for -- the answer could not change what runs, and
        the scan is the expensive half of deciding it. A check tracking
        direct inputs is the exception, since it still has to be handed
        those files (ADR-0049).

        The cache identity is decided from the `per-file-ignores` outcome
        alone, never from the prefilter: a prefiltered-out check genuinely
        has nothing to report for that file, so its absence is already part
        of what a cache entry means (ADR-0049).

        A file matched by no enabled check's prefilter and not named by
        `per-file-ignores` either gets no entry at all, so it is never read
        or parsed and a syntax error in it is never reported -- an accepted
        scope boundary, not an oversight (ADR-0052).
        """
        # Intersected with this run's own checks: one table serves both
        # published hooks (ADR-0045), so an entry naming only the other one's
        # check has switched nothing off here and must not change what this
        # run reads or reports.
        ignored_by_file = {
            filepath_str: self._per_file_ignores.ignored_check_ids(filepath_str) & self._check_ids
            for filepath_str in filepaths
        }

        matches_by_check_id: dict[str, set[str]] = {}
        for check in self.checks:
            pattern = check.get_prefilter_pattern()
            if pattern:
                candidates = [
                    filepath_str
                    for filepath_str in filepaths
                    if check.tracks_direct_inputs or check.check_id not in ignored_by_file[filepath_str]
                ]
                matches_by_check_id[check.check_id] = set(batch_filter_files(candidates, pattern))

        checks_by_file: dict[str, _FileChecks] = {}
        for filepath_str in filepaths:
            ignored = ignored_by_file[filepath_str]
            applicable = [
                check
                for check in self.checks
                if check.check_id not in matches_by_check_id or filepath_str in matches_by_check_id[check.check_id]
            ]
            run = tuple(check for check in applicable if check.check_id not in ignored)
            record_only = tuple(
                check for check in applicable if check.check_id in ignored and check.tracks_direct_inputs
            )
            # A file `per-file-ignores` emptied out is still an input the
            # user named, and dropping it here is what would make an
            # unreadable one vanish without a word (ch. 13). A file with
            # neither -- no enabled check's prefilter matched it, nothing
            # ignored it -- is dropped on purpose (ADR-0052).
            if applicable or ignored:
                checks_by_file[filepath_str] = _FileChecks(
                    run=run,
                    record_only=record_only,
                    hook_name=self._hook_name_for(self._cacheable_check_ids - ignored),
                )

        return checks_by_file

    def _hook_name_for(self, check_ids: frozenset[str]) -> str:
        """Identity of *what produced* a cached result: a short hash of
        `check_ids` (a subset of the enabled cacheable checks, since
        `per-file-ignores` can switch some of them off for one file) and
        their own config. See ADR-0044 and ADR-0005 for why this is split
        from `_generate_cache_version()`, and ADR-0049 for why it is decided
        per file.
        """
        hook_name = self._hook_names.get(check_ids)
        if hook_name is None:
            fingerprints = sorted(self._cacheable_fingerprints[check_id] for check_id in check_ids)
            digest = hashlib.sha1(",".join(fingerprints).encode(), usedforsecurity=False).hexdigest()[:16]
            hook_name = self._hook_names[check_ids] = f"ruff-extra-rules:{digest}"
        return hook_name

    def _generate_cache_version(self) -> str:
        """Whether a cached result can be trusted *at all*: this package's
        own source-tree hash, and the running interpreter's major.minor
        (`ast.parse()`'s output isn't guaranteed identical across Python
        minor versions). See ADR-0044 and ADR-0005.
        """
        tree_hash = CacheManager.compute_tree_hash(_PACKAGE_ROOT)
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        return f"{tree_hash}|{python_version}"

    def _get_cached_violations(self, filepath: Path, hook_name: str) -> list[Violation] | None:
        try:
            # self.cache's own cache_version already rejects a stale entry,
            # and `hook_name` a mismatched-config one, before this ever sees
            # it.
            cached = self.cache.get_cached_result(filepath, hook_name)
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

    def _cache_violations(self, filepath: Path, hook_name: str, violations: list[Violation]) -> None:
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

            self.cache.set_cached_result(filepath, hook_name, {"violations": serialized})
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

    def _check_file(
        self, filepath: Path, checks: Sequence[ASTCheck], *, record_only: Sequence[ASTCheck] = ()
    ) -> list[Violation] | None:
        return self._check_file_with_lifecycle(filepath, checks, direct=True, record_only=record_only)

    def _check_derived_file(self, filepath: Path, checks: Sequence[ASTCheck]) -> list[Violation] | None:
        return self._check_file_with_lifecycle(filepath, checks, direct=False)

    def _check_file_with_lifecycle(
        self, filepath: Path, checks: Sequence[ASTCheck], *, direct: bool, record_only: Sequence[ASTCheck] = ()
    ) -> list[Violation] | None:
        if not self.fix_mode or not locking_is_available():
            return self._check_file_locked(filepath, checks, direct=direct, record_only=record_only)

        try:
            lock_path = _fix_lock_path(filepath)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with locked(
                lock_path,
                timeout_seconds=_FIX_LOCK_TIMEOUT_SECONDS,
                poll_interval_seconds=_FIX_LOCK_POLL_INTERVAL_SECONDS,
            ):
                return self._check_file_locked(filepath, checks, direct=direct, record_only=record_only)
        except TimeoutError:
            logger.debug(
                "Could not acquire the fix lock for %s within %ss -- another process may be fixing it",
                filepath,
                _FIX_LOCK_TIMEOUT_SECONDS,
                exc_info=True,
            )
            return None
        except OSError:
            logger.debug("Could not acquire the fix lock for %s", filepath, exc_info=True)
            return None

    def _parsed_source(self, filepath: Path) -> tuple[str, ast.Module] | None:
        """`None` if the file couldn't be read, decoded, or parsed — every
        caller here turns that into the same "unprocessable file" outcome.

        Debug-only logging: see `_read_source`'s own docstring for why an
        ERROR-level `.exception()` call here would just be redundant noise.
        """
        read_result = self._read_source(filepath)
        if read_result is None:
            return None
        source, _encoding = read_result
        try:
            return source, ast.parse(source, filename=filepath)
        except SyntaxError:
            logger.debug("Failed to parse %s", filepath, exc_info=True)
            return None

    def _check_file_locked(
        self, filepath: Path, checks: Sequence[ASTCheck], *, direct: bool, record_only: Sequence[ASTCheck] = ()
    ) -> list[Violation] | None:
        parsed = self._parsed_source(filepath)
        if parsed is None:
            return None
        source, tree = parsed

        all_violations: list[Violation] = []
        for check in checks:
            if check.check_id in self._unavailable_check_ids:
                # Already recorded in unavailable_checks for an earlier
                # file this run -- a missing/misbehaving prerequisite
                # doesn't get better on the next file, so this check is
                # never worth retrying (or re-recording) again this run.
                continue
            try:
                violations = check.check(filepath, tree, source)
            except CheckUnavailableError as error:
                # Recorded once here rather than per file: see
                # CheckUnavailableError's own docstring for why this must
                # not abort every other check's results for the rest of
                # this run.
                logger.debug("Check %s is unavailable: %s", check.check_id, error, exc_info=True)
                self._record_unavailable_check(check, error)
            except Exception:
                # Debug-only: reported cleanly via rule_failures below — see
                # _read_source's own docstring for why ERROR-level
                # .exception() logging here would just be redundant noise.
                logger.debug("Check %s failed on %s", check.check_id, filepath, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))
            else:
                all_violations.extend(violations)
                if direct:
                    self._record_direct_input(check, filepath, source)

        if direct:
            for check in record_only:
                self._record_direct_input(check, filepath, source)

        if self.fix_mode and all_violations:
            self._apply_fixes(filepath, all_violations)

        return all_violations

    def _record_direct_inputs(self, filepath: Path, checks: Sequence[ASTCheck]) -> bool:
        """Reads and parses `filepath` for its own sake, for the one caller
        with nothing else to read it. `False` if it couldn't be, which is that
        caller's cue to report the file unprocessable.
        """
        parsed = self._parsed_source(filepath)
        if parsed is None:
            return False
        source, _tree = parsed
        for check in checks:
            self._record_direct_input(check, filepath, source)
        return True

    def _record_direct_input(self, check: ASTCheck, filepath: Path, source: str) -> None:
        if check.check_id in self._unavailable_check_ids:
            return
        try:
            check.record_direct_input(filepath, source)
        except CheckUnavailableError as error:
            logger.debug("Check %s is unavailable: %s", check.check_id, error, exc_info=True)
            self._record_unavailable_check(check, error)
        except Exception:
            logger.debug("Check %s failed to record direct input %s", check.check_id, filepath, exc_info=True)
            self.rule_failures.append((str(filepath), check.check_id))

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

        Sets `self._fix_changed_file` for `_process_single_file` to read
        back: a check with zero violations this run is never re-verified
        here or by `_refresh_stale_positions()` below (neither one has any
        reason to look at a check_id with no entries in `violations`), so
        if some *other* check's fix changed the file, that zero-violation
        check's result is no longer known to be accurate against the file's
        final content — a fresh check() next run might disagree. See
        ADR-0044.
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
                current_tree = ast.parse(current_source, filename=filepath)

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
                except ConcurrentModificationError:
                    # atomic_write_text() refused to write — the file is
                    # untouched, but this time it's not a bug in the check's
                    # fix logic: something else (an editor, a concurrent
                    # process outside this run's own per-file fix lock)
                    # modified the file after current_source was read above.
                    # Debug-only — mark_fix_aborted() below already reports
                    # this cleanly as [FIX ABORTED]; see _read_source's own
                    # docstring for why ERROR-level .exception() logging here
                    # would just be redundant noise.
                    logger.debug(
                        "File %s changed on disk while fixing %s; the fix was discarded.",
                        filepath,
                        check.check_id,
                        exc_info=True,
                    )
                    for v in fresh_violations:
                        mark_fix_aborted(v)
                    # The external edit itself can shift line numbers too,
                    # same as a successful fix. See ADR-0042.
                    file_changed = True
                except Exception:
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
            except Exception:
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
        self._fix_changed_file = file_changed

    def _refresh_stale_positions(
        self,
        filepath: Path,
        violations: list[Violation],
    ) -> None:
        """Re-check `filepath`'s final on-disk state and refresh the
        position of every still-*open* violation (no fixed/rejected/
        errored/failed/aborted outcome yet this call) — covers both a check that
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
        rejected/errored/failed/aborted entry is skipped *entirely* this
        pass, including its own still-open entries (if any): a fresh
        `check()` call would rediscover the still-present rejected/errored/
        failed/aborted violation too, and there's no reliable way to tell
        that rediscovery
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
            final_tree = ast.parse(final_source, filename=filepath)
        except SyntaxError:
            return

        for check in self.checks:
            if check.check_id in self._unavailable_check_ids:
                # Already recorded in unavailable_checks -- see _check_file's
                # own matching guard for why this check is never worth
                # retrying again this run.
                continue
            check_entries = [v for v in violations if v.check_id == check.check_id]
            if not check_entries or any(_has_terminal_fix_state(v) for v in check_entries):
                continue

            stale = [v for v in check_entries if not is_fixed(v)]
            if not stale:
                continue

            try:
                fresh = check.check(filepath, final_tree, final_source)
            except Exception:
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
        this call. If the file couldn't be re-read or no longer parses,
        conservatively returns every key unresolved rather than raising —
        nothing is marked fixed on an unverifiable outcome. Never marks
        fixed a violation already carrying a rejected/errored/failed/aborted
        outcome from the check's own per-violation loop. See
        `docs/adr/0042-abort-fixes-on-concurrent-source-modification.md`.
        """
        post_read_result = self._read_source(filepath)
        if post_read_result is None:
            return {(v.line, v.col, v.message) for v in fresh_violations}

        post_source, _post_encoding = post_read_result
        try:
            post_tree = ast.parse(post_source, filename=filepath)
        except SyntaxError:
            return {(v.line, v.col, v.message) for v in fresh_violations}
        still_present: set[ViolationKey] = {
            (v.line, v.col, v.message) for v in check.check(filepath, post_tree, post_source) if v.fixable
        }
        for v in fresh_violations:
            if (v.line, v.col, v.message) not in still_present and not _has_terminal_fix_state(v):
                mark_fixed(v)
        return still_present


def load_checks(
    select: set[str] | None = None,
    ignore: set[str] | None = None,
    check_args: dict[str, Any] | None = None,
    check_classes: Sequence[type[ASTCheck]] | None = None,
) -> list[ASTCheck]:
    """Mirrors `ruff check --select`/`--ignore`: `select` narrows the
    candidate set (None = all checks), and `ignore` always subtracts from
    whatever that candidate set is, whether or not `select` was given.
    """
    if check_args is None:
        check_args = {}

    checks: list[ASTCheck] = []

    for check_class in ALL_CHECKS if check_classes is None else check_classes:
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
