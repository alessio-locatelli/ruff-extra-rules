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
import logging
import sys
from pathlib import Path
from typing import Any

from pre_commit_hooks._cache import CacheManager
from pre_commit_hooks._prefilter import batch_filter_files

from ._base import ASTCheck, Violation


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

# Check registry will be populated as checks are implemented
_CHECK_REGISTRY: dict[str, type[ASTCheck]] = {}


def register_check(check_class: type[ASTCheck]) -> type[ASTCheck]:
    """Register a check class in the global registry.

    Args:
        check_class: Check class to register

    Returns:
        The same check class (for use as decorator)
    """
    # Instantiate to get check_id
    instance = check_class()
    _CHECK_REGISTRY[instance.check_id] = check_class
    return check_class


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
        self.cache = CacheManager(hook_name="ast-checks")

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

        # Step 3: Generate cache key from enabled checks
        cache_key = self._generate_cache_key()

        # Step 4: Process each file
        all_violations: dict[str, list[Violation]] = {}

        for filepath_str in candidate_files:
            filepath = Path(filepath_str)

            # Try cache first (skip in fix mode since file will be modified)
            cached_violations: list[Violation] | None = None
            if not self.fix_mode:
                cached_violations = self._get_cached_violations(filepath, cache_key)

            violations: list[Violation] | None
            if cached_violations is not None:
                # Cache hit
                violations = cached_violations
            else:
                # Cache miss - run checks
                violations = self._check_file(filepath)

                # Cache results (only if not in fix mode)
                if not self.fix_mode and violations is not None:
                    self._cache_violations(filepath, cache_key, violations)

            if violations is not None and violations:
                all_violations[filepath_str] = violations

        return all_violations

    def _generate_cache_key(self) -> str:
        """Generate cache key from enabled checks.

        Returns:
            Cache key string (sorted, comma-separated check IDs)
        """
        check_ids = sorted(check.check_id for check in self.checks)
        return ",".join(check_ids)

    def _get_cached_violations(
        self, filepath: Path, cache_key: str
    ) -> list[Violation] | None:
        """Retrieve cached violations for a file.

        Args:
            filepath: Path to file
            cache_key: Cache key for enabled checks

        Returns:
            List of violations if cache hit, None if cache miss
        """
        try:
            cached = self.cache.get_cached_result(filepath, "ast-checks")
            if cached is None:
                return None

            # Verify cache key matches (enabled checks haven't changed)
            if cached.get("cache_key") != cache_key:
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

    def _cache_violations(
        self, filepath: Path, cache_key: str, violations: list[Violation]
    ) -> None:
        """Cache violations for a file.

        Args:
            filepath: Path to file
            cache_key: Cache key for enabled checks
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
                filepath,
                "ast-checks",
                {"cache_key": cache_key, "violations": serialized},
            )
        except (TypeError, ValueError) as error:
            logger.warning("Cache serialization failed: %s", repr(error))

    def _check_file(self, filepath: Path) -> list[Violation] | None:
        """Check a file with all enabled checks.

        Args:
            filepath: Path to file

        Returns:
            List of violations, or None if file couldn't be processed
        """
        try:
            # Read file
            source = filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            logger.error("Failed to read %s: %s", filepath, repr(error))
            return None

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
            self._apply_fixes(filepath, all_violations, source, tree)

        return all_violations

    def _apply_fixes(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
    ) -> None:
        """Apply fixes for fixable violations.

        Args:
            filepath: Path to file
            violations: All violations found in file
            source: Original source code
            tree: Parsed AST tree
        """
        # Group violations by check
        by_check: dict[str, list[Violation]] = {}
        for v in violations:
            if v.fixable:
                by_check.setdefault(v.check_id, []).append(v)

        # Apply fixes for each check
        for check in self.checks:
            check_violations = by_check.get(check.check_id, [])
            if check_violations:
                try:
                    # Re-read source in case previous fix modified it
                    current_source = filepath.read_text(encoding="utf-8")
                    current_tree = ast.parse(current_source, filename=str(filepath))

                    success = check.fix(
                        filepath, check_violations, current_source, current_tree
                    )
                    if success:
                        # Mark violations as fixed
                        for v in check_violations:
                            if v.fix_data is None:
                                v.fix_data = {}
                            v.fix_data["fixed"] = True
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

    checks = []

    for check_id, check_class in _CHECK_REGISTRY.items():
        # Determine if check should be loaded
        if enabled is not None:
            # Explicit enable list - only load if in list
            if check_id not in enabled:
                continue
        elif disabled is not None and check_id in disabled:
            # Explicit disable list - skip if in list
            continue

        # Instantiate with check-specific arguments
        try:
            args = check_args.get(check_id, {})
            check = check_class(**args) if args else check_class()
            checks.append(check)
        except Exception as init_error:  # noqa: BLE001
            logger.error("Failed to load check %s: %s", check_id, repr(init_error))

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

    # Check-specific arguments
    parser.add_argument(
        "--forbid-vars-names",
        help="Forbidden variable names for forbid-vars check (default: data,result)",
    )

    args = parser.parse_args(argv)

    # List checks if requested
    if args.list_checks:
        print("Available checks:")
        for check_id in sorted(_CHECK_REGISTRY.keys()):
            check = _CHECK_REGISTRY[check_id]()
            print(f"  - {check_id}: {check.error_code}")
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
    all_check_ids = set(_CHECK_REGISTRY.keys())
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

    # Build check-specific arguments
    check_args: dict[str, dict[str, Any]] = {}
    if args.forbid_vars_names:
        names_list = args.forbid_vars_names.split(",")
        forbidden_names = {n.strip() for n in names_list if n.strip()}
        check_args["forbid-vars"] = {"forbidden_names": forbidden_names}

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
            fixed = bool(v.fix_data and v.fix_data.get("fixed", False))
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


# Import checks to register them (must be at end of file)
from . import (  # noqa: E402
    excessive_blank_lines,  # noqa: F401
    forbid_vars,  # noqa: F401
    misplaced_comment,  # noqa: F401
    redundant_assignment,  # noqa: F401
    redundant_super_init,  # noqa: F401
    validate_function_name,  # noqa: F401
)
