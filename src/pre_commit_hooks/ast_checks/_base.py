from __future__ import annotations

import ast
import io
import os
import re
import stat
import tempfile
import tokenize
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator

    from ._options import CheckOption


class FixOutcome(Enum):
    APPLIED = "applied"
    DECLINED = "declined"
    REJECTED = "rejected"
    ABORTED = "aborted"
    ERRORED = "errored"
    FAILED = "failed"
    RESOLVED_INDIRECTLY = "resolved_indirectly"


@dataclass(frozen=True, slots=True)
class FixResult:
    outcomes: tuple[FixOutcome, ...]

    @classmethod
    def for_violations(cls, violations: list[Violation], outcome: FixOutcome) -> FixResult:
        return cls((outcome,) * len(violations))


@dataclass(slots=True)
class Violation:
    check_id: str
    error_code: str
    line: int
    col: int
    message: str
    fixable: bool
    fix_data: dict[str, Any] | None = None
    fix_outcome: FixOutcome | None = None


@dataclass(frozen=True, slots=True)
class SuppressionUsage:
    check_id: str
    error_code: str
    line: int


class CheckResult(list[Violation]):
    __slots__ = ("suppression_usages",)

    def __init__(
        self,
        violations: Iterable[Violation] = (),
        suppression_usages: Iterable[SuppressionUsage] = (),
    ) -> None:
        super().__init__(violations)
        self.suppression_usages = tuple(suppression_usages)  # pytriage: TR6


@dataclass(frozen=True, slots=True)
class PytriageComment:
    line: int
    col: int
    codes: tuple[str, ...]


class ASTCheck(Protocol):
    @property
    def check_id(self) -> str: ...

    @property
    def error_code(self) -> str: ...

    @property
    def default_enabled(self) -> bool: ...

    @property
    def cacheable(self) -> bool: ...

    @property
    def tracks_direct_inputs(self) -> bool: ...

    def record_direct_input(self, filepath: Path, source: str) -> None: ...

    def reconcile_direct_inputs(self, direct_inputs: list[Path]) -> list[Path]: ...

    def get_prefilter_pattern(self) -> list[str] | None: ...

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]: ...

    def check_with_suppression_tracking(self, filepath: Path, tree: ast.Module, source: str) -> CheckResult: ...

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> FixResult: ...

    OPTIONS: ClassVar[tuple[CheckOption, ...]]


class BaseCheck:
    __slots__ = ()

    OPTIONS: ClassVar[tuple[CheckOption, ...]] = ()

    @property
    def cacheable(self) -> bool:
        return True

    @property
    def default_enabled(self) -> bool:
        return True

    @property
    def tracks_direct_inputs(self) -> bool:
        return False

    def record_direct_input(self, _filepath: Path, _source: str) -> None:
        return

    def reconcile_direct_inputs(self, _direct_inputs: list[Path]) -> list[Path]:
        return []

    def check(self, _filepath: Path, _tree: ast.Module, _source: str) -> list[Violation]:
        raise NotImplementedError

    def check_with_suppression_tracking(self, filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        check_result = self.check(filepath, tree, source)
        return check_result if isinstance(check_result, CheckResult) else CheckResult(check_result)


class CheckUnavailableError(Exception):
    pass


def byte_col_to_char_col(line: str, byte_col: int) -> int:
    return len(line.encode("utf-8")[:byte_col].decode("utf-8"))


_AST_LINE_PATTERN = re.compile(r"(.*?(?:\r\n|\n|\r|$))")


def split_lines_like_ast(source: str) -> list[str]:
    return _AST_LINE_PATTERN.findall(source)


def line_terminator(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


_LONE_CR_PATTERN = re.compile(r"\r(?!\n)")


def normalize_for_tokenize(source: str) -> str:
    return _LONE_CR_PATTERN.sub("\n", source)


def fast_get_source_segment(source: str, ast_lines: list[str], node: ast.expr) -> str | None:
    if node.end_lineno is None or node.end_col_offset is None:
        return None
    if node.end_lineno != node.lineno:
        return ast.get_source_segment(source, node)
    line = ast_lines[node.lineno - 1]
    return line.encode()[node.col_offset : node.end_col_offset].decode()


def read_source_with_encoding(filepath: Path) -> tuple[str, str]:
    raw = filepath.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding), encoding


class FixValidationError(Exception):
    def __init__(self, path: Path, syntax_error: SyntaxError) -> None:
        super().__init__(f"Fix for {path} would produce invalid syntax: {syntax_error}")
        self.path = path
        self.syntax_error = syntax_error


class ConcurrentModificationError(Exception):
    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} changed on disk after it was read for this fix; the fix was discarded")
        self.path = path


def atomic_write_text(path: Path, content: str, encoding: str, expected_source: str) -> None:
    try:
        compile(content, path, "exec")
    except SyntaxError as syntax_error:
        raise FixValidationError(path, syntax_error) from syntax_error

    real_path = path.resolve()
    fd, temp_name = tempfile.mkstemp(dir=real_path.parent, prefix=f".{real_path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as temp_file:
            temp_file.write(content)
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
    escaped = re.escape(error_code)
    return re.compile(rf"#\s*pytriage:\s*(?:[^,\s]+\s*,\s*)*{escaped}(?!\w)", re.IGNORECASE)


_PYTRIAGE_COMMENT_PATTERN = re.compile(r"#\s*pytriage\s*:\s*(.*)", re.IGNORECASE)


def _parse_pytriage_comment(tok: tokenize.TokenInfo) -> PytriageComment | None:
    match = _PYTRIAGE_COMMENT_PATTERN.search(tok.string)
    if match is None:
        return None
    codes = tuple(
        code.upper()
        for segment in match.group(1).split(",")
        if (code := segment.strip().split(maxsplit=1)[0] if segment.strip() else "")
    )
    if not codes:
        return None
    return PytriageComment(line=tok.start[0], col=tok.start[1], codes=codes)


_FMT_OFF_PATTERN = re.compile(r"#\s*fmt:\s*off")
_FMT_ON_PATTERN = re.compile(r"#\s*fmt:\s*on")
_YAPF_DISABLE_PATTERN = re.compile(r"#\s*yapf:\s*disable")
_YAPF_ENABLE_PATTERN = re.compile(r"#\s*yapf:\s*enable")
_FMT_SKIP_SEGMENT_PATTERN = re.compile(r"fmt:\s*skip")


def _is_fmt_skip_comment(comment_text: str) -> bool:
    return any(_FMT_SKIP_SEGMENT_PATTERN.fullmatch(segment.strip()) for segment in comment_text.split("#"))


_LOGICAL_LINE_BOUNDARY_TOKEN_TYPES = frozenset(
    {tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER}
)


class _FormatSuppressionScanner:
    __slots__ = ("_last_line", "_logical_start", "_pending_skip", "_suppressed_from")

    def __init__(self) -> None:
        self._suppressed_from: int | None = None
        self._logical_start: int | None = None
        self._pending_skip = False
        self._last_line = 0

    def observe(self, tok: tokenize.TokenInfo) -> set[int] | None:
        if tok.type not in (tokenize.ENDMARKER, tokenize.DEDENT):
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


def _scan_token_stream(
    tokens: Iterable[tokenize.TokenInfo], *patterns: re.Pattern[str]
) -> tuple[set[int], set[int], set[int], set[int], tuple[PytriageComment, ...]]:
    ignored: set[int] = set()
    format_suppressed: set[int] = set()
    comment_lines: set[int] = set()
    code_lines: set[int] = set()
    comments: list[PytriageComment] = []
    scanner = _FormatSuppressionScanner()

    for tok in tokens:
        newly_ignored = scanner.observe(tok)
        if newly_ignored:
            ignored |= newly_ignored
            format_suppressed |= newly_ignored
        if tok.type == tokenize.COMMENT:
            comment_lines.add(tok.start[0])
            parsed = _parse_pytriage_comment(tok)
            if parsed is not None:
                comments.append(parsed)
            if any(pattern.search(tok.string) for pattern in patterns):
                ignored.add(tok.start[0])
        elif tok.type not in _NON_CODE_TOKEN_TYPES:
            code_lines.update(range(tok.start[0], tok.end[0] + 1))

    finalized = scanner.finalize()
    ignored |= finalized
    format_suppressed |= finalized
    return ignored, comment_lines - code_lines, comment_lines & code_lines, format_suppressed, tuple(comments)


def ignored_lines_and_pytriage_comments_from_tokens(
    tokens: Iterable[tokenize.TokenInfo], *patterns: re.Pattern[str]
) -> tuple[set[int], set[int], tuple[PytriageComment, ...]]:
    ignored, _comment_only, _trailing, format_suppressed, comments = _scan_token_stream(tokens, *patterns)
    return ignored, format_suppressed, comments


def find_ignored_lines_and_pytriage_comments(
    source: str, *patterns: re.Pattern[str]
) -> tuple[set[int], set[int], tuple[PytriageComment, ...]]:
    return ignored_lines_and_pytriage_comments_from_tokens(tokenize_source(source), *patterns)


def find_ignored_lines_and_classify_comments_and_pytriage(
    source: str, *patterns: re.Pattern[str]
) -> tuple[set[int], set[int], set[int], set[int], tuple[PytriageComment, ...]]:
    return _scan_token_stream(tokenize_source(source), *patterns)


def find_suppression_usage(
    comments: Iterable[PytriageComment],
    format_suppressed: set[int],
    check_id: str,
    error_code: str,
    candidate_lines: Collection[int],
) -> SuppressionUsage | None:
    normalized_code = error_code.upper()
    for comment in comments:
        if comment.line in format_suppressed:
            continue
        if comment.line in candidate_lines and normalized_code in comment.codes:
            return SuppressionUsage(check_id=check_id, error_code=normalized_code, line=comment.line)
    return None


def record_suppression_usage_if_ignored(
    suppression_usages: list[SuppressionUsage],
    comments: Iterable[PytriageComment],
    *,
    ignored_lines: set[int],
    format_suppressed: set[int],
    check_id: str,
    error_code: str,
    candidate_lines: Collection[int],
) -> bool:
    if not any(line in ignored_lines for line in candidate_lines):
        return False
    non_format_candidate_lines = tuple(line for line in candidate_lines if line not in format_suppressed)
    if len(non_format_candidate_lines) != len(candidate_lines):
        return True
    usage = find_suppression_usage(comments, format_suppressed, check_id, error_code, non_format_candidate_lines)
    if usage is not None:
        suppression_usages.append(usage)
    return True


def tokenize_source(source: str) -> Iterator[tokenize.TokenInfo]:
    return tokenize.generate_tokens(io.StringIO(normalize_for_tokenize(source)).readline)


def ignored_lines_from_tokens(tokens: Iterable[tokenize.TokenInfo], *patterns: re.Pattern[str]) -> set[int]:
    ignored, _format_suppressed, _comments = ignored_lines_and_pytriage_comments_from_tokens(tokens, *patterns)
    return ignored


def find_ignored_lines(source: str, *patterns: re.Pattern[str]) -> set[int]:
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
    comment_lines: set[int] = set()
    code_lines: set[int] = set()

    for tok_type, _tok_string, start, end, _ in tokenize_source(source):
        if tok_type == tokenize.COMMENT:
            comment_lines.add(start[0])
        elif tok_type not in _NON_CODE_TOKEN_TYPES:
            code_lines.update(range(start[0], end[0] + 1))

    return comment_lines - code_lines, comment_lines & code_lines


def find_ignored_lines_and_classify_comments(
    source: str, *patterns: re.Pattern[str]
) -> tuple[set[int], set[int], set[int]]:
    ignored, comment_only, trailing, _format_suppressed, _comments = _scan_token_stream(
        tokenize_source(source), *patterns
    )
    return ignored, comment_only, trailing
