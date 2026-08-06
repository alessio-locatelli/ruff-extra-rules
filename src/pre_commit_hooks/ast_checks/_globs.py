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

__all__ = ["InvalidGlobError", "anchored_pattern", "compile_glob", "glob_matches", "relative_to_anchor"]


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
def anchored_pattern(pattern: str, anchor: Path) -> str | None:
    """`pattern` resolved against `anchor` and expressed relative to it again,
    the way `ruff` resolves a pattern before matching: `./tests/**`,
    `tests/../src/**`, `../<project>/tests/**` and an absolute path all name
    what they look like. `None` for a pattern resolving outside `anchor`,
    which nothing beneath it can ever match (see
    `docs/adr/0046-exclude-glob-semantics.md`).

    Only the anchored half of a match uses this. `ruff` matches a bare file
    name against the pattern exactly as written, so `./mod.py` covers the
    project root's own `mod.py` and no other.
    """
    absolute = pattern if pattern.startswith("/") else f"{anchor}/{pattern}"
    parts = _resolved_parts(absolute)
    anchor_parts = _resolved_parts(str(anchor))
    if parts[: len(anchor_parts)] != anchor_parts:
        return None
    return "/".join(parts[len(anchor_parts) :])


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
    cursor = index + 1
    negated = cursor < len(pattern) and pattern[cursor] in "!^"
    if negated:
        cursor += 1

    members: list[str] = []
    if cursor < len(pattern) and pattern[cursor] == "]":
        members.append("\\]")
        cursor += 1
    while cursor < len(pattern) and pattern[cursor] != "]":
        char = pattern[cursor]
        members.append(f"\\{char}" if char in "^\\" else char)
        cursor += 1

    if cursor == len(pattern):
        message = f"`{pattern}` leaves a `[` unclosed"
        raise InvalidGlobError(message)
    return f"[{'^' if negated else ''}{''.join(members)}]", cursor + 1
