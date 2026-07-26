from __future__ import annotations

import pytest

from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import (
    ALL_CONSTRUCTORS,
    IMMUTABLE_CONSTRUCTORS,
    MUTABLE_CONSTRUCTORS,
    ConfidenceLevel,
    eligible_constructors,
    hover_passes_gate,
)


def test_immutable_and_mutable_constructors_partition_all_eleven() -> None:
    # Issue #108 says "all ten builtin type/collection constructors" but
    # then enumerates eleven (str/int/float/bool/bytes/frozenset/tuple/
    # list/dict/set/bytearray), consistently, everywhere it lists them --
    # the explicit, repeated enumeration is authoritative over the "ten"
    # miscount.
    assert IMMUTABLE_CONSTRUCTORS | MUTABLE_CONSTRUCTORS == ALL_CONSTRUCTORS
    assert frozenset() == IMMUTABLE_CONSTRUCTORS & MUTABLE_CONSTRUCTORS
    assert len(ALL_CONSTRUCTORS) == 11


def test_conservative_is_immutable_only() -> None:
    assert eligible_constructors(ConfidenceLevel.CONSERVATIVE) == IMMUTABLE_CONSTRUCTORS


def test_permissive_is_all_eleven() -> None:
    assert eligible_constructors(ConfidenceLevel.PERMISSIVE) == ALL_CONSTRUCTORS


@pytest.mark.parametrize("hover_text", [None, "", "Any"])
def test_gate_rejects_unusable_hover_at_both_levels(hover_text: str | None) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.CONSERVATIVE, "str") is False
    assert hover_passes_gate(hover_text, ConfidenceLevel.PERMISSIVE, "str") is False


@pytest.mark.parametrize(
    ("hover_text", "constructor"),
    [
        ("str", "str"),
        ("int", "int"),
        ("float", "float"),
        ("bool", "bool"),
        ("bytes", "bytes"),
        ("frozenset[int]", "frozenset"),
        ("tuple[int, str]", "tuple"),
    ],
    ids=["str", "int", "float", "bool", "bytes", "frozenset-generic", "tuple-generic"],
)
def test_conservative_accepts_an_exact_match(hover_text: str, constructor: str) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.CONSERVATIVE, constructor) is True


@pytest.mark.parametrize(
    ("hover_text", "constructor"),
    [
        ("str | None", "str"),
        ("LiteralString", "str"),
        ("bool", "int"),  # bool is an int subclass, but not the same type
        ("Iterable[str]", "list"),  # structural match only -- not textually "list" or "list["
    ],
    ids=["union", "str-subtype", "bool-not-int", "protocol-match"],
)
def test_conservative_rejects_a_non_exact_match(hover_text: str, constructor: str) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.CONSERVATIVE, constructor) is False


@pytest.mark.parametrize(
    ("hover_text", "constructor"),
    [
        ('Literal["hi"]', "str"),
        ("Literal[5]", "int"),
        ("Literal[-5]", "int"),
        ("Literal[True]", "bool"),
        ("Literal[False]", "bool"),
        ('Literal[b"hi"]', "bytes"),
    ],
    ids=["str-literal", "int-literal", "negative-int-literal", "bool-true", "bool-false", "bytes-literal"],
)
def test_conservative_accepts_a_flow_narrowed_literal_of_the_same_scalar(hover_text: str, constructor: str) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.CONSERVATIVE, constructor) is True


@pytest.mark.parametrize(
    ("hover_text", "constructor"),
    [
        ("Literal[True]", "int"),  # bool value, int() genuinely changes the runtime type
        ("Literal[False]", "int"),
        ("Literal[1]", "bool"),  # int value, not a bool literal spelling
        ('Literal["hi"]', "bytes"),
        ('Literal[b"hi"]', "str"),
        # A Literal[...] result isn't even meaningful for these constructors
        # (a literal float/frozenset/tuple isn't a thing `ty` reports as
        # Literal[...]), so _literal_matches_constructor's fallback branch
        # must reject it rather than falling through to some default match.
        ("Literal[1]", "float"),
        ("Literal[1]", "frozenset"),
        ("Literal[1]", "tuple"),
    ],
    ids=[
        "bool-literal-as-int",
        "bool-false-as-int",
        "int-literal-as-bool",
        "str-literal-as-bytes",
        "bytes-as-str",
        "literal-as-float",
        "literal-as-frozenset",
        "literal-as-tuple",
    ],
)
def test_conservative_rejects_a_literal_of_a_different_scalar(hover_text: str, constructor: str) -> None:
    # Regression guard: bool is an int subclass at runtime, so a naive
    # "Literal[...] always matches" rule would let int(some_bool) through
    # as if it were a no-op, even though it genuinely changes the runtime
    # type from bool to plain int.
    assert hover_passes_gate(hover_text, ConfidenceLevel.CONSERVATIVE, constructor) is False


@pytest.mark.parametrize(
    "hover_text",
    ["str | None", "Iterable[str]", "list[int]", "dict[str, int]", 'Literal["hi"]', "SomeCustomClass"],
)
def test_permissive_accepts_any_resolved_type(hover_text: str) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.PERMISSIVE, "list") is True
