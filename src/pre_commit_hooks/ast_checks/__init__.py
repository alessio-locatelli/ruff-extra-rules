"""Grouped AST-based linter for pre-commit hooks.

This module provides a unified interface for running multiple AST-based checks
in a single pass, improving performance by eliminating redundant file I/O and
AST parsing operations.

Error Codes
-----------
  - TRI001: Forbid meaningless variable names (forbid-vars)
  - TRI002: Excessive blank lines (excessive-blank-lines)
  - TRI003: Redundant super init (redundant-super-init)
  - TRI004: Function naming violations (validate-function-name)
  - TRI005: Redundant variable assignments (redundant-assignment)
  - STYLE-001: Comment misplaced on closing bracket line (misplaced-comment)

Inline Ignore Comments
----------------------
Use `# pytriage: ignore=<code>` to suppress specific violations.

Example:
    data = [1, 2, 3]  # pytriage: ignore=TRI001
    def get_users():  # pytriage: ignore=TRI004
        return []
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pre_commit_hooks._cache import CacheManager
from pre_commit_hooks._prefilter import batch_filter_files

from ._base import (
    ASTCheck,
    FixValidationError,
    Violation,
    is_fix_errored,
    is_fix_rejected,
    is_fixed,
    mark_fix_errored,
    mark_fix_rejected,
    mark_fixed,
    read_source_with_encoding,
)
from .excessive_blank_lines import ExcessiveBlankLinesCheck
from .forbid_vars import ForbidVarsCheck
from .misplaced_comment import MisplacedCommentCheck
from .redundant_assignment import RedundantAssignmentCheck
from .redundant_super_init import RedundantSuperInitCheck
from .validate_function_name import ValidateFunctionNameCheck


def filter_excluded_files(filepaths: list[str], exclude_patterns: list[str]) -> list[str]:
    """Filter out files matching exclude patterns.

    Args:
        filepaths: List of file paths to filter
        exclude_patterns: List of glob patterns to exclude

    Returns:
        Filtered list of file paths
    """
    if not exclude_patterns:
        return filepaths

    filtered = []
    for filepath_str in filepaths:
        filepath = Path(filepath_str)
        excluded = False

        for pattern in exclude_patterns:
            # Check if file matches pattern using glob-style matching
            # Support both relative and absolute patterns
            if filepath.match(pattern):
                excluded = True
                break
            # Also check if any parent directory matches
            if any(part for part in filepath.parts if Path(part).match(pattern)):
                excluded = True
                break

        if not excluded:
            filtered.append(filepath_str)

    return filtered


logger = logging.getLogger("ast_checks")

# The complete, fixed set of checks the ruff-extra-rules hook can run. This
# package has no plugin mechanism for third-party checks, so a static list is
# all that's needed — add new checks here rather than via a registration
# side effect.
ALL_CHECKS: list[type[ASTCheck]] = [
    ForbidVarsCheck,
    ExcessiveBlankLinesCheck,
    RedundantSuperInitCheck,
    ValidateFunctionNameCheck,
    RedundantAssignmentCheck,
    MisplacedCommentCheck,
]

# src/pre_commit_hooks/ — the tree CacheManager.compute_tree_hash() hashes to
# invalidate every cached result whenever any check's own code, or shared
# code it depends on, changes.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


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
        """Initialize the orchestrator.

        Args:
            checks: List of check instances to run
            fix_mode: If True, apply auto-fixes for fixable violations
        """
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
        """Process files and return violations for each file.

        Args:
            filepaths: List of file paths to check

        Returns:
            Dict mapping filepath to list of violations. A file that
            couldn't be read or parsed has no entry here (indistinguishable
            from "processed, zero violations") — check
            `self.unprocessable_files` for those. A file where one check
            crashed while others ran fine can still have an entry here (the
            other checks' violations), but its results are incomplete —
            check `self.rule_failures` for those.
        """
        self.unprocessable_files = []
        self.rule_failures = []

        if not filepaths:
            return {}

        # Step 1: Aggregate pre-filter patterns from all checks
        patterns = []
        for check in self.checks:
            check_patterns = check.get_prefilter_pattern()
            if check_patterns:
                patterns.extend(check_patterns)

        # Step 2: Pre-filter files (OR logic: file matches if it contains ANY pattern).
        # No patterns means all files need to be checked.
        candidate_files = batch_filter_files(filepaths, patterns) if patterns else filepaths

        if not candidate_files:
            return {}

        # Step 3: Process each file. self.cache's own cache_version (set at
        # construction from _generate_cache_key()) already gates staleness —
        # no separate per-file cache_key needed here.
        all_violations: dict[str, list[Violation]] = {}

        for filepath_str in candidate_files:
            filepath = Path(filepath_str)

            # Try cache first (skip in fix mode since file will be modified)
            cached_violations: list[Violation] | None = None
            if not self.fix_mode:
                cached_violations = self._get_cached_violations(filepath)

            violations: list[Violation] | None
            if cached_violations is not None:
                # Cache hit
                violations = cached_violations
            else:
                # Cache miss - run checks
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
        """Cache key from the enabled checks, their own config, and this
        package's own source — replaces a hand-maintained CACHE_VERSION
        constant that a developer had to remember to bump whenever any
        check's behavior changed (a real bug, commit 0e3efba, already came
        from forgetting to). Any of the three changing invalidates every
        cached result for every check — deliberately coarse-grained in
        exchange for never missing a real change again.
        """
        check_ids = sorted(check.check_id for check in self.checks)
        fingerprints = sorted(f"{check.check_id}={_fingerprint_check(check)}" for check in self.checks)
        tree_hash = CacheManager.compute_tree_hash(_PACKAGE_ROOT)
        return "|".join([",".join(check_ids), ",".join(fingerprints), tree_hash])

    def _get_cached_violations(self, filepath: Path) -> list[Violation] | None:
        """Retrieve cached violations for a file.

        Args:
            filepath: Path to file

        Returns:
            List of violations if cache hit, None if cache miss
        """
        try:
            # self.cache's own cache_version already rejects a stale entry
            # (enabled checks, their config, or this package's own source
            # changed since it was written) before this ever sees it.
            cached = self.cache.get_cached_result(filepath, "ruff-extra-rules")
            if cached is None:
                return None

            # Deserialize violations
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
        """Cache violations for a file.

        Args:
            filepath: Path to file
            violations: List of violations to cache
        """
        try:
            # Serialize violations (skip fix_data as it may contain
            # non-serializable objects like AST nodes)
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
        """Read a file's content, honoring a PEP 263 encoding declaration.

        Thin error-handling wrapper around read_source_with_encoding: logs
        and returns None on any failure instead of raising, since every
        caller here treats "file couldn't be processed" the same way.

        Args:
            filepath: Path to file

        Returns:
            (source, encoding) so a fix() can write back in the same
            encoding, or None if the file couldn't be read/decoded
        """
        try:
            return read_source_with_encoding(filepath)
        except OSError:
            logger.exception("Failed to read %s", filepath)
            return None
        except SyntaxError:
            logger.exception("Failed to detect encoding for %s", filepath)
            return None
        except UnicodeDecodeError, LookupError:
            logger.exception("Failed to decode %s", filepath)
            return None

    def _check_file(self, filepath: Path) -> list[Violation] | None:
        """Check a file with all enabled checks.

        Args:
            filepath: Path to file

        Returns:
            List of violations, or None if file couldn't be processed
        """
        read_result = self._read_source(filepath)
        if read_result is None:
            return None
        source, _encoding = read_result

        try:
            # Parse AST once
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            logger.exception("Failed to parse %s", filepath)
            return None

        # Run all checks on the same tree
        all_violations: list[Violation] = []
        for check in self.checks:
            try:
                violations = check.check(filepath, tree, source)
                all_violations.extend(violations)
            except Exception:
                logger.exception("Check %s failed on %s", check.check_id, filepath)
                self.rule_failures.append((str(filepath), check.check_id))

        # Apply fixes if in fix mode
        if self.fix_mode and all_violations:
            self._apply_fixes(filepath, all_violations)

        return all_violations

    def _apply_fixes(
        self,
        filepath: Path,
        violations: list[Violation],
    ) -> None:
        """Apply fixes for fixable violations.

        Args:
            filepath: Path to file
            violations: All violations found in file so far this run.
                Mutated in place: each fixable check's own stale entries
                (collected once, before any fix ran) are replaced with a
                freshly recomputed list, each marked fixed/rejected/errored/
                left alone against the file's actual post-fix state. Matching a
                stale entry back to "is this the same violation, now fixed"
                by identity isn't reliable — an earlier check's own fix can
                shift line/col numbers, and two distinct violations can
                share an identical message (e.g. a same-named free function
                and method both suggesting the same rename) — so the stale
                entries for this check_id are discarded outright rather
                than matched.
        """
        # Which checks reported at least one fixable violation
        fixable_check_ids = {v.check_id for v in violations if v.fixable}

        # Apply fixes for each check
        for check in self.checks:
            if check.check_id not in fixable_check_ids:
                continue
            try:
                # Re-read source in case a previous check's fix in this same
                # loop already modified the file
                read_result = self._read_source(filepath)
                if read_result is None:
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
                    # check's fix logic, not an expected outcome.
                    logger.exception(
                        "Fix for %s produced invalid syntax on %s; the file was left untouched.",
                        check.check_id,
                        filepath,
                    )
                    for v in fresh_violations:
                        mark_fix_rejected(v)
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
                    logger.exception(
                        "Fix for %s raised an unexpected exception on %s.",
                        check.check_id,
                        filepath,
                    )
                    # Always recorded, even if every fresh_violations entry
                    # turns out resolved below (e.g. fix() committed its
                    # edits, then raised afterwards during unrelated
                    # cleanup): an exception genuinely happened here, and
                    # that must never become invisible to the user just
                    # because nothing is left to mark [FIX ERRORED].
                    self.rule_failures.append((str(filepath), check.check_id))
                    still_present = self._mark_resolved_and_get_still_present(filepath, check, fresh_violations)
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
                    self._mark_resolved_and_get_still_present(filepath, check, fresh_violations)
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
                logger.exception("Fix failed for %s on %s", check.check_id, filepath)

    def _mark_resolved_and_get_still_present(
        self,
        filepath: Path,
        check: ASTCheck,
        fresh_violations: list[Violation],
    ) -> set[tuple[int, int, str]]:
        """Re-check `filepath` against its actual current on-disk content
        and call `mark_fixed()` on every violation in `fresh_violations`
        that's no longer present there — regardless of whether `check.fix()`
        returned normally or raised partway through. A check that writes
        more than once per `fix()` call (looping over violations
        individually, like `validate_function_name`) can have already
        committed some violations before a later one failed or raised;
        matching by (line, col, message) against the file's real state,
        rather than trusting a bool return or "fix() didn't raise", is what
        catches that.

        Returns:
            The (line, col, message) keys of `fresh_violations` still
            present, so a caller with more context (e.g. "fix() itself
            raised for this check") can mark those specifically, distinct
            from the ones already resolved by this call. If the file
            couldn't be re-read (e.g. deleted concurrently), conservatively
            returns every key unresolved — nothing is marked fixed on an
            unverifiable outcome.
        """
        post_read_result = self._read_source(filepath)
        if post_read_result is None:
            return {(v.line, v.col, v.message) for v in fresh_violations}

        post_source, _post_encoding = post_read_result
        post_tree = ast.parse(post_source, filename=str(filepath))
        still_present = {(v.line, v.col, v.message) for v in check.check(filepath, post_tree, post_source) if v.fixable}
        for v in fresh_violations:
            if (v.line, v.col, v.message) not in still_present:
                mark_fixed(v)
        return still_present


def load_checks(
    select: set[str] | None = None,
    ignore: set[str] | None = None,
    check_args: dict[str, Any] | None = None,
) -> list[ASTCheck]:
    """Load and instantiate checks based on select/ignore sets.

    Mirrors `ruff check --select`/`--ignore`: `select` narrows the
    candidate set (None = all checks), and `ignore` always subtracts from
    whatever that candidate set is, whether or not `select` was given.

    Args:
        select: Set of check IDs to restrict to (None = all checks)
        ignore: Set of check IDs to exclude, applied on top of `select`
        check_args: Dict of check-specific arguments

    Returns:
        List of instantiated check objects
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

        # Determine if check should be loaded
        if select is not None and check_id not in select:
            continue
        if ignore is not None and check_id in ignore:
            continue

        # Re-instantiate with check-specific arguments, if any were given
        args = check_args.get(check_id, {})
        if args:
            try:
                check = check_class(**args)
            except Exception:
                logger.exception("Failed to load check %s", check_id)
                continue

        checks.append(check)

    return checks


def main(argv: list[str] | None = None) -> int:
    """Main entry point for grouped AST checks.

    Args:
        argv: Command-line arguments

    Returns:
        0: no violations, and every requested file was read, parsed, and
            checked without error (this includes a `--fix` run that
            resolved every violation — matching the pre-commit convention
            that a hook only reports success when the working tree needs
            no further review, not `ruff check --fix`'s own bare-CLI
            default of exit 0 on a fully-fixed run).
        1: any of — a violation is present in the report (fixed, fixable,
            rejected, errored, or non-fixable; see the tags in each printed
            line); a file couldn't be read, decoded, or parsed
            (`--list-checks` and `--exclude`d files, so also `orchestrator.
            unprocessable_files`); a check raised while analyzing a file
            (`orchestrator.rule_failures`); or invalid CLI input (unknown
            `--select`/`--ignore` check id, or every check disabled).
            `--list-checks` and no-files-to-check return 0 unconditionally,
            before any of the above can apply.

        `argparse` itself calls `sys.exit(2)` directly (bypassing this
        function's own return) for malformed CLI arguments themselves (e.g.
        an unknown flag) — a third, separate value from this function's
        0/1 contract above.
    """
    parser = argparse.ArgumentParser(
        prog="ruff-extra-rules",
        description="Run multiple AST-based checks in a single pass",
    )
    parser.add_argument("filenames", nargs="*", help="Python files to check")
    parser.add_argument(
        "--select",
        help="Comma-separated list of checks to restrict to (default: all)",
    )
    parser.add_argument(
        "--ignore",
        help="Comma-separated list of checks to exclude",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-fix violations where possible",
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="List available checks and exit",
    )
    parser.add_argument(
        "--exclude",
        help="Glob pattern(s) to exclude files/directories (comma-separated)",
    )

    # Check-specific arguments: each check registers its own, if any
    for check_class in ALL_CHECKS:
        check_class.add_cli_arguments(parser)

    args = parser.parse_args(argv)

    # List checks if requested
    if args.list_checks:
        print("Available checks:")
        instances = sorted((cls() for cls in ALL_CHECKS), key=lambda c: c.check_id)
        for check in instances:
            print(f"  - {check.check_id}: {check.error_code}")
        return 0

    # No files to check
    if not args.filenames:
        return 0

    # Filter excluded files
    exclude_patterns = []
    if args.exclude:
        exclude_patterns = [p.strip() for p in args.exclude.split(",") if p.strip()]

    filenames = filter_excluded_files(args.filenames, exclude_patterns)
    if not filenames:
        # All files were excluded
        return 0

    # Parse select/ignore sets
    select = {c.strip() for c in args.select.split(",")} if args.select else None
    ignore = {c.strip() for c in args.ignore.split(",")} if args.ignore else None

    # Validate check IDs
    all_check_ids = {cls().check_id for cls in ALL_CHECKS}
    if select:
        invalid = select - all_check_ids
        if invalid:
            checks_str = ", ".join(sorted(invalid))
            print(f"Error: Unknown checks: {checks_str}", file=sys.stderr)
            return 1
    if ignore:
        invalid = ignore - all_check_ids
        if invalid:
            checks_str = ", ".join(sorted(invalid))
            print(f"Error: Unknown checks: {checks_str}", file=sys.stderr)
            return 1

    # Build check-specific arguments: each check translates its own parsed
    # CLI args into its own __init__ kwargs, if any
    check_args: dict[str, dict[str, Any]] = {}
    for check_class in ALL_CHECKS:
        kwargs = check_class.cli_kwargs_from_args(args)
        if kwargs:
            check_args[check_class().check_id] = kwargs

    # Load checks
    checks = load_checks(select=select, ignore=ignore, check_args=check_args)

    if not checks:
        print("Error: No checks enabled", file=sys.stderr)
        return 1

    # Run orchestrator
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=args.fix)
    all_violations = orchestrator.process_files(filenames)

    # Report results
    exit_code = 0

    # A file that couldn't be read or parsed must never look identical to a
    # clean file: report it and fail the run, rather than letting it vanish
    # from all_violations with only a debug log line as evidence.
    for filepath in sorted(orchestrator.unprocessable_files):
        print(f"{filepath}: error: could not be read or parsed; file skipped", file=sys.stderr)
        exit_code = 1

    # A check that crashes on every file it sees must not look like a clean
    # run merely because no other check reported anything for the same
    # files — report the specific check and file, and fail the run.
    for filepath, check_id in sorted(orchestrator.rule_failures):
        print(
            f"{filepath}: error: check '{check_id}' raised an unexpected exception; "
            "its results for this file may be incomplete",
            file=sys.stderr,
        )
        exit_code = 1

    for filepath, violations in sorted(all_violations.items()):
        for v in violations:
            fixed = is_fixed(v)
            rejected = is_fix_rejected(v)
            errored = is_fix_errored(v)
            if fixed:
                tag = "[FIXED] "
            elif rejected:
                tag = "[FIX REJECTED] "
            elif errored:
                tag = "[FIX ERRORED] "
            elif v.fixable:
                tag = "[FIXABLE] "
            else:
                tag = ""
            if rejected:
                hint = (
                    " --fix produced invalid syntax, so the change was discarded — this is a bug, "
                    "please report it: https://github.com/alessio-locatelli/ruff-extra-rules/issues"
                )
            elif errored:
                hint = (
                    " --fix raised an unexpected internal error and was not applied — this is a bug, "
                    "please report it: https://github.com/alessio-locatelli/ruff-extra-rules/issues"
                )
            elif v.fixable and not fixed:
                hint = " Run with --fix to inline automatically."
            else:
                hint = ""
            print(
                f"{filepath}:{v.line}: {v.error_code}: {tag}{v.message}{hint}",
                file=sys.stderr,
            )
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
