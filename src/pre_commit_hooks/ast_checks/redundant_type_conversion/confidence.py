"""Confidence tiering for TRI006 — see ADR-0035 and this check's own
`docs/rules/redundant-type-conversion.md` for the full rationale.
"""

from __future__ import annotations

from enum import Enum, auto

# Constructors whose result can never alias or share mutable state with
# their argument -- an exact-type-match redundant conversion of one of
# these is always safe to remove, so these are eligible at the
# conservative (default) confidence level.
IMMUTABLE_CONSTRUCTORS = frozenset({"str", "int", "float", "bool", "bytes", "frozenset", "tuple"})

# Copy-producing constructors: even when the argument is already of the
# exact produced type, removing the call changes a fresh, independent copy
# into a shared reference to the original object -- safe from the type
# checker's own point of view, but a behavior change if the caller (or the
# callee) mutates or aliases the result. Only eligible at the permissive
# confidence level, as an explicit, opt-in trade-off.
MUTABLE_CONSTRUCTORS = frozenset({"list", "dict", "set", "bytearray"})

ALL_CONSTRUCTORS = IMMUTABLE_CONSTRUCTORS | MUTABLE_CONSTRUCTORS


class ConfidenceLevel(Enum):
    CONSERVATIVE = auto()
    PERMISSIVE = auto()


def eligible_constructors(level: ConfidenceLevel) -> frozenset[str]:
    """Which of the eleven builtin constructors TRI006 considers a candidate
    at all, at `level`. This is one of two independent confidence axes —
    the other, `hover_passes_gate()`, decides how strictly a candidate's
    own argument type must already match before it's even worth the
    synthetic-rewrite-and-recheck's own cost.
    """
    return ALL_CONSTRUCTORS if level is ConfidenceLevel.PERMISSIVE else IMMUTABLE_CONSTRUCTORS


def _literal_matches_constructor(hover_text: str, constructor: str) -> bool:
    """Whether a `Literal[...]` hover result (`ty` reports a variable's
    flow-narrowed value — e.g. `x: str = "hi"` hovers as `Literal["hi"]`,
    not the wider declared `str` — rather than only ever widening back to
    the declared annotation) is exactly of the type `constructor` would
    produce.

    Deliberately keyed on the literal's own textual spelling
    (`"True"`/`"False"` vs. a bare digit sequence vs. a quoted string/byte
    string), not on runtime subtyping: `bool` is an `int` subclass, so
    `Literal[True]` is technically assignable wherever an `int` is
    expected, but `int(some_bool)` is *not* a redundant conversion — it
    genuinely produces a plain `int`, a different runtime type than the
    `bool` it started from. Matching by spelling is what keeps `int` and
    `bool` from being conflated here, the same distinction PEP 586 itself
    draws between `Literal[1]` and `Literal[True]`.
    """
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
    """Cheap pre-filter deciding whether a candidate is worth the expense
    of the synthetic rewrite-and-recheck (the actual, final redundancy
    decision — this never replaces it, only narrows what pays for it).

    `hover_text` is the argument expression's own statically-inferred type,
    as plain text from `ty`'s hover response. `None`/empty, or the literal
    text `"Any"`, always fails the gate at *both* levels: `Any` trivially
    satisfies every type, so a recheck against it would look "redundant"
    regardless of the argument's real runtime type — it carries no genuine
    type-safety evidence, at either confidence level (see
    docs/audits/type-checker-selection-for-redundant-type-conversion.md's
    "Hover alone is not enough" section for the general shape of this
    problem).

    At `PERMISSIVE`, any other resolved type passes -- this is exactly the
    tier meant to also catch a *structural/protocol-satisfying* match
    (e.g. `list[str]` satisfying an `Iterable[str]` parameter), which by
    definition doesn't textually match the constructor's own produced
    type, so no stricter comparison is possible here (see the audit's
    "Hover alone is not enough" finding — only the recheck itself, not
    hover, can decide that case).

    At `CONSERVATIVE`, the argument's own type must already be an *exact*
    match for what `constructor` would produce: the constructor's own bare
    name for a scalar (`"str"` for `str(...)`), that name followed by `[`
    for a generic immutable container (`"frozenset["`/`"tuple["`), or a
    flow-narrowed `Literal[...]` of the same scalar (see
    `_literal_matches_constructor`).
    """
    if not hover_text or hover_text == "Any":
        return False
    if level is ConfidenceLevel.PERMISSIVE:
        return True
    if hover_text == constructor or hover_text.startswith(f"{constructor}["):
        return True
    return _literal_matches_constructor(hover_text, constructor)
