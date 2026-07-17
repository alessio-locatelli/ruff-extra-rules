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

from ._base import ASTCheck, Violation, is_fixed, mark_fixed, read_source_with_encoding
from .excessive_blank_lines import ExcessiveBlankLinesCheck
from .forbid_vars import ForbidVarsCheck
from .misplaced_comment import MisplacedCommentCheck
from .redundant_assignment import RedundantAssignmentCheck
from .redundant_super_init import RedundantSuperInitCheck
from .validate_function_name import ValidateFunctionNameCheck


def filter_excluded_files(
    filepaths: list[str], exclude_patterns: list[str]
) -> list[str]:
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

# The complete, fixed set of checks the ast-checks hook can run. This package
# has no plugin mechanism for third-party checks, so a static list is all
# that's needed — add new checks here rather than via a registration side effect.
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


def _fingerprint_default(value: object) -> Any:
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
        fix_mode: bool = False,
    ) -> None:
        """Initialize the orchestrator.

        Args:
            checks: List of check instances to run
            fix_mode: If True, apply auto-fixes for fixable violations
        """
        self.checks = checks
        self.fix_mode = fix_mode
        self.cache = CacheManager(
            hook_name="ast-checks", cache_version=self._generate_cache_key()
        )

    def process_files(self, filepaths: list[str]) -> dict[str, list[Violation]]:
        """Process files and return violations for each file.

        Args:
            filepaths: List of file paths to check

        Returns:
            Dict mapping filepath to list of violations
        """
        if not filepaths:
            return {}

        # Step 1: Aggregate pre-filter patterns from all checks
        patterns = []
        for check in self.checks:
            check_patterns = check.get_prefilter_pattern()
            if check_patterns:
                patterns.extend(check_patterns)

        # Step 2: Pre-filter files (OR logic: file matches if it contains ANY pattern)
        if patterns:
            candidate_files = batch_filter_files(filepaths, patterns)
        else:
            # No patterns means all files need to be checked
            candidate_files = filepaths

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
                violations = self._check_file(filepath)

                # Cache results (only if not in fix mode)
                if not self.fix_mode and violations is not None:
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
        fingerprints = sorted(
            f"{check.check_id}={_fingerprint_check(check)}" for check in self.checks
        )
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
            cached = self.cache.get_cached_result(filepath, "ast-checks")
            if cached is None:
                return None

            # Deserialize violations
            violations = []
            for v_dict in cached.get("violations", []):
                violations.append(
                    Violation(
                        check_id=v_dict["check_id"],
                        error_code=v_dict["error_code"],
                        line=v_dict["line"],
                        col=v_dict["col"],
                        message=v_dict["message"],
                        fixable=v_dict["fixable"],
                        fix_data=v_dict.get("fix_data"),
                    )
                )
            return violations
        except (KeyError, TypeError, ValueError) as error:
            logger.debug("Cache deserialization failed: %s", repr(error))
            return None

    def _cache_violations(self, filepath: Path, violations: list[Violation]) -> None:
        """Cache violations for a file.

        Args:
            filepath: Path to file
            violations: List of violations to cache
        """
        try:
            # Serialize violations (skip fix_data as it may contain
            # non-serializable objects like AST nodes)
            serialized = []
            for v in violations:
                serialized.append(
                    {
                        "check_id": v.check_id,
                        "error_code": v.error_code,
                        "line": v.line,
                        "col": v.col,
                        "message": v.message,
                        "fixable": v.fixable,
                        # Note: fix_data is NOT cached as it may contain AST nodes
                    }
                )

            self.cache.set_cached_result(
                filepath, "ast-checks", {"violations": serialized}
            )
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
        except OSError as error:
            logger.error("Failed to read %s: %s", filepath, repr(error))
            return None
        except SyntaxError as error:
            logger.error("Failed to detect encoding for %s: %s", filepath, repr(error))
            return None
        except (UnicodeDecodeError, LookupError) as error:
            logger.error("Failed to decode %s: %s", filepath, repr(error))
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
        except SyntaxError as syntax_error:
            logger.error("Failed to parse %s: %s", filepath, repr(syntax_error))
            return None

        # Run all checks on the same tree
        all_violations: list[Violation] = []
        for check in self.checks:
            try:
                violations = check.check(filepath, tree, source)
                all_violations.extend(violations)
            except Exception as check_error:  # noqa: BLE001
                logger.error(
                    "Check %s failed on %s: %s",
                    check.check_id,
                    filepath,
                    repr(check_error),
                )

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
            violations: All violations found in file (used only to know which
                checks reported something fixable, and to mark the caller's
                own Violation objects as fixed for reporting — the actual
                positions handed to each check's fix() are recomputed fresh
                below, never taken from this stale list)
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
                fresh_violations = [
                    v
                    for v in check.check(filepath, current_tree, current_source)
                    if v.fixable
                ]
                if not fresh_violations:
                    continue

                success = check.fix(
                    filepath, fresh_violations, current_source, current_tree, encoding
                )
                if success:
                    # Mark the original (stale) violations as fixed so the
                    # caller's reporting loop shows [FIXED] correctly.
                    for v in violations:
                        if v.check_id == check.check_id and v.fixable:
                            mark_fixed(v)
            except Exception as fix_error:  # noqa: BLE001
                logger.error(
                    "Fix failed for %s on %s: %s",
                    check.check_id,
                    filepath,
                    repr(fix_error),
                )


def load_checks(
    enabled: set[str] | None = None,
    disabled: set[str] | None = None,
    check_args: dict[str, Any] | None = None,
) -> list[ASTCheck]:
    """Load and instantiate checks based on enabled/disabled sets.

    Args:
        enabled: Set of check IDs to enable (None = all checks)
        disabled: Set of check IDs to disable
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
        except Exception as init_error:  # noqa: BLE001
            logger.error(
                "Failed to load check %s: %s", check_class.__name__, repr(init_error)
            )
            continue

        check_id = check.check_id

        # Determine if check should be loaded
        if enabled is not None:
            # Explicit enable list - only load if in list
            if check_id not in enabled:
                continue
        elif disabled is not None and check_id in disabled:
            # Explicit disable list - skip if in list
            continue

        # Re-instantiate with check-specific arguments, if any were given
        args = check_args.get(check_id, {})
        if args:
            try:
                check = check_class(**args)
            except Exception as init_error:  # noqa: BLE001
                logger.error("Failed to load check %s: %s", check_id, repr(init_error))
                continue

        checks.append(check)

    return checks


def main(argv: list[str] | None = None) -> int:
    """Main entry point for grouped AST checks.

    Args:
        argv: Command-line arguments

    Returns:
        Exit code (0 if no violations, 1 if violations found)
    """
    parser = argparse.ArgumentParser(
        prog="ast-checks",
        description="Run multiple AST-based checks in a single pass",
    )
    parser.add_argument("filenames", nargs="*", help="Python files to check")
    parser.add_argument(
        "--enable",
        help="Comma-separated list of checks to enable (default: all)",
    )
    parser.add_argument(
        "--disable",
        help="Comma-separated list of checks to disable",
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

    # Parse enabled/disabled sets
    enabled = {c.strip() for c in args.enable.split(",")} if args.enable else None
    disabled = {c.strip() for c in args.disable.split(",")} if args.disable else None

    # Validate check IDs
    all_check_ids = {cls().check_id for cls in ALL_CHECKS}
    if enabled:
        invalid = enabled - all_check_ids
        if invalid:
            checks_str = ", ".join(sorted(invalid))
            print(f"Error: Unknown checks: {checks_str}", file=sys.stderr)
            return 1
    if disabled:
        invalid = disabled - all_check_ids
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
    checks = load_checks(enabled=enabled, disabled=disabled, check_args=check_args)

    if not checks:
        print("Error: No checks enabled", file=sys.stderr)
        return 1

    # Run orchestrator
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=args.fix)
    all_violations = orchestrator.process_files(filenames)

    # Report results
    exit_code = 0
    for filepath, violations in sorted(all_violations.items()):
        for v in violations:
            fixed = is_fixed(v)
            if fixed:
                tag = "[FIXED] "
            elif v.fixable:
                tag = "[FIXABLE] "
            else:
                tag = ""
            hint = (
                " Run with --fix to inline automatically."
                if v.fixable and not fixed
                else ""
            )
            print(
                f"{filepath}:{v.line}: {v.error_code}: {tag}{v.message}{hint}",
                file=sys.stderr,
            )
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
