"""Base protocols and data structures for AST-based checks."""

from __future__ import annotations

import io
import logging
import os
import re
import stat
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import argparse
    import ast

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

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Register this check's own CLI arguments on the shared parser.

        Optional — a check with no check-specific configuration doesn't
        need to override this. Pair with `cli_kwargs_from_args()` to turn
        the parsed values into this check's own `__init__` kwargs.
        """
        ...

    @classmethod
    def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]:
        """Translate parsed CLI args into this check's own `__init__` kwargs.

        Optional, paired with `add_cli_arguments()`.
        """
        ...


class BaseCheck:
    """No-op defaults for ASTCheck's optional CLI-argument extension
    points, so a check with nothing check-specific doesn't have to repeat
    the override itself.
    """

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        return

    @classmethod
    def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]:
        return {}


def byte_col_to_char_col(line: str, byte_col: int) -> int:
    """Convert a UTF-8 byte offset within `line` to a character offset.

    CPython's AST column offsets (`col_offset`/`end_col_offset`) are UTF-8
    byte offsets, not character offsets. On a line containing any non-ASCII
    text before the target position, indexing or regex-matching `line`
    (a `str`, indexed by character) directly with the raw `col_offset`
    lands on the wrong character. Converting first keeps position-based
    fixes correct on such lines.

    Args:
        line: The source line the offset was recorded against
        byte_col: A UTF-8 byte offset into `line`

    Returns:
        The equivalent character offset into `line`
    """
    return len(line.encode("utf-8")[:byte_col].decode("utf-8"))


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


def atomic_write_text(path: Path, content: str, encoding: str) -> None:
    """Write `content` to `path` via temp-file-then-rename, atomic on POSIX.

    Mirrors `_cache.py`'s `_write_cache`, with three refinements needed for
    a source file rather than a cache blob: the write targets `path.resolve()`
    rather than `path` itself, so a symlinked file gets its target's content
    replaced in place rather than having the symlink itself overwritten with
    a plain file (`replace()`, unlike the `open()` a plain `write_text()`
    uses, acts on the symlink's own directory entry, not what it points to);
    the temp file gets a unique name from `tempfile.mkstemp` (a fixed
    `<name>.tmp` sibling could collide if two hook processes fix the same
    file at once); and its permission bits are copied from the resolved
    target before the rename (`mkstemp` creates files mode 0600, which would
    otherwise silently strip an executable script's +x bit). A plain
    `Path.write_text()` can also leave a truncated, invalid file on disk if
    the process is killed mid-write; writing to a temp file first and
    renaming it into place means the target always ends up either fully old
    or fully new.
    """
    real_path = path.resolve()
    fd, temp_name = tempfile.mkstemp(
        dir=real_path.parent, prefix=f".{real_path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as temp_file:
            temp_file.write(content)
        temp_path.chmod(stat.S_IMODE(real_path.stat().st_mode))
        temp_path.replace(real_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def ignore_pattern_for(error_code: str) -> re.Pattern[str]:
    """Compile the inline-ignore regex for a check's error code.

    Every check that supports `# pytriage: ignore=<code>` suppression
    compiled a near-identical pattern by hand; this is the single place that
    pattern is defined, so all checks agree on its syntax (case-insensitive,
    optional whitespace around `:`).
    """
    return re.compile(rf"#\s*pytriage:\s*ignore={re.escape(error_code)}", re.IGNORECASE)


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


def mark_fixed(violation: Violation) -> None:
    """The single place that writes the `fix_data["fixed"]` convention —
    previously three independent hand-written sites.
    """
    if violation.fix_data is None:
        violation.fix_data = {}
    violation.fix_data["fixed"] = True


def is_fixed(violation: Violation) -> bool:
    """Whether `mark_fixed()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("fixed", False))
