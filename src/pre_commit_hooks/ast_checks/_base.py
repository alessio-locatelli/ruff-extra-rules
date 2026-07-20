"""Base protocols and data structures for AST-based checks."""

from __future__ import annotations

import ast
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

logger = logging.getLogger("ast_checks")


@dataclass
class Violation:
    check_id: str
    error_code: str
    line: int  # 1-indexed
    # 0-indexed *character* offset (or 0 when a check has no more specific
    # position than "this line") — not a byte offset. `ast.col_offset` is a
    # UTF-8 byte offset, so a check that reports one directly must first
    # convert it via `byte_col_to_char_col()`, the same way `forbid_vars` and
    # `redundant_assignment` already do; `misplaced_comment`'s own
    # `tokenize`-derived column is already a character offset. `main()`
    # reports this as a conventional 1-based column (`col + 1`).
    col: int
    message: str
    fixable: bool
    fix_data: dict[str, Any] | None = None


class ASTCheck(Protocol):
    """Interface for pluggable AST checks in the grouped linter.

    Each check is independent and stateless across files.
    """

    @property
    def check_id(self) -> str:
        """Kebab-case identifier for this check, e.g. "forbid-vars"."""
        ...

    @property
    def error_code(self) -> str:
        """Error code prefix for this check's violations, e.g. "TRI001"."""
        ...

    def get_prefilter_pattern(self) -> list[str] | None:
        """Fixed-string git-grep patterns that identify candidate files for this
        check, combined with OR logic (a file is a candidate if it contains ANY
        pattern), or None to check every file with no pre-filtering.

        Examples:
            - ["def get_"] for validate-function-name
            - ["super().__init__"] for redundant-super-init
            - ["data", "result"] for forbid-vars
            - None for excessive-blank-lines (check all files)
        """
        ...

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]: ...

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool:
        """`encoding` must match what `filepath` was originally read as, so a
        PEP 263 declaration round-trips correctly.

        A check with a single write per `fix()` call needs no special
        handling: let `FixValidationError` (raised by `atomic_write_text()`
        if the fix would produce invalid syntax) propagate uncaught —
        `CheckOrchestrator._apply_fixes` catches it and attributes the
        rejection to every violation passed in. A check that writes more
        than once per `fix()` call (looping over violations individually,
        like `validate_function_name`) should instead catch
        `FixValidationError` around each individual write and call
        `mark_fix_rejected()` on that specific violation, so a later write
        in the same call still gets attempted.

        `OSError` from `atomic_write_text()` (missing parent directory,
        permission denied, disk full) is different: every implementation
        must catch it itself and return `False`, matching this method's own
        "`True`/`False`, never raises" contract — `CheckOrchestrator`'s own
        outer `except Exception` only protects the full pipeline, not a
        caller that calls a check's `fix()` directly.
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
    def add_cli_arguments(cls, _parser: argparse.ArgumentParser) -> None:
        return

    @classmethod
    def cli_kwargs_from_args(cls, _args: argparse.Namespace) -> dict[str, Any]:
        return {}


def byte_col_to_char_col(line: str, byte_col: int) -> int:
    """Convert a UTF-8 byte offset within `line` to a character offset.

    CPython's AST column offsets (`col_offset`/`end_col_offset`) are UTF-8
    byte offsets, not character offsets. On a line containing any non-ASCII
    text before the target position, indexing or regex-matching `line`
    (a `str`, indexed by character) directly with the raw `col_offset`
    lands on the wrong character. Converting first keeps position-based
    fixes correct on such lines.
    """
    return len(line.encode("utf-8")[:byte_col].decode("utf-8"))


# Matches ast's own private _splitlines_no_ff: split only on \r\n / \n / \r
# (keeping the separator on each line), the same line boundaries the parser
# itself uses for lineno/end_lineno. Deliberately not reusing that private
# function directly (an implementation detail of the ast module, not a
# public contract) — this is a small, stable regex to own instead.
_AST_LINE_PATTERN = re.compile(r"(.*?(?:\r\n|\n|\r|$))")


def split_lines_like_ast(source: str) -> list[str]:
    """Split `source` into lines the same way `ast`'s own line numbers
    (`lineno`/`end_lineno`) are computed: only on `\\r\\n`/`\\n`/`\\r`.

    Deliberately not `source.splitlines()`: that also splits on form feed
    and several other Unicode line-separator characters (`\\x0b`, `\\x1c`
    -`\\x1e`, `\\x85`, `\\u2028`, `\\u2029`) that Python's own tokenizer
    treats as ordinary intra-line whitespace/content, not a line boundary —
    all legal, if unusual, inside otherwise ordinary Python source. Indexing
    into `source.splitlines()` by an AST line number can silently return a
    truncated line whenever the source contains one of those characters;
    indexing into this function's result never diverges from the AST's own
    line numbering.
    """
    return _AST_LINE_PATTERN.findall(source)


def fast_get_source_segment(source: str, ast_lines: list[str], node: ast.expr) -> str | None:
    """Equivalent to `ast.get_source_segment(source, node)` for a
    single-line node, without that stdlib function's own per-call cost.

    `ast.get_source_segment()` re-splits the *entire* `source` into lines
    on every call (see its implementation), which is fine for a handful of
    calls but turns a hot per-node loop — one call per assignment, across
    every assignment in a file — into O(nodes x source size) instead of
    O(source size) overall. `ast_lines` is computed once by the caller via
    `split_lines_like_ast()` and reused across every call.

    Falls back to the real `ast.get_source_segment` for a node spanning
    multiple lines: reconstructing a multi-line segment correctly needs
    each line's own newline still attached (which `split_lines_like_ast`'s
    lines already have, but the fallback is simplest — rare enough among
    the assignment/call RHS expressions this is used for that the fast
    path not covering it doesn't matter).

    Returns None if `node` is missing end-position info, mirroring
    `ast.get_source_segment`'s own contract.
    """
    if node.end_lineno is None or node.end_col_offset is None:
        return None
    if node.end_lineno != node.lineno:
        return ast.get_source_segment(source, node)
    line = ast_lines[node.lineno - 1]
    return line.encode()[node.col_offset : node.end_col_offset].decode()


def read_source_with_encoding(filepath: Path) -> tuple[str, str]:
    """Read a file's content, honoring a PEP 263 encoding declaration.

    Reads raw bytes and decodes them manually (rather than opening in text
    mode) so line endings are never touched — a CRLF file's decoded string
    keeps its literal "\\r\\n" sequences, which ast.parse and tokenize both
    tolerate. tokenize.detect_encoding also handles a leading UTF-8 BOM
    (returning "utf-8-sig").

    Returns (source, encoding) — the encoding is returned alongside the
    source so a fix can write the file back in the same encoding it was
    read in.

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


class FixValidationError(Exception):
    """Raised by `atomic_write_text()` when the content it was asked to
    write doesn't parse as valid Python — the check that produced it has a
    bug. `path` is left completely untouched: validation runs before the
    temp file is even created, so there's nothing to roll back.
    """

    def __init__(self, path: Path, syntax_error: SyntaxError) -> None:
        super().__init__(f"Fix for {path} would produce invalid syntax: {syntax_error}")
        self.path = path
        self.syntax_error = syntax_error


def atomic_write_text(path: Path, content: str, encoding: str) -> None:
    """Validate `content` parses as Python, then write it to `path` via
    temp-file-then-rename, atomic on POSIX.

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

    Every check's `fix()` ultimately writes through this one function, so
    validating here — rather than in each check — guarantees no fix, from
    any check, can ever leave a file syntactically broken on disk: a bad fix
    never gets far enough to create a temp file, let alone rename it into
    place.

    Raises:
        FixValidationError: if `content` isn't valid Python. Raised before
            any file I/O, so `path` still holds its prior content.
    """
    try:
        # compile(), not ast.parse(): some invalid code is only rejected at
        # compile time, not by the grammar alone — e.g. `return`/`yield`
        # outside a function, `break`/`continue` outside a loop, or a
        # `from __future__ import` that isn't the first statement. All
        # still raise SyntaxError, just later in the pipeline than parsing.
        compile(content, str(path), "exec")
    except SyntaxError as syntax_error:
        raise FixValidationError(path, syntax_error) from syntax_error

    real_path = path.resolve()
    fd, temp_name = tempfile.mkstemp(dir=real_path.parent, prefix=f".{real_path.name}.", suffix=".tmp")
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


def mark_fix_rejected(violation: Violation) -> None:
    """Record that a fix was attempted for `violation` but rejected by
    `atomic_write_text()` because it would have produced invalid syntax.
    Mirrors `mark_fixed()`/`is_fixed()`'s `fix_data["fixed"]` convention.
    """
    if violation.fix_data is None:
        violation.fix_data = {}
    violation.fix_data["fix_rejected"] = True


def is_fix_rejected(violation: Violation) -> bool:
    """Whether `mark_fix_rejected()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("fix_rejected", False))


def mark_fix_errored(violation: Violation) -> None:
    """Record that `fix()` itself raised an exception other than
    `FixValidationError` while attempting `violation` — a bug in the
    check's own fix logic, distinct from `mark_fix_rejected()` (fix() ran
    to completion but its *output* didn't parse). Mirrors
    `mark_fixed()`/`is_fixed()`'s `fix_data["fixed"]` convention.
    """
    if violation.fix_data is None:
        violation.fix_data = {}
    violation.fix_data["fix_errored"] = True


def is_fix_errored(violation: Violation) -> bool:
    """Whether `mark_fix_errored()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("fix_errored", False))


def mark_fix_failed(violation: Violation) -> None:
    """Record that `fix()` returned `False` (without raising) for
    `violation` because it caught an `OSError` while writing the file back —
    exactly the third outcome `ASTCheck.fix()`'s own docstring documents
    ("OSError from atomic_write_text() ... every implementation must catch
    it itself and return False"). Distinct from `mark_fix_errored()`: this
    is an environmental failure (disk full, permission denied, missing
    parent directory), not a bug in the check's own fix logic, so it must
    not carry the same "this is a bug, please report it" hint. Mirrors
    `mark_fixed()`/`is_fixed()`'s `fix_data["fixed"]` convention.
    """
    if violation.fix_data is None:
        violation.fix_data = {}
    violation.fix_data["fix_failed"] = True


def is_fix_failed(violation: Violation) -> bool:
    """Whether `mark_fix_failed()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("fix_failed", False))
