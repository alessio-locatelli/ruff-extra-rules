"""Base protocols and data structures for AST-based checks."""

from __future__ import annotations

import ast
import io
import logging
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("ast_checks")


@dataclass
class Violation:
    """Represents a single violation found by a check.

    Attributes:
        check_id: Unique identifier for the check (e.g., "forbid-vars")
        error_code: Error code for the violation (e.g., "TRI001")
        line: Line number where the violation occurs
        col: Column offset where the violation occurs
        message: Human-readable description of the violation
        fixable: Whether the violation can be auto-fixed
        fix_data: Check-specific data needed for applying the fix
    """

    check_id: str
    error_code: str
    line: int
    col: int
    message: str
    fixable: bool
    fix_data: dict[str, Any] | None = None


class ASTCheck(Protocol):
    """Protocol that all AST-based checks must implement.

    This protocol defines the interface for pluggable AST checks in the
    grouped linter. Each check should be independent and stateless with
    respect to file processing.
    """

    @property
    def check_id(self) -> str:
        """Unique identifier for this check.

        Examples: "forbid-vars", "redundant-super-init", "validate-function-name"

        Returns:
            Check identifier string
        """
        ...

    @property
    def error_code(self) -> str:
        """Error code prefix for violations from this check.

        Examples: "TRI001", "TRI002", "TRI003"

        Returns:
            Error code string
        """
        ...

    def get_prefilter_pattern(self) -> list[str] | None:
        """Patterns for git grep pre-filtering.

        Return fixed string patterns that identify files that might
        contain violations for this check. If None, all files will be
        checked (no pre-filtering). Multiple patterns are combined with
        OR logic — a file is a candidate if it contains ANY of the patterns.

        Returns:
            List of pattern strings for git grep, or None for no filtering

        Examples:
            - ["def get_"] for validate-function-name
            - ["super().__init__"] for redundant-super-init
            - ["data", "result"] for forbid-vars
            - None for excessive-blank-lines (check all files)
        """
        ...

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        """Run check on a file and return violations.

        Args:
            filepath: Path to the file being checked
            tree: Parsed AST tree of the file
            source: Original source code as string

        Returns:
            List of violations found in the file
        """
        ...

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool:
        """Apply fixes for the given violations.

        Args:
            filepath: Path to the file to fix
            violations: List of violations to fix (all from this check)
            source: Original source code as string
            tree: Parsed AST tree of the file
            encoding: Encoding to write the file back with (matching what it
                was read as, so a PEP 263 declaration round-trips correctly)

        Returns:
            True if fixes were successfully applied, False otherwise
        """
        ...


def read_source_with_encoding(filepath: Path) -> tuple[str, str]:
    """Read a file's content, honoring a PEP 263 encoding declaration.

    Reads raw bytes and decodes them manually (rather than opening in text
    mode) so line endings are never touched — a CRLF file's decoded string
    keeps its literal "\\r\\n" sequences, which ast.parse and tokenize both
    tolerate. tokenize.detect_encoding also handles a leading UTF-8 BOM
    (returning "utf-8-sig").

    Args:
        filepath: Path to file

    Returns:
        (source, encoding), so a fix can write back in the same encoding

    Raises:
        OSError: if the file can't be read
        SyntaxError: if the PEP 263 encoding cookie itself is malformed
        UnicodeDecodeError: if the content isn't valid in the declared/
            detected encoding
        LookupError: if the declared encoding name is unknown
    """
    raw = filepath.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding), encoding


def find_ignored_lines(source: str, pattern: re.Pattern[str]) -> set[int]:
    """Extract line numbers that have an inline ignore comment matching `pattern`.

    Uses the tokenize module to accurately detect comments, so a string or
    byte literal that happens to contain matching text (e.g. a dict key)
    is never mistaken for a suppression directive.

    Args:
        source: Python source code as string
        pattern: Compiled regex identifying the ignore-comment marker

    Returns:
        Set of 1-indexed line numbers with a matching ignore comment
    """
    ignored: set[int] = set()

    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)

        for tok_type, tok_string, (line, _), _, _ in tokens:
            if tok_type != tokenize.COMMENT:
                continue

            if pattern.search(tok_string):
                ignored.add(line)
    except tokenize.TokenError as token_error:  # pragma: no cover
        # Defensive: source is already parsed by AST, so tokenizing it can't
        # realistically fail. If it ever does, treat it as no lines ignored.
        logger.debug(repr(token_error))

    return ignored
