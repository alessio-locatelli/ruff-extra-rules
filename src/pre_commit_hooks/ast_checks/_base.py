"""Base protocols and data structures for AST-based checks."""

from __future__ import annotations

import ast
import io
import os
import re
import stat
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from ._options import CheckOption


@dataclass(slots=True)
class Violation:
    check_id: str
    error_code: str
    line: int  # 1-indexed
    # 0-indexed *character* offset (or 0 when a check has no more specific
    # position than "this line") — not a byte offset. `ast.col_offset` is a
    # UTF-8 byte offset, so a check that reports one directly must first
    # convert it via `byte_col_to_char_col()`, the same way `meaningless_vars` and
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
        """Kebab-case identifier for this check, e.g. "meaningless-vars"."""
        ...

    @property
    def error_code(self) -> str:
        """Error code prefix for this check's violations, e.g. "TR1"."""
        ...

    @property
    def cacheable(self) -> bool:
        """Whether `CheckOrchestrator` may store/reuse this check's own
        violations in the shared per-file cache. `True` for almost every
        check (`BaseCheck`'s own default) — only a check whose result for
        one file can depend on another file's current content (e.g. a type
        checker resolving a cross-file import) must override this to
        `False`, since a cache hit keyed on this file's own content hash
        alone couldn't tell that the *other* file changed. See
        `docs/adr/0034-cacheable-check-flag-and-always-rerun-orchestrator-split.md`.
        """
        ...

    @property
    def tracks_direct_inputs(self) -> bool:
        """Whether `record_direct_input` below actually does something, so
        `CheckOrchestrator` still feeds this check a file it has been
        switched off for by `per-file-ignores`: suppressing a *report* in one
        file must not also withhold that file's content from the cross-file
        analysis another file's report depends on. See
        `docs/adr/0049-per-file-ignores.md` and
        `docs/adr/0041-persistent-ty-daemon-for-cross-file-reanalysis.md`.
        """
        ...

    def record_direct_input(self, filepath: Path, source: str) -> None: ...

    def reconcile_direct_inputs(self, direct_inputs: list[Path]) -> list[Path]: ...

    def get_prefilter_pattern(self) -> list[str] | None:
        """Fixed-string git-grep patterns that identify candidate files for this
        check, combined with OR logic (a file is a candidate if it contains ANY
        pattern), or None to check every file with no pre-filtering.

        Examples:
            - ["def get_"] for validate-function-name
            - ["super().__init__"] for redundant-super-init
            - ["data", "result"] for meaningless-vars
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

        `ConcurrentModificationError` (also raised by `atomic_write_text()`)
        follows the same split as `FixValidationError` above: propagate
        uncaught for a single-write `fix()`, or catch it around each
        individual write and call `mark_fix_aborted()` on that specific
        violation for a multi-write one. See `docs/adr/0042-abort-fixes-on-concurrent-source-modification.md`.

        `OSError` from `atomic_write_text()` (missing parent directory,
        permission denied, disk full) is different: every implementation
        must catch it itself and return `False`, matching this method's own
        "`True`/`False`, never raises" contract — `CheckOrchestrator`'s own
        outer `except Exception` only protects the full pipeline, not a
        caller that calls a check's `fix()` directly.
        """
        ...

    OPTIONS: ClassVar[tuple[CheckOption, ...]]
    """This check's own configurable options, each named by the `__init__`
    keyword it supplies. Empty for a check with nothing to configure.
    """


class BaseCheck:
    """No-op defaults for ASTCheck's optional extension points, so a check
    with nothing check-specific doesn't have to repeat the override
    itself, plus the `cacheable=True` default every check except a
    cross-file one (see `ASTCheck.cacheable`) wants.
    """

    __slots__ = ()

    OPTIONS: ClassVar[tuple[CheckOption, ...]] = ()

    @property
    def cacheable(self) -> bool:
        return True

    @property
    def tracks_direct_inputs(self) -> bool:
        return False

    def record_direct_input(self, _filepath: Path, _source: str) -> None:
        return

    def reconcile_direct_inputs(self, _direct_inputs: list[Path]) -> list[Path]:
        return []


class CheckUnavailableError(Exception):
    """Raised by `check()` when the check cannot function at all in the
    current environment — e.g. a required external tool is missing, or
    present but failing its own compatibility self-test — as opposed to an
    ordinary bug in the check's own logic.

    `CheckOrchestrator` deliberately does not swallow this into a per-file
    `rule_failures` entry the way it does for every other exception a
    check's `check()` can raise: a missing prerequisite affects every file
    identically, so reporting it once per file would just spam the same
    diagnosis N times instead of stating it clearly once. Instead, the
    orchestrator records `str(self)` once (see `unavailable_checks`) and
    disables that specific check for the rest of the run — every other
    check, and this check's own already-collected results for files it
    examined before this was raised, are unaffected. A check being
    enabled by default (as every check in `ALL_CHECKS` is, absent
    `--select`) must never let one missing prerequisite take down every
    unrelated check's results for a consumer who hasn't installed it.
    """


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


def line_terminator(line: str) -> str:
    """Return whichever of `\\r\\n`/`\\n`/`\\r` `line` (from
    `splitlines(keepends=True)`) ends with, or `""` if it has none (the
    file's last line, when the file doesn't end in a newline).

    A fixer that reconstructs a line's text from scratch (rather than
    slicing the original string, which naturally keeps its own trailing
    terminator attached) must reuse this instead of hardcoding `"\\n"`, or a
    CRLF file gets silently mixed line endings on exactly the lines the fix
    touched.
    """
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


_LONE_CR_PATTERN = re.compile(r"\r(?!\n)")


def normalize_for_tokenize(source: str) -> str:
    """Replace a bare `\\r` line ending with `\\n` before handing `source` to
    `tokenize`.

    Unlike `ast.parse()`/`split_lines_like_ast()` (which, like Python's own
    grammar, treat a lone `\\r` as a line boundary), `io.StringIO.readline()`
    does not — it only splits on `\\n` — so `tokenize.generate_tokens()` over
    an old-Mac-style CR-only file sees the whole file as one giant physical
    line and never emits a `COMMENT` token on the lines that actually have
    one. Every call site in this codebase that tokenizes source for comment
    detection must normalize through this first, or a real trailing comment
    on such a file goes undetected — the same failure mode a naive
    string-based `#` scan has, just from a different cause. Only ever
    replaces a bare `\\r` (never one that's part of `\\r\\n`), so it can't
    change the line count/line numbering `tokenize` reports.
    """
    return _LONE_CR_PATTERN.sub("\n", source)


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


class ConcurrentModificationError(Exception):
    """Raised by `atomic_write_text()` when `path`'s current on-disk content
    no longer decodes to `expected_source` — something modified the file
    after it was read for this fix. `path` itself is left untouched; only
    the already-written temp file is cleaned up. See
    `docs/adr/0042-abort-fixes-on-concurrent-source-modification.md`.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} changed on disk after it was read for this fix; the fix was discarded")
        self.path = path


def atomic_write_text(path: Path, content: str, encoding: str, expected_source: str) -> None:
    """Writes `content` to `path` via temp-file-then-rename, atomic on
    POSIX, after validating it parses as Python and that `path` still
    matches `expected_source`. Mirrors `_cache.py`'s `_write_cache`. See
    `docs/adr/0010-fix-validation-before-write.md` and
    `docs/adr/0042-abort-fixes-on-concurrent-source-modification.md`.
    """
    try:
        # compile(), not ast.parse(): some invalid code is only rejected at
        # compile time, not by the grammar alone — e.g. `return`/`yield`
        # outside a function, `break`/`continue` outside a loop, or a
        # `from __future__ import` that isn't the first statement. All
        # still raise SyntaxError, just later in the pipeline than parsing.
        compile(content, path, "exec")
    except SyntaxError as syntax_error:
        raise FixValidationError(path, syntax_error) from syntax_error

    real_path = path.resolve()
    fd, temp_name = tempfile.mkstemp(dir=real_path.parent, prefix=f".{real_path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as temp_file:
            temp_file.write(content)
        # Compare decoded text, not raw bytes -- see ADR-0042.
        try:
            current_source = real_path.read_bytes().decode(encoding)
        except UnicodeDecodeError:
            raise ConcurrentModificationError(path) from None
        if current_source != expected_source:
            raise ConcurrentModificationError(path)
        temp_path.chmod(stat.S_IMODE(real_path.stat().st_mode))
        temp_path.replace(real_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def ignore_pattern_for(error_code: str) -> re.Pattern[str]:
    """Compile the inline-suppression regex for a check's error code.

    Matches `# pytriage: TR1` alone or as one entry in a comma-separated
    list (`# pytriage: TR1,TR5`), in any position. The trailing `(?!\\w)`
    stops a short code from matching inside a longer one that starts with
    the same digits (`TR1` inside `TR10`).
    """
    escaped = re.escape(error_code)
    return re.compile(rf"#\s*pytriage:\s*(?:[^,\s]+\s*,\s*)*{escaped}(?!\w)", re.IGNORECASE)


_FMT_OFF_PATTERN = re.compile(r"#\s*fmt:\s*off")
_FMT_ON_PATTERN = re.compile(r"#\s*fmt:\s*on")
_YAPF_DISABLE_PATTERN = re.compile(r"#\s*yapf:\s*disable")
_YAPF_ENABLE_PATTERN = re.compile(r"#\s*yapf:\s*enable")
# See docs/adr/0050-format-suppression-pragmas.md.
_FMT_SKIP_SEGMENT_PATTERN = re.compile(r"fmt:\s*skip")


def _is_fmt_skip_comment(comment_text: str) -> bool:
    return any(_FMT_SKIP_SEGMENT_PATTERN.fullmatch(segment.strip()) for segment in comment_text.split("#"))


# See docs/adr/0050-format-suppression-pragmas.md.
_LOGICAL_LINE_BOUNDARY_TOKEN_TYPES = frozenset(
    {tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER}
)


class _FormatSuppressionScanner:
    """See `docs/adr/0050-format-suppression-pragmas.md`.

    `observe()` returns `None`, not an empty `set()`, when a token
    suppresses nothing new -- the common case on every token. Call
    `finalize()` once after the last token.
    """

    __slots__ = ("_last_line", "_logical_start", "_pending_skip", "_suppressed_from")

    def __init__(self) -> None:
        self._suppressed_from: int | None = None
        self._logical_start: int | None = None
        self._pending_skip = False
        self._last_line = 0

    def observe(self, tok: tokenize.TokenInfo) -> set[int] | None:
        if tok.type not in (tokenize.ENDMARKER, tokenize.DEDENT):
            # ENDMARKER's and DEDENT's own reported line is one past the
            # file's last physical line when the source ends in a newline
            # (DEDENT too, whenever the last real content is inside an
            # indented suite) -- both excluded so an unterminated fmt:off's
            # finalize() below doesn't suppress a phantom line past the
            # file's real content.
            self._last_line = tok.end[0]

        if tok.type == tokenize.COMMENT:
            return self._observe_comment(tok)
        if tok.type == tokenize.NEWLINE:
            return self._observe_newline(tok)
        if tok.type not in _LOGICAL_LINE_BOUNDARY_TOKEN_TYPES and self._logical_start is None:
            self._logical_start = tok.start[0]
        return None

    def _observe_comment(self, tok: tokenize.TokenInfo) -> set[int] | None:
        if self._logical_start is not None:
            if _is_fmt_skip_comment(tok.string):
                self._pending_skip = True
            return None

        stripped = tok.string.rstrip()
        if self._suppressed_from is None:
            if _FMT_OFF_PATTERN.fullmatch(stripped) or _YAPF_DISABLE_PATTERN.fullmatch(stripped):
                self._suppressed_from = tok.start[0]
        elif _FMT_ON_PATTERN.fullmatch(stripped) or _YAPF_ENABLE_PATTERN.fullmatch(stripped):
            suppressed = set(range(self._suppressed_from, tok.start[0] + 1))
            self._suppressed_from = None
            return suppressed
        return None

    def _observe_newline(self, tok: tokenize.TokenInfo) -> set[int] | None:
        suppressed: set[int] | None = None
        if self._pending_skip and self._logical_start is not None:
            suppressed = set(range(self._logical_start, tok.start[0] + 1))
        self._logical_start = None
        self._pending_skip = False
        return suppressed

    def finalize(self) -> set[int]:
        if self._suppressed_from is None:
            return set()
        return set(range(self._suppressed_from, self._last_line + 1))


def tokenize_source(source: str) -> Iterator[tokenize.TokenInfo]:
    """Tokenize `source`, normalizing line endings first (see
    `normalize_for_tokenize`) — the one `tokenize`-invocation boilerplate
    every tokenize-based helper in this module shares.
    """
    return tokenize.generate_tokens(io.StringIO(normalize_for_tokenize(source)).readline)


def ignored_lines_from_tokens(tokens: Iterable[tokenize.TokenInfo], *patterns: re.Pattern[str]) -> set[int]:
    """Extract line numbers with a comment matching any of `patterns` from an
    already-tokenized stream, plus every line suppressed by a ruff/Black-style
    `# fmt: off`/`# fmt: on`/`# fmt: skip` pragma (or YAPF's `# yapf:
    disable`/`# yapf: enable` equivalents) -- see `_FormatSuppressionScanner`
    and `docs/adr/0050-format-suppression-pragmas.md`. Distinct from this
    project's own `# pytriage: <code>` inline ignore comment (see
    `ignore_pattern_for`), which is what `patterns` matches; a caller passes
    its own error-code pattern there, but gets format suppression for free.

    Shared building block for `find_ignored_lines` (which tokenizes `source`
    itself) and a caller that already has its own token stream in hand (e.g.
    `misplaced_comment`, which would otherwise tokenize the same source a
    second time just to check for suppression comments).
    """
    ignored: set[int] = set()
    scanner = _FormatSuppressionScanner()
    for tok in tokens:
        newly_ignored = scanner.observe(tok)
        if newly_ignored:
            ignored |= newly_ignored
        if tok.type == tokenize.COMMENT and any(p.search(tok.string) for p in patterns):
            ignored.add(tok.start[0])
    ignored |= scanner.finalize()
    return ignored


def find_ignored_lines(source: str, *patterns: re.Pattern[str]) -> set[int]:
    """Extract line numbers that have an inline ignore comment matching any
    of `patterns`, plus every line a ruff/Black-style format-suppression
    pragma covers -- see `ignored_lines_from_tokens`, which this delegates to.

    Uses the tokenize module to accurately detect comments, so a string or
    byte literal that happens to contain matching text (e.g. a dict key)
    is never mistaken for a suppression directive. Accepts more than one
    pattern so a caller checking several suppression forms against the same
    source (e.g. redundant-type-conversion's own pragma plus a third-party
    type-checker's `# type: ignore`) tokenizes it only once.
    """
    return ignored_lines_from_tokens(tokenize_source(source), *patterns)


_NON_CODE_TOKEN_TYPES = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)


def classify_comment_lines(source: str) -> tuple[set[int], set[int]]:
    """Classify every 1-indexed line containing a `#` comment via
    `tokenize`, into (comment_only_lines, trailing_comment_lines).

    A naive single-quote-at-a-time text scan for `#` outside string
    literals (checking only whether the immediately preceding character is
    a backslash) misparses a line whose string content has an odd number of
    the delimiter's own quote character before the comment — e.g. a string
    ending in an escaped backslash (`"\\\\"  # comment`, where the escape
    check wrongly treats the closing quote as itself escaped) or a
    triple-quoted string with an embedded single quote — silently missing a
    real trailing comment. `tokenize` parses the same lexical grammar
    Python itself uses, so it can't be fooled the same way.
    """
    comment_lines: set[int] = set()
    code_lines: set[int] = set()

    for tok_type, _tok_string, start, end, _ in tokenize_source(source):
        if tok_type == tokenize.COMMENT:
            comment_lines.add(start[0])
        elif tok_type not in _NON_CODE_TOKEN_TYPES:
            # A multiline token (a triple-quoted string spanning
            # several lines) reports only its *start* line in
            # start[0] — every line up to and including end[0] (e.g.
            # the closing line, which can carry its own trailing
            # comment) is just as much "code" as the first.
            code_lines.update(range(start[0], end[0] + 1))

    return comment_lines - code_lines, comment_lines & code_lines


def find_ignored_lines_and_classify_comments(
    source: str, *patterns: re.Pattern[str]
) -> tuple[set[int], set[int], set[int]]:
    """Tokenize `source` exactly once, returning (ignored_lines,
    comment_only_lines, trailing_comment_lines) — the same results
    `find_ignored_lines`/`classify_comment_lines` would each compute from
    their own separate tokenize pass, fused into one. `ignored_lines` also
    includes every line suppressed by a ruff/Black-style format-suppression
    pragma, same as `ignored_lines_from_tokens` — see that function's own
    docstring and `docs/adr/0050-format-suppression-pragmas.md`.

    `RedundantAssignmentCheck.check()` needs both, and streams this single
    combined pass over `tokenize_source`'s lazy `Iterator` (rather than
    materializing the whole token stream once and feeding it to each
    helper) so a large file's token count never inflates peak memory — only
    the resulting line-number sets are retained, one token at a time.
    """
    ignored_lines: set[int] = set()
    comment_lines: set[int] = set()
    code_lines: set[int] = set()
    scanner = _FormatSuppressionScanner()

    for tok in tokenize_source(source):
        newly_ignored = scanner.observe(tok)
        if newly_ignored:
            ignored_lines |= newly_ignored
        if tok.type == tokenize.COMMENT:
            comment_lines.add(tok.start[0])
            if any(p.search(tok.string) for p in patterns):
                ignored_lines.add(tok.start[0])
        elif tok.type not in _NON_CODE_TOKEN_TYPES:
            code_lines.update(range(tok.start[0], tok.end[0] + 1))

    ignored_lines |= scanner.finalize()
    return ignored_lines, comment_lines - code_lines, comment_lines & code_lines


def _mark(violation: Violation, outcome: str) -> None:
    """The single place that writes the `fix_data` outcome convention every
    `mark_*()` below shares.
    """
    if violation.fix_data is None:
        violation.fix_data = {}
    violation.fix_data[outcome] = True


def mark_fixed(violation: Violation) -> None:
    """Record that this check's own `fix()` resolved `violation`."""
    _mark(violation, "fixed")


def is_fixed(violation: Violation) -> bool:
    """Whether `mark_fixed()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("fixed", False))


def mark_resolved_indirectly(violation: Violation) -> None:
    """Record that another check's fix in the same run removed `violation`
    as a side effect. Distinct from `mark_fixed()`: no fix of this
    violation's own check ever resolved it. See
    `docs/adr/0053-indirect-resolution-outcome.md`.
    """
    _mark(violation, "resolved_indirectly")


def is_resolved_indirectly(violation: Violation) -> bool:
    """Whether `mark_resolved_indirectly()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("resolved_indirectly", False))


def mark_fix_rejected(violation: Violation) -> None:
    """Record that a fix was attempted for `violation` but rejected by
    `atomic_write_text()` because it would have produced invalid syntax.
    """
    _mark(violation, "fix_rejected")


def is_fix_rejected(violation: Violation) -> bool:
    """Whether `mark_fix_rejected()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("fix_rejected", False))


def mark_fix_aborted(violation: Violation) -> None:
    """Record that a fix was attempted for `violation` but discarded by
    `atomic_write_text()` because the file changed on disk after it was read
    for this fix — an external edit or a concurrent process outside this
    tool's own per-file fix lock (see `ConcurrentModificationError`).
    """
    _mark(violation, "fix_aborted")


def is_fix_aborted(violation: Violation) -> bool:
    """Whether `mark_fix_aborted()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("fix_aborted", False))


def mark_fix_errored(violation: Violation) -> None:
    """Record that `fix()` itself raised an exception other than
    `FixValidationError` while attempting `violation` — a bug in the
    check's own fix logic, distinct from `mark_fix_rejected()` (fix() ran
    to completion but its *output* didn't parse).
    """
    _mark(violation, "fix_errored")


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
    not carry the same "this is a bug, please report it" hint.
    """
    _mark(violation, "fix_failed")


def is_fix_failed(violation: Violation) -> bool:
    """Whether `mark_fix_failed()` has already been called on `violation`."""
    return bool(violation.fix_data and violation.fix_data.get("fix_failed", False))
