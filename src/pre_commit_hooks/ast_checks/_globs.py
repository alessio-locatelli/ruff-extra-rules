"""Glob matching in `ruff`'s own dialect, plus the anchoring every pattern
source shares.

See `docs/adr/0049-per-file-ignores.md` and
`docs/adr/0046-exclude-glob-semantics.md`.
"""

from __future__ import annotations

import functools
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["InvalidGlobError", "anchored_patterns", "compile_glob", "glob_matches", "relative_to_anchor"]


class InvalidGlobError(ValueError):
    """Raised for a pattern `ruff` itself would refuse to compile."""


class _Piece(NamedTuple):
    """One translated token. `fixed_width` is what makes the piece safe to
    put inside an atomic group (see `_join`); an alternation branch or a
    recursive `**` component is not.
    """

    regex: str
    fixed_width: bool
    is_star: bool = False


_STAR = _Piece(".*", fixed_width=False, is_star=True)


@functools.cache
def compile_glob(pattern: str) -> re.Pattern[str]:
    try:
        # DOTALL: a newline is a legal character in a POSIX file name, so
        # `?` and `*` have to match one like any other.
        return re.compile(_translate(pattern), re.DOTALL)
    except re.error as error:
        # A character class carries its members through to the regex as-is,
        # so a range `ruff` itself rejects (`[z-a]`) surfaces here rather
        # than in `_translate_class`.
        message = f"`{pattern}` could not be compiled: {error}"
        raise InvalidGlobError(message) from error


def glob_matches(pattern: str, candidate: str) -> bool:
    return compile_glob(pattern).fullmatch(candidate) is not None


@functools.cache
def anchored_patterns(pattern: str, anchor: Path) -> tuple[str, ...]:
    """`pattern` resolved against `anchor` and expressed relative to it again,
    the way `ruff` resolves a pattern before matching: `./tests/**`,
    `tests/../src/**`, `../<project>/tests/**` and an absolute path all name
    what they look like. Empty when nothing it names lies beneath `anchor`,
    which nothing there can ever match (see
    `docs/adr/0046-exclude-glob-semantics.md`).

    Alternatives are expanded first, since resolution is textual and a `..`
    has to pop whatever each branch actually names -- `../{a,b}/src/**`
    resolves to a different directory per branch, and only some of them may
    land beneath the anchor.

    Only the anchored half of a match uses this. `ruff` matches a bare file
    name against the pattern exactly as written, so `./mod.py` covers the
    project root's own `mod.py` and no other.
    """
    anchor_parts = _resolved_parts(str(anchor))
    resolved: list[str] = []
    for alternative in _expanded(pattern):
        absolute = alternative if alternative.startswith("/") else f"{anchor}/{alternative}"
        parts = _resolved_parts(absolute)
        if parts[: len(anchor_parts)] == anchor_parts:
            resolved.append("/".join(parts[len(anchor_parts) :]))
    return tuple(resolved)


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


def _expanded(pattern: str) -> list[str]:
    """Every branch `pattern`'s alternations spell out, reading `\\` and
    `[...]` the way `_translate` does so a `{` those already spoke for is
    left alone.
    """
    group = _alternation_group(pattern)
    if group is None:
        return [pattern]
    start, end = group
    head, tail = pattern[:start], pattern[end + 1 :]
    return [
        expansion for branch in _branches(pattern[start + 1 : end]) for expansion in _expanded(f"{head}{branch}{tail}")
    ]


def _alternation_group(pattern: str) -> tuple[int, int] | None:
    """The outermost `{`/`}` pair, or `None` when there is nothing to expand.
    An unterminated `{` reads as nothing to expand here and is rejected by
    `compile_glob` instead.
    """
    depth = 0
    start = 0
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index = _class_end(pattern, index)
            continue
        if char == "{":
            if not depth:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if not depth:
                return start, index
        index += 1
    return None


def _branches(group: str) -> list[str]:
    branches: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(group):
        char = group[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index = _class_end(group, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and not depth:
            branches.append(group[start:index])
            start = index + 1
        index += 1
    branches.append(group[start:])
    return branches


def _class_end(pattern: str, index: int) -> int:
    """Just past the `]` closing the class opening at `index`."""
    cursor = index + 1
    if cursor < len(pattern) and pattern[cursor] in "!^":
        cursor += 1
    if cursor < len(pattern) and pattern[cursor] == "]":
        cursor += 1
    while cursor < len(pattern) and pattern[cursor] != "]":
        cursor += 1
    return cursor + 1


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
            pieces.append(_Piece("(?:", fixed_width=False))
            index += 1
        elif char == "}":
            if not depth:
                message = f"`{pattern}` closes a `{{` that was never opened"
                raise InvalidGlobError(message)
            depth -= 1
            pieces.append(_Piece(")", fixed_width=False))
            index += 1
        elif char == "," and depth:
            pieces.append(_Piece("|", fixed_width=False))
            index += 1
        else:
            pieces.append(_Piece(re.escape(char), fixed_width=True))
            index += 1

    if depth:
        message = f"`{pattern}` leaves a `{{` unclosed"
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
    members = "".join(f"\\{char}" if char in "^\\]" else char for char in body)
    return f"[{'^' if negated else ''}{members}]", end
