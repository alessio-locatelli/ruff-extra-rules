from __future__ import annotations

from enum import Enum, auto

IMMUTABLE_CONSTRUCTORS = frozenset({"str", "int", "float", "bool", "bytes", "frozenset", "tuple"})

MUTABLE_CONSTRUCTORS = frozenset({"list", "dict", "set", "bytearray"})

ALL_CONSTRUCTORS = IMMUTABLE_CONSTRUCTORS | MUTABLE_CONSTRUCTORS


class ConfidenceLevel(Enum):
    CONSERVATIVE = auto()
    PERMISSIVE = auto()


def eligible_constructors(level: ConfidenceLevel) -> frozenset[str]:
    return ALL_CONSTRUCTORS if level is ConfidenceLevel.PERMISSIVE else IMMUTABLE_CONSTRUCTORS


def _literal_matches_constructor(hover_text: str, constructor: str) -> bool:
    if not (hover_text.startswith("Literal[") and hover_text.endswith("]")):
        return False
    inner = hover_text[len("Literal[") : -1]
    if constructor == "bool":
        return inner in {"True", "False"}
    if constructor == "int":
        return inner.lstrip("-").isdigit()
    if constructor == "str":
        return inner.startswith(('"', "'"))
    if constructor == "bytes":
        return inner.startswith(('b"', "b'"))
    return False


def _exact_match(hover_text: str, constructor: str) -> bool:
    if hover_text == constructor or hover_text.startswith(f"{constructor}["):
        return True
    return _literal_matches_constructor(hover_text, constructor)


def _is_unreliable(hover_text: str) -> bool:
    head = hover_text.split(" & ", 1)[0]
    return head in {"Any", "Unknown"}


def _split_top_level_union(hover_text: str) -> list[str]:
    members: list[str] = []
    depth = 0
    start = 0
    index = 0
    length = len(hover_text)
    while index < length:
        char = hover_text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and hover_text.startswith(" | ", index):
            members.append(hover_text[start:index])
            index += 3
            start = index
            continue
        index += 1
    members.append(hover_text[start:])
    return members


def is_exact_match(hover_text: str, constructor: str) -> bool:
    if _exact_match(hover_text, constructor):
        return True
    members = _split_top_level_union(hover_text)
    return len(members) > 1 and all(_exact_match(member, constructor) for member in members)


PUREPATH_HOVER_NAMES = frozenset({"Path", "PurePath", "PosixPath", "WindowsPath", "PurePosixPath", "PureWindowsPath"})


def is_purepath_hover(hover_text: str) -> bool:
    return any(member in PUREPATH_HOVER_NAMES for member in _split_top_level_union(hover_text))


def hover_passes_gate(hover_text: str | None, level: ConfidenceLevel, constructor: str) -> bool:
    if not hover_text or _is_unreliable(hover_text):
        return False
    if _exact_match(hover_text, constructor):
        return True
    if level is not ConfidenceLevel.PERMISSIVE:
        return False
    members = _split_top_level_union(hover_text)
    if len(members) > 1:
        return all(_exact_match(member, constructor) for member in members)
    return True
