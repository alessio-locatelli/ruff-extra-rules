from __future__ import annotations

import re
import tokenize
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    SuppressionUsage,
    Violation,
    ignore_pattern_for,
    ignored_lines_and_classify_comments_and_pytriage_from_tokens,
    record_suppression_usage_if_ignored,
    tokenize_source,
)

if TYPE_CHECKING:
    import ast
    from pathlib import Path

    from ._base import PytriageComment

CHECK_ID = "suppression-rationale"
ERROR_CODE = "TR12"
IGNORE_PATTERN = ignore_pattern_for(ERROR_CODE)
_NOQA = re.compile(r"#+\s*(?:(?:ruff|flake8)\s*:\s*)?(?i:noqa)(?=$|\s|:|#)")
_NOQA_CODE = re.compile(r"[A-Z]+[0-9]+")
_NATIVE = re.compile(r"#\s*ruff\s*:\s*(file-ignore|ignore|disable|enable)\s*\[([^\]]*)\]")
_RANGE_FRAGMENT = re.compile(r"#\s*ruff\s*:\s*(?:disable|enable)")
_ISORT_SKIP = re.compile(r"#+\s*(?:ruff\s*:\s*)?isort: ?(skip_file|skip)")
_PYTRIAGE_CODE = re.compile(r"TR[0-9]+", re.IGNORECASE)

type _Action = Literal["line", "file", "file-ignore", "disable", "enable", "off", "on"]
_NATIVE_ACTIONS: dict[str, _Action] = {
    "file-ignore": "file-ignore",
    "ignore": "line",
    "disable": "disable",
    "enable": "enable",
}
_RANGE_TRIVIA = frozenset(
    {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
)


@dataclass(frozen=True, slots=True)
class _Directive:
    end: int
    action: _Action = "line"
    codes: tuple[str, ...] = ()


def _skip_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _noqa_directive(text: str) -> _Directive | None:
    prefix = _NOQA.match(text)
    if prefix is None:
        return None
    end = prefix.end()
    action: _Action = "file" if ":" in prefix.group() else "line"
    position = _skip_whitespace(text, end)
    if position == len(text) or text[position] != ":":
        return _Directive(end, action)

    position += 1
    while True:
        position = _skip_whitespace(text, position)
        code = _NOQA_CODE.match(text, position)
        if code is None:
            return None
        end = code.end()
        if end < len(text) and not (text[end].isspace() or text[end] in ",#"):
            return None
        position = _skip_whitespace(text, end)
        if position < len(text) and text[position] == ",":
            position += 1
            position = _skip_whitespace(text, position)
            if position == len(text) or text[position] == "#" or text.startswith("--", position):
                return _Directive(position, action)
        elif _NOQA_CODE.match(text, position) is None:
            return _Directive(end, action)


def _recognize(text: str, pytriage: PytriageComment | None) -> _Directive | None:
    if native := _NATIVE.match(text):
        codes = tuple(code.strip() for code in native[2].split(","))
        if not codes[-1]:
            codes = codes[:-1]
        if not codes or any(
            not code or not code[0].isalpha() or any(not (char.isalnum() or char in "_-:") for char in code)
            for code in codes
        ):
            return None
        return _Directive(native.end(), _NATIVE_ACTIONS[native[1]], codes)

    if noqa := _noqa_directive(text):
        return noqa

    if pytriage is not None:
        matches = tuple(ignore_pattern_for(code).search(text) for code in pytriage.codes)
        if all(_PYTRIAGE_CODE.fullmatch(code) for code in pytriage.codes) and all(matches):
            return _Directive(max(match.end() for match in matches if match is not None))
        return None

    if skip := _ISORT_SKIP.match(text):
        return _Directive(skip.end())
    if text.rstrip() in {"# isort: off", "# ruff: isort: off"}:
        return _Directive(len(text.rstrip()), "off")
    if text.rstrip() in {"# isort: on", "# ruff: isort: on"}:
        return _Directive(len(text.rstrip()), "on")
    return None


def _has_inline_rationale(text: str, directive: _Directive) -> bool:
    trailing = text[directive.end :].strip()
    if trailing.startswith("#"):
        trailing = trailing[1:].strip()
    elif trailing.startswith("--"):
        trailing = trailing[2:].strip()
    return bool(trailing)


def _preceding_rationale_lines(comments: tuple[tokenize.TokenInfo, ...], comment_only: set[int]) -> set[int]:
    explained: set[int] = set()
    previous_line = 0
    nonempty_block = False
    for token in comments:
        line = token.start[0]
        if line != previous_line + 1:
            nonempty_block = False
        if line in comment_only:
            nonempty_block |= bool(token.string.lstrip("#").strip())
            if nonempty_block:
                explained.add(line + 1)
        else:
            nonempty_block = False
        previous_line = line
    return explained


def _matched_range_starts(
    tokens: tuple[tokenize.TokenInfo, ...], directives: dict[int, _Directive], comment_only: set[int]
) -> set[int]:
    matched: set[int] = set()
    if not any(directive.action in {"disable", "off"} for directive in directives.values()):
        return matched
    indents = {"", *(token.string for token in tokens if token.type == tokenize.INDENT)}
    pending: tuple[tokenize.TokenInfo, _Directive] | None = None
    nested = False
    bracket_depth = 0
    for token in tokens:
        if token.type == tokenize.OP:
            bracket_depth += (token.string in "([{") - (token.string in ")]}")
        if pending is not None and token.type not in _RANGE_TRIVIA and token.start[1] < pending[0].start[1]:
            pending = None
        if token.type != tokenize.COMMENT:
            continue
        directive = directives.get(token.start[0])
        if directive is None:
            if _RANGE_FRAGMENT.search(token.string):
                pending = None
            continue
        if directive.action not in {"disable", "enable", "off", "on"}:
            continue
        indent = token.line[: token.start[1]]
        if token.start[0] not in comment_only or indent not in indents or bracket_depth:
            pending = None
            continue
        if directive.action in {"disable", "off"}:
            nested = pending is not None
            pending = token, directive
        else:
            if pending is not None and not nested:
                start, opening = pending
                if (
                    start.line[: start.start[1]] == indent
                    and opening.codes == directive.codes
                    and (opening.action, directive.action) in {("disable", "enable"), ("off", "on")}
                ):
                    matched.add(start.start[0])
            pending = None
            nested = False
    return matched


class SuppressionRationaleCheck(BaseCheck):
    __slots__ = ()

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    @property
    def default_enabled(self) -> bool:
        return False

    def get_prefilter_pattern(self) -> list[str] | None:
        return ["#"]

    def check(self, _filepath: Path, _tree: ast.Module, source: str) -> CheckResult:
        tokens = tuple(tokenize_source(source))
        ignored, comment_only, _trailing, format_suppressed, pytriage = (
            ignored_lines_and_classify_comments_and_pytriage_from_tokens(tokens, IGNORE_PATTERN)
        )
        comments = tuple(token for token in tokens if token.type == tokenize.COMMENT)
        pytriage_by_line = {comment.line: comment for comment in pytriage}
        directives = {
            token.start[0]: directive
            for token in comments
            if (directive := _recognize(token.string, pytriage_by_line.get(token.start[0]))) is not None
        }
        explained = _preceding_rationale_lines(comments, comment_only)
        matched = _matched_range_starts(tokens, directives, comment_only)
        violations: list[Violation] = []
        usages: list[SuppressionUsage] = []
        for token in comments:
            line = token.start[0]
            directive = directives.get(line)
            if (
                directive is None
                or directive.action in {"enable", "on"}
                or (directive.action in {"disable", "off"} and line not in matched)
                or (directive.action in {"file", "file-ignore"} and line not in comment_only)
                or (directive.action == "file-ignore" and token.start[1] != 0)
                or line in explained
                or _has_inline_rationale(token.string, directive)
            ):
                continue
            if record_suppression_usage_if_ignored(
                usages,
                pytriage,
                ignored_lines=ignored,
                format_suppressed=format_suppressed,
                check_id=CHECK_ID,
                error_code=ERROR_CODE,
                candidate_lines=(line,),
            ):
                continue
            violations.append(
                Violation(
                    check_id=CHECK_ID,
                    error_code=ERROR_CODE,
                    line=line,
                    col=token.start[1],
                    message="Suppression comment requires an explanation inline or in a preceding comment block.",
                    fixable=False,
                )
            )
        return CheckResult(violations, usages)

    def fix(
        self,
        _filepath: Path,
        violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.DECLINED)
