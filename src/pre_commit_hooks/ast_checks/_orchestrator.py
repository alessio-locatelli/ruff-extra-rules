from __future__ import annotations

import ast
import hashlib
import json
import logging
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pre_commit_hooks._cache import CacheManager
from pre_commit_hooks._filelock import locked, locking_is_available
from pre_commit_hooks._prefilter import batch_filter_files

from . import ALL_CHECKS
from ._base import (
    ASTCheck,
    CheckResult,
    CheckUnavailableError,
    ConcurrentModificationError,
    FixOutcome,
    FixResult,
    FixValidationError,
    SuppressionUsage,
    Violation,
    read_source_with_encoding,
)
from ._per_file_ignores import PerFileIgnoreList
from .unused_pytriage import UnusedPytriageCheck

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger("ast_checks")


_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


type ViolationKey = tuple[int, int, str]


def _as_check_result(result: list[Violation]) -> CheckResult:
    return result if isinstance(result, CheckResult) else CheckResult(result)


def _merge_check_results(*results: CheckResult) -> CheckResult:
    merged = CheckResult()
    for result in results:
        merged.extend(result)
        merged.suppression_usages += result.suppression_usages
    return merged


def _check_with_suppression_tracking(check: ASTCheck, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
    return check.check_with_suppression_tracking(filepath, tree, source)


_FIX_LOCK_TIMEOUT_SECONDS = 30.0
_FIX_LOCK_POLL_INTERVAL_SECONDS = 0.05


def _fix_lock_path(filepath: Path) -> Path:
    file_hash = hashlib.sha1(str(filepath.resolve()).encode(), usedforsecurity=False).hexdigest()
    lock_dir = Path(tempfile.gettempdir()).resolve() / "ruff-extra-rules" / "fix-locks" / file_hash[:2]
    return lock_dir / f"{file_hash}.lock"


def _has_terminal_fix_state(violation: Violation) -> bool:
    return violation.fix_outcome in {
        FixOutcome.REJECTED,
        FixOutcome.ERRORED,
        FixOutcome.FAILED,
        FixOutcome.ABORTED,
    }


def _replace_check_violations(
    all_violations: dict[str, list[Violation]], key: str, check_id: str, new_violations: list[Violation]
) -> None:

    kept = [violation for violation in all_violations.get(key, []) if violation.check_id != check_id]
    combined = kept + new_violations
    if combined:
        all_violations[key] = combined
    else:
        all_violations.pop(key, None)


def _group_by_check_id(violations: Iterable[Violation]) -> dict[str, list[Violation]]:
    grouped: dict[str, list[Violation]] = {}
    for violation in violations:
        grouped.setdefault(violation.check_id, []).append(violation)
    return grouped


def _unmatched_by_message(dropped: list[Violation], replacements: list[Violation]) -> list[Violation]:
    unclaimed = Counter(violation.message for violation in replacements)
    unmatched = []
    for violation in dropped:
        if unclaimed[violation.message]:
            unclaimed[violation.message] -= 1
        else:
            unmatched.append(violation)
    return unmatched


def _record_indirect_resolutions(violations: list[Violation], initial_violations: dict[str, list[Violation]]) -> None:

    final_by_check = _group_by_check_id(violations)
    surviving = {id(violation) for violation in violations}
    for check_id, initial in initial_violations.items():
        initial_ids = {id(violation) for violation in initial}
        dropped = [violation for violation in initial if id(violation) not in surviving]
        replacements = [violation for violation in final_by_check.get(check_id, ()) if id(violation) not in initial_ids]
        missing = len(dropped) - len(replacements)
        if missing <= 0:
            continue
        for violation in _unmatched_by_message(dropped, replacements)[:missing]:
            violation.fix_outcome = FixOutcome.RESOLVED_INDIRECTLY
            violations.append(violation)


def _set_fix_outcomes(violations: list[Violation], fix_result: FixResult) -> None:
    if len(fix_result.outcomes) != len(violations):
        raise ValueError("fix result did not include one outcome per violation")
    for violation, outcome in zip(violations, fix_result.outcomes, strict=True):
        violation.fix_outcome = outcome


def _fingerprint_default(value: object) -> object:

    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return repr(value)


def _instance_state(check: ASTCheck) -> dict[str, object]:

    state: dict[str, object] = {}
    for cls in type(check).__mro__:
        for slot in cls.__dict__.get("__slots__", ()):
            state[slot] = getattr(check, slot)
    instance_dict = getattr(check, "__dict__", None)
    if instance_dict:
        state.update(instance_dict)
    return state


def _fingerprint_check(check: ASTCheck) -> str:

    return json.dumps(_instance_state(check), default=_fingerprint_default, sort_keys=True)


@dataclass(frozen=True, slots=True)
class _FileChecks:
    run: tuple[ASTCheck, ...]
    record_only: tuple[ASTCheck, ...]
    hook_name: str


class CheckOrchestrator:
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

        self.unprocessable_files: list[str] = []

        self.rule_failures: list[tuple[str, str]] = []

        self.unavailable_checks: list[tuple[str, str]] = []

        self._unavailable_check_ids: set[str] = set()

        self._fix_changed_file = False

    def process_files(self, filepaths: list[str]) -> dict[str, list[Violation]]:

        self.unprocessable_files = []
        self.rule_failures = []
        self.unavailable_checks = []
        self._unavailable_check_ids = set()

        if not filepaths:
            return {}

        checks_by_file = self._checks_by_file(filepaths)

        if not checks_by_file:
            return {}

        all_violations: dict[str, list[Violation]] = {}

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

        record_only = file_checks.record_only
        checks = file_checks.run
        if not checks:
            return self._check_file(filepath, (), record_only=record_only)

        hook_name = file_checks.hook_name
        cacheable_checks = [check for check in checks if check.cacheable]
        always_rerun_checks = [check for check in checks if not check.cacheable]

        cached_violations: CheckResult | None = None
        if cacheable_checks:
            cached_violations = self._get_cached_violations(filepath, hook_name)

        if cached_violations is not None and (not self.fix_mode or not cached_violations):
            if not always_rerun_checks:
                if record_only and not self._record_direct_inputs(filepath, record_only):
                    return None
                return cached_violations

            self._fix_changed_file = False
            rule_failures_before = len(self.rule_failures)
            fresh = self._check_file(
                filepath,
                [*always_rerun_checks],
                record_only=record_only,
                prior_suppression_usages=cached_violations.suppression_usages,
                prior_active_error_codes=frozenset(
                    check.error_code for check in cacheable_checks if not isinstance(check, UnusedPytriageCheck)
                ),
            )
            if fresh is None:
                return None
            if not self._fix_changed_file:
                return _merge_check_results(cached_violations, fresh)

            rerun_checks = [*cacheable_checks, *(check for check in checks if isinstance(check, UnusedPytriageCheck))]
            audit_check_ids = {check.check_id for check in rerun_checks if isinstance(check, UnusedPytriageCheck)}
            fresh_regular_result = CheckResult(
                (violation for violation in fresh if violation.check_id not in audit_check_ids),
                fresh.suppression_usages,
            )
            fresh_failure_ids = {
                check_id
                for _filepath, check_id in self.rule_failures[rule_failures_before:]
                if Path(_filepath) == filepath
            }
            return self._check_and_cache(
                filepath,
                rerun_checks,
                hook_name,
                extra_result=fresh_regular_result,
                prior_suppression_usages=fresh.suppression_usages,
                prior_active_error_codes=frozenset(
                    check.error_code
                    for check in [*cacheable_checks, *always_rerun_checks]
                    if not isinstance(check, UnusedPytriageCheck)
                    and check.check_id not in self._unavailable_check_ids
                    and check.check_id not in fresh_failure_ids
                ),
            )

        return self._check_and_cache(filepath, checks, hook_name, record_only=record_only)

    def _check_and_cache(
        self,
        filepath: Path,
        checks: Sequence[ASTCheck],
        hook_name: str,
        *,
        extra_result: CheckResult | None = None,
        record_only: Sequence[ASTCheck] = (),
        prior_suppression_usages: tuple[SuppressionUsage, ...] = (),
        prior_active_error_codes: frozenset[str] = frozenset(),
    ) -> list[Violation] | None:

        rule_failures_before = len(self.rule_failures)
        self._fix_changed_file = False
        if prior_suppression_usages or prior_active_error_codes:
            violations = self._check_file(
                filepath,
                checks,
                record_only=record_only,
                prior_suppression_usages=prior_suppression_usages,
                prior_active_error_codes=prior_active_error_codes,
            )
        else:
            violations = self._check_file(filepath, checks, record_only=record_only)
        new_failure_ids = {check_id for _fp, check_id in self.rule_failures[rule_failures_before:]}

        if violations is None:
            return None

        cacheable_ids = {check.check_id for check in checks if check.cacheable}

        terminal_negative_ids = {v.check_id for v in violations if _has_terminal_fix_state(v)}
        incomplete_ids = new_failure_ids | self._unavailable_check_ids | terminal_negative_ids

        if cacheable_ids and not (incomplete_ids & cacheable_ids) and not self._fix_changed_file:
            cacheable_violations = [
                v for v in violations if v.check_id in cacheable_ids and v.fix_outcome is not FixOutcome.APPLIED
            ]
            cacheable_usages = tuple(
                usage for usage in violations.suppression_usages if usage.check_id in cacheable_ids
            )
            self._cache_violations(filepath, hook_name, CheckResult(cacheable_violations, cacheable_usages))

        return _merge_check_results(violations, extra_result) if extra_result else violations

    def _checks_by_file(self, filepaths: list[str]) -> dict[str, _FileChecks]:

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

            if applicable or ignored:
                checks_by_file[filepath_str] = _FileChecks(
                    run=run,
                    record_only=record_only,
                    hook_name=self._hook_name_for(self._cacheable_check_ids - ignored),
                )

        return checks_by_file

    def _hook_name_for(self, check_ids: frozenset[str]) -> str:

        hook_name = self._hook_names.get(check_ids)
        if hook_name is None:
            fingerprints = sorted(self._cacheable_fingerprints[check_id] for check_id in check_ids)
            digest = hashlib.sha1(",".join(fingerprints).encode(), usedforsecurity=False).hexdigest()[:16]
            hook_name = self._hook_names[check_ids] = f"ruff-extra-rules:{digest}"
        return hook_name

    def _generate_cache_version(self) -> str:

        tree_hash = CacheManager.compute_tree_hash(_PACKAGE_ROOT)
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        return f"{tree_hash}|{python_version}"

    def _get_cached_violations(self, filepath: Path, hook_name: str) -> CheckResult | None:
        try:
            cached = self.cache.get_cached_result(filepath, hook_name)
            if cached is None:
                return None
            if "suppression_usages" not in cached:
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
            suppression_usages = tuple(
                SuppressionUsage(
                    check_id=usage_dict["check_id"],
                    error_code=usage_dict["error_code"],
                    line=usage_dict["line"],
                )
                for usage_dict in cached.get("suppression_usages", [])
            )
        except (KeyError, TypeError, ValueError) as error:
            logger.debug("Cache deserialization failed: %s", repr(error))
            return None
        else:
            return CheckResult(violations, suppression_usages)

    def _cache_violations(self, filepath: Path, hook_name: str, check_result: CheckResult) -> None:
        try:
            serialized = [
                {
                    "check_id": v.check_id,
                    "error_code": v.error_code,
                    "line": v.line,
                    "col": v.col,
                    "message": v.message,
                    "fixable": v.fixable,
                }
                for v in check_result
            ]
            self.cache.set_cached_result(
                filepath,
                hook_name,
                {
                    "violations": serialized,
                    "suppression_usages": [
                        {
                            "check_id": usage.check_id,
                            "error_code": usage.error_code,
                            "line": usage.line,
                        }
                        for usage in check_result.suppression_usages
                    ],
                },
            )
        except (TypeError, ValueError) as error:
            logger.warning("Cache serialization failed: %s", repr(error))

    def _read_source(self, filepath: Path) -> tuple[str, str] | None:

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
        self,
        filepath: Path,
        checks: Sequence[ASTCheck],
        *,
        record_only: Sequence[ASTCheck] = (),
        prior_suppression_usages: tuple[SuppressionUsage, ...] = (),
        prior_active_error_codes: frozenset[str] = frozenset(),
    ) -> CheckResult | None:
        return self._check_file_with_lifecycle(
            filepath,
            checks,
            direct=True,
            record_only=record_only,
            prior_suppression_usages=prior_suppression_usages,
            prior_active_error_codes=prior_active_error_codes,
        )

    def _check_derived_file(self, filepath: Path, checks: Sequence[ASTCheck]) -> CheckResult | None:
        return self._check_file_with_lifecycle(filepath, checks, direct=False)

    def _check_file_with_lifecycle(
        self,
        filepath: Path,
        checks: Sequence[ASTCheck],
        *,
        direct: bool,
        record_only: Sequence[ASTCheck] = (),
        prior_suppression_usages: tuple[SuppressionUsage, ...] = (),
        prior_active_error_codes: frozenset[str] = frozenset(),
    ) -> CheckResult | None:
        if not self.fix_mode or not locking_is_available():
            return self._check_file_locked(
                filepath,
                checks,
                direct=direct,
                record_only=record_only,
                prior_suppression_usages=prior_suppression_usages,
                prior_active_error_codes=prior_active_error_codes,
            )

        try:
            lock_path = _fix_lock_path(filepath)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with locked(
                lock_path,
                timeout_seconds=_FIX_LOCK_TIMEOUT_SECONDS,
                poll_interval_seconds=_FIX_LOCK_POLL_INTERVAL_SECONDS,
            ):
                return self._check_file_locked(
                    filepath,
                    checks,
                    direct=direct,
                    record_only=record_only,
                    prior_suppression_usages=prior_suppression_usages,
                    prior_active_error_codes=prior_active_error_codes,
                )
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
        self,
        filepath: Path,
        checks: Sequence[ASTCheck],
        *,
        direct: bool,
        record_only: Sequence[ASTCheck] = (),
        prior_suppression_usages: tuple[SuppressionUsage, ...] = (),
        prior_active_error_codes: frozenset[str] = frozenset(),
    ) -> CheckResult | None:
        parsed = self._parsed_source(filepath)
        if parsed is None:
            return None
        source, tree = parsed

        all_violations = CheckResult(suppression_usages=prior_suppression_usages)
        active_error_codes = set(prior_active_error_codes)
        regular_checks = [check for check in checks if not isinstance(check, UnusedPytriageCheck)]
        audit_checks = [check for check in checks if isinstance(check, UnusedPytriageCheck)]
        for check in regular_checks:
            if check.check_id in self._unavailable_check_ids:
                continue
            try:
                if audit_checks:
                    violations = _check_with_suppression_tracking(check, filepath, tree, source)
                else:
                    violations = check.check(filepath, tree, source)
            except CheckUnavailableError as error:
                logger.debug("Check %s is unavailable: %s", check.check_id, error, exc_info=True)
                self._record_unavailable_check(check, error)
            except Exception:
                logger.debug("Check %s failed on %s", check.check_id, filepath, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))
            else:
                check_result = _as_check_result(violations)
                all_violations.extend(check_result)
                all_violations.suppression_usages += check_result.suppression_usages
                active_error_codes.add(check.error_code)
                if direct:
                    self._record_direct_input(check, filepath, source)

        for check in audit_checks:
            try:
                audit_result = check.check_with_suppression_usage(
                    source, all_violations.suppression_usages, frozenset(active_error_codes)
                )
            except Exception:
                logger.debug("Check %s failed on %s", check.check_id, filepath, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))
            else:
                all_violations.extend(audit_result)

        if direct:
            for check in record_only:
                self._record_direct_input(check, filepath, source)

        if self.fix_mode and all_violations:
            self._apply_fixes(filepath, all_violations)
            if self._fix_changed_file and audit_checks:
                self._refresh_unused_pytriage(
                    filepath,
                    regular_checks,
                    audit_checks,
                    all_violations,
                    prior_suppression_usages=prior_suppression_usages,
                    prior_active_error_codes=prior_active_error_codes,
                )

        return all_violations

    def _refresh_unused_pytriage(
        self,
        filepath: Path,
        regular_checks: Sequence[ASTCheck],
        audit_checks: Sequence[UnusedPytriageCheck],
        violations_result: CheckResult,
        *,
        prior_suppression_usages: tuple[SuppressionUsage, ...] = (),
        prior_active_error_codes: frozenset[str] = frozenset(),
    ) -> None:
        parsed = self._parsed_source(filepath)
        if parsed is None:
            return
        source, tree = parsed
        usages = list(prior_suppression_usages)
        active_error_codes = set(prior_active_error_codes)
        for check in regular_checks:
            if check.check_id in self._unavailable_check_ids:
                continue
            try:
                fresh = _check_with_suppression_tracking(check, filepath, tree, source)
            except CheckUnavailableError as error:
                logger.debug("Check %s is unavailable while refreshing unused suppressions: %s", check.check_id, error)
                self._record_unavailable_check(check, error)
            except Exception:
                logger.debug("Check %s failed while refreshing unused suppressions", check.check_id, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))
            else:
                fresh_result = _as_check_result(fresh)
                usages.extend(fresh_result.suppression_usages)
                active_error_codes.add(check.error_code)

        fresh_audits = CheckResult()
        for check in audit_checks:
            try:
                fresh_audits.extend(
                    check.check_with_suppression_usage(source, tuple(usages), frozenset(active_error_codes))
                )
            except Exception:
                logger.debug("Check %s failed while refreshing unused suppressions", check.check_id, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))

        violations_result[:] = [
            violation
            for violation in violations_result
            if violation.check_id not in {check.check_id for check in audit_checks}
        ]
        violations_result.extend(fresh_audits)
        violations_result.suppression_usages = tuple(usages)

    def _record_direct_inputs(self, filepath: Path, checks: Sequence[ASTCheck]) -> bool:

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

        initial_violations = _group_by_check_id(violations)
        fixable_check_ids = {v.check_id for v in violations if v.fixable}

        file_changed = False

        externally_modified = False

        for check in self.checks:
            if check.check_id not in fixable_check_ids:
                continue
            try:
                read_result = self._read_source(filepath)
                if read_result is None:
                    self.rule_failures.append((str(filepath), check.check_id))
                    for v in violations:
                        if v.check_id == check.check_id and v.fixable:
                            v.fix_outcome = FixOutcome.ERRORED
                    continue
                current_source, encoding = read_result
                current_tree = ast.parse(current_source, filename=filepath)

                fresh_violations = [v for v in check.check(filepath, current_tree, current_source) if v.fixable]
                if not fresh_violations:
                    continue

                try:
                    fix_result = check.fix(filepath, fresh_violations, current_source, current_tree, encoding)
                except FixValidationError:
                    logger.debug(
                        "Fix for %s produced invalid syntax on %s; the file was left untouched.",
                        check.check_id,
                        filepath,
                        exc_info=True,
                    )
                    fix_result = FixResult.for_violations(fresh_violations, FixOutcome.REJECTED)
                    _set_fix_outcomes(fresh_violations, fix_result)
                except ConcurrentModificationError:
                    logger.debug(
                        "File %s changed on disk while fixing %s; the fix was discarded.",
                        filepath,
                        check.check_id,
                        exc_info=True,
                    )
                    fix_result = FixResult.for_violations(fresh_violations, FixOutcome.ABORTED)
                    _set_fix_outcomes(fresh_violations, fix_result)
                    file_changed = True
                    externally_modified = True
                except Exception:
                    logger.debug(
                        "Fix for %s raised an unexpected exception on %s.",
                        check.check_id,
                        filepath,
                        exc_info=True,
                    )
                    self.rule_failures.append((str(filepath), check.check_id))
                    still_present = self._mark_resolved_and_get_still_present(filepath, check, fresh_violations)
                    if len(still_present) < len(fresh_violations):
                        file_changed = True
                    for v in fresh_violations:
                        if (v.line, v.col, v.message) in still_present:
                            v.fix_outcome = FixOutcome.ERRORED
                else:
                    _set_fix_outcomes(fresh_violations, fix_result)
                    still_present = self._mark_resolved_and_get_still_present(filepath, check, fresh_violations)
                    if len(still_present) < len(fresh_violations):
                        file_changed = True
                violations[:] = [v for v in violations if v.check_id != check.check_id or not v.fixable]
                violations.extend(fresh_violations)
            except Exception:
                logger.debug("Fix failed for %s on %s", check.check_id, filepath, exc_info=True)
                self.rule_failures.append((str(filepath), check.check_id))
                for v in violations:
                    if v.check_id == check.check_id and v.fixable:
                        v.fix_outcome = FixOutcome.ERRORED

        if file_changed:
            self._refresh_stale_positions(filepath, violations)
            if not externally_modified:
                _record_indirect_resolutions(violations, initial_violations)
        self._fix_changed_file = file_changed

    def _refresh_stale_positions(
        self,
        filepath: Path,
        violations: list[Violation],
    ) -> None:

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
                continue
            check_entries = [v for v in violations if v.check_id == check.check_id]
            if not check_entries or any(_has_terminal_fix_state(v) for v in check_entries):
                continue

            stale = [v for v in check_entries if v.fix_outcome is not FixOutcome.APPLIED]
            if not stale:
                continue

            try:
                fresh = check.check(filepath, final_tree, final_source)
            except Exception:
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
        post_read_result = self._read_source(filepath)
        if post_read_result is None:
            for violation in fresh_violations:
                if violation.fix_outcome is FixOutcome.APPLIED:
                    violation.fix_outcome = FixOutcome.DECLINED
            return {(v.line, v.col, v.message) for v in fresh_violations}

        post_source, _post_encoding = post_read_result
        try:
            post_tree = ast.parse(post_source, filename=filepath)
        except SyntaxError:
            for violation in fresh_violations:
                if violation.fix_outcome is FixOutcome.APPLIED:
                    violation.fix_outcome = FixOutcome.DECLINED
            return {(v.line, v.col, v.message) for v in fresh_violations}
        still_present: set[ViolationKey] = {
            (v.line, v.col, v.message) for v in check.check(filepath, post_tree, post_source) if v.fixable
        }
        for v in fresh_violations:
            if (v.line, v.col, v.message) not in still_present and v.fix_outcome is None:
                v.fix_outcome = FixOutcome.APPLIED
            elif (v.line, v.col, v.message) in still_present and v.fix_outcome is FixOutcome.APPLIED:
                v.fix_outcome = FixOutcome.DECLINED
        return still_present


def load_checks(
    select: set[str] | None = None,
    ignore: set[str] | None = None,
    check_args: dict[str, Any] | None = None,
    check_classes: Sequence[type[ASTCheck]] | None = None,
) -> list[ASTCheck]:

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
        if select is None and not check.default_enabled:
            continue

        args = check_args.get(check_id, {})
        if args:
            try:
                check = check_class(**args)
            except Exception:
                logger.exception("Failed to load check %s", check_id)
                continue

        checks.append(check)

    return checks
