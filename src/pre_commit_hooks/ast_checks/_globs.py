from __future__ import annotations

import functools
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["InvalidGlobError", "anchored_pattern", "compile_glob", "glob_matches", "relative_to_anchor"]


class InvalidGlobError(ValueError):
    pass


class _Piece(NamedTuple):
    regex: str
    fixed_width: bool
    is_star: bool = False


_STAR = _Piece(".*", fixed_width=False, is_star=True)

_CLASS_MEMBERS_TO_ESCAPE = "^\\][&|~"

_MAX_ALTERNATIVES = 1024


@functools.cache
def compile_glob(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(_translate(pattern), re.DOTALL)
    except re.error as error:
        message = f"`{pattern}` could not be compiled: {error}"
        raise InvalidGlobError(message) from error


def glob_matches(pattern: str, candidate: str) -> bool:
    return compile_glob(pattern).fullmatch(candidate) is not None


@functools.cache
def anchored_pattern(pattern: str, anchor: Path) -> str:
    joined = pattern if pattern.startswith("/") else f"{_escaped(str(anchor))}/{pattern}"
    return "/" + "/".join(_resolved_parts(joined))


def _resolved_parts(path: str) -> list[str]:
    parts: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return parts


def _escaped(path: str) -> str:
    return "".join(f"\\{char}" if char in "\\*?[]{}," else char for char in path)


def relative_to_anchor(absolute: PurePosixPath, anchor: Path) -> PurePosixPath | None:
    """A file outside the anchor is never matched by that anchor's patterns;
    see `docs/adr/0046-exclude-glob-semantics.md`.
    """
    try:
        return absolute.relative_to(PurePosixPath(anchor))
    except ValueError:
        return None


def _translate(pattern: str) -> str:
    pieces: list[_Piece] = []
    index = 0
    depth = 0
    branches: list[int] = []
    alternatives = 1
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 1
            if index == len(pattern):
                message = f"`{pattern}` ends with an unfinished escape"
                raise InvalidGlobError(message)
            pieces.append(_Piece(re.escape(pattern[index]), fixed_width=True))
            index += 1
        elif char == "*":
            piece, index = _translate_stars(pattern, index)
            pieces.append(piece)
        elif char == "?":
            pieces.append(_Piece(".", fixed_width=True))
            index += 1
        elif char == "[":
            regex, index = _translate_class(pattern, index)
            pieces.append(_Piece(regex, fixed_width=True))
        elif char == "{":
            depth += 1
            branches.append(1)
            pieces.append(_Piece("(?:", fixed_width=False))
            index += 1
        elif char == "}":
            if not depth:
                message = f"`{pattern}` closes a `{{` that was never opened"
                raise InvalidGlobError(message)
            depth -= 1
            alternatives = min(alternatives * branches.pop(), _MAX_ALTERNATIVES + 1)
            pieces.append(_Piece(")", fixed_width=False))
            index += 1
        elif char == "," and depth:
            branches[-1] += 1
            pieces.append(_Piece("|", fixed_width=False))
            index += 1
        else:
            pieces.append(_Piece(re.escape(char), fixed_width=True))
            index += 1

    if depth:
        message = f"`{pattern}` leaves a `{{` unclosed"
        raise InvalidGlobError(message)
    if alternatives > _MAX_ALTERNATIVES:
        message = f"`{pattern}` spells out more than {_MAX_ALTERNATIVES} ways to match"
        raise InvalidGlobError(message)
    return _join(pieces)


def _join(pieces: list[_Piece]) -> str:
    """Spells each interior `<star><fixed-width run>` as an atomic group, the
    way `fnmatch.translate` does: nested greedy `.*` groups otherwise
    backtrack exponentially, so one pattern like `*a*a*a...b` takes seconds
    to reject a name it doesn't match. A run that isn't fixed-width can't
    take the atomic form -- committing to the first alternative of
    `*{ab,a}*b` would reject `ab`, which the pattern does match.
    """
    parts: list[str] = []
    index = 0
    while index < len(pieces) and not pieces[index].is_star:
        parts.append(pieces[index].regex)
        index += 1

    while index < len(pieces):
        index += 1
        run = []
        fixed_width = True
        while index < len(pieces) and not pieces[index].is_star:
            run.append(pieces[index].regex)
            fixed_width &= pieces[index].fixed_width
            index += 1
        joined = "".join(run)
        if fixed_width and index < len(pieces):
            parts.append(f"(?>.*?{joined})")
        else:
            # A trailing run is anchored by the end of the candidate itself,
            # and a variable-width one can't be committed to; either way,
            # leave the star free to give back what it consumed.
            parts.append(f".*{joined}")

    return "".join(parts)


def _translate_stars(pattern: str, index: int) -> tuple[_Piece, int]:
    """`*` spans path separators, so `**` differs from it only where it is a
    path component of its own: there it also matches zero components, making
    `a/**/b.py` match `a/b.py`. A longer run of stars is just that many `*`,
    so `a/***/b.py` still requires a component between the two.
    """
    end = index
    while end < len(pattern) and pattern[end] == "*":
        end += 1

    is_own_component = (
        end - index == 2 and (index == 0 or pattern[index - 1] == "/") and (end == len(pattern) or pattern[end] == "/")
    )
    if not is_own_component or end == len(pattern):
        return _STAR, end
    return _Piece("(?:.*/)?", fixed_width=False), end + 1


def _class_end(pattern: str, index: int) -> int:
    """Just past the `]` closing the class opening at `index`, or past the end
    of `pattern` when there is none.
    """
    cursor = index + 1
    if cursor < len(pattern) and pattern[cursor] in "!^":
        cursor += 1
    if cursor < len(pattern) and pattern[cursor] == "]":
        cursor += 1
    while cursor < len(pattern) and pattern[cursor] != "]":
        cursor += 1
    return cursor + 1


def _translate_class(pattern: str, index: int) -> tuple[str, int]:
    """A backslash is an ordinary member here rather than an escape, so `[a-\\z]`
    is the range `a` to `\\` — invalid, and rejected, exactly as in `ruff`.
    """
    end = _class_end(pattern, index)
    if end > len(pattern):
        message = f"`{pattern}` leaves a `[` unclosed"
        raise InvalidGlobError(message)

    body = pattern[index + 1 : end - 1]
    negated = body.startswith(("!", "^"))
    if negated:
        body = body[1:]
    members = "".join(f"\\{char}" if char in _CLASS_MEMBERS_TO_ESCAPE else char for char in body)
    return f"[{'^' if negated else ''}{members}]", end
