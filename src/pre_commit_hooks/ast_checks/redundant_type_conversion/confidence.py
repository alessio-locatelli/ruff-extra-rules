"""Confidence tiering for TRI006 — see ADR-0035 and `docs/rules/redundant-type-conversion.md`."""

from __future__ import annotations

from enum import Enum, auto

# Eligible at the conservative (default) level -- see ADR-0035.
IMMUTABLE_CONSTRUCTORS = frozenset({"str", "int", "float", "bool", "bytes", "frozenset", "tuple"})

# Eligible only at the permissive level -- see ADR-0035.
MUTABLE_CONSTRUCTORS = frozenset({"list", "dict", "set", "bytearray"})

ALL_CONSTRUCTORS = IMMUTABLE_CONSTRUCTORS | MUTABLE_CONSTRUCTORS


class ConfidenceLevel(Enum):
    CONSERVATIVE = auto()
    PERMISSIVE = auto()


def eligible_constructors(level: ConfidenceLevel) -> frozenset[str]:
    return ALL_CONSTRUCTORS if level is ConfidenceLevel.PERMISSIVE else IMMUTABLE_CONSTRUCTORS


def _literal_matches_constructor(hover_text: str, constructor: str) -> bool:
    # Keyed on the literal's own textual spelling, not runtime subtyping --
    # see ADR-0035's "Confidence tiering" for why bool/int must not be
    # conflated here.
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


def hover_passes_gate(hover_text: str | None, level: ConfidenceLevel, constructor: str) -> bool:
    """Cheap pre-filter for whether a candidate is worth the recheck's own cost. See ADR-0035's "Confidence tiering"."""
    if not hover_text or hover_text == "Any":
        return False
    if level is ConfidenceLevel.PERMISSIVE:
        return True
    if hover_text == constructor or hover_text.startswith(f"{constructor}["):
        return True
    return _literal_matches_constructor(hover_text, constructor)
