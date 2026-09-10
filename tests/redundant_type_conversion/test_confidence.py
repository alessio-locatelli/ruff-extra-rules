from __future__ import annotations

import pytest

from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import (
    ALL_CONSTRUCTORS,
    IMMUTABLE_CONSTRUCTORS,
    MUTABLE_CONSTRUCTORS,
    ConfidenceLevel,
    eligible_constructors,
    hover_passes_gate,
    is_comparison_safe_hover,
    is_exact_match,
    is_purepath_hover,
)


def test_immutable_and_mutable_constructors_partition_all_eleven() -> None:
    assert IMMUTABLE_CONSTRUCTORS | MUTABLE_CONSTRUCTORS == ALL_CONSTRUCTORS
    assert frozenset() == IMMUTABLE_CONSTRUCTORS & MUTABLE_CONSTRUCTORS
    assert len(ALL_CONSTRUCTORS) == 11


@pytest.mark.parametrize(
    ("level", "expected"),
    [(ConfidenceLevel.CONSERVATIVE, IMMUTABLE_CONSTRUCTORS), (ConfidenceLevel.AGGRESSIVE, ALL_CONSTRUCTORS)],
    ids=["conservative-is-immutable-only", "aggressive-is-all-eleven"],
)
def test_eligible_constructors_by_level(level: ConfidenceLevel, expected: frozenset[str]) -> None:
    assert eligible_constructors(level) == expected


@pytest.mark.parametrize(
    "hover_text",
    [
        None,
        "",
        "Any",
        "Unknown",
        "Any & ~AlwaysFalsy",
        "Unknown & ~AlwaysFalsy",
    ],
    ids=["none", "empty", "any", "unknown", "narrowed-any", "narrowed-unknown"],
)
def test_gate_rejects_unusable_hover_at_both_levels(hover_text: str | None) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.CONSERVATIVE, "str") is False
    assert hover_passes_gate(hover_text, ConfidenceLevel.AGGRESSIVE, "str") is False


@pytest.mark.parametrize(
    ("hover_text", "constructor", "expected"),
    [
        ("str", "str", True),
        ("int", "int", True),
        ("float", "float", True),
        ("bool", "bool", True),
        ("bytes", "bytes", True),
        ("frozenset[int]", "frozenset", True),
        ("tuple[int, str]", "tuple", True),
        ('Literal["hi"]', "str", True),
        ("Literal[5]", "int", True),
        ("Literal[-5]", "int", True),
        ("Literal[True]", "bool", True),
        ("Literal[False]", "bool", True),
        ('Literal[b"hi"]', "bytes", True),
        ("str | None", "str", False),
        ("LiteralString", "str", False),
        ("bool", "int", False),
        ("Iterable[str]", "list", False),
        ("Literal[True]", "int", False),
        ("Literal[False]", "int", False),
        ("Literal[1]", "bool", False),
        ('Literal["hi"]', "bytes", False),
        ('Literal[b"hi"]', "str", False),
        ("Literal[1]", "float", False),
        ("Literal[1]", "frozenset", False),
        ("Literal[1]", "tuple", False),
    ],
    ids=[
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "frozenset-generic",
        "tuple-generic",
        "str-literal",
        "int-literal",
        "negative-int-literal",
        "bool-true",
        "bool-false",
        "bytes-literal",
        "union",
        "str-subtype",
        "bool-not-int",
        "protocol-match",
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
def test_conservative_hover_gate(hover_text: str, constructor: str, expected: bool) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.CONSERVATIVE, constructor) is expected


@pytest.mark.parametrize(
    ("hover_text", "constructor", "expected"),
    [
        ("Iterable[str]", "list", True),
        ("list[int]", "list", True),
        ("dict[str, int]", "list", True),
        ("frozenset[int]", "set", True),
        ("Mapping[str, int]", "dict", True),
        ("KeysView[str]", "set", True),
        ("AbstractSet[int]", "frozenset", True),
        ("str", "list", True),
        ("bytes", "list", True),
        ("memoryview", "tuple", True),
        ("str & ~AlwaysFalsy", "list", True),
        ("bytes", "bytearray", True),
        ("memoryview", "bytearray", True),
        ("int | float", "int", False),
        ("str | None", "list", False),
        ("Literal[True] | int", "bool", False),
        ("list[int] | list[str]", "list", True),
        ("Literal[1] | Literal[2]", "int", True),
        ("Iterable[str | int]", "list", True),
        ("Sequence[int | str]", "list", True),
        ("Mapping[str, int | float]", "dict", True),
        ("SomeCustomClass", "list", False),
        ('Literal["hi"]', "list", False),
        ("ExtendedClientResponseError", "str", False),
        ("SomeCustomClass", "dict", False),
        ("int", "list", False),
        ("str", "bytearray", False),
        ("list[int]", "set", False),
        ("tuple[int, ...]", "set", False),
        ("list[int]", "frozenset", False),
        ("tuple[int, ...]", "frozenset", False),
        ("Sequence[int]", "set", False),
        ("Iterable[int]", "frozenset", False),
        ("ValuesView[int]", "set", False),
        ("str", "set", False),
        ("ItemsView[str, int]", "set", False),
        ("ItemsView[str, list[int]]", "set", False),
        ("ItemsView[str, int]", "frozenset", False),
    ],
    ids=[
        "iterable-protocol",
        "exact-generic",
        "dict-is-iterable",
        "frozenset-to-set",
        "mapping-protocol",
        "keysview",
        "abstractset-to-frozenset",
        "str-is-iterable",
        "bytes-is-iterable",
        "memoryview-is-iterable",
        "narrowed-intersection-type",
        "bytes-to-bytearray",
        "memoryview-to-bytearray",
        "union-int-or-float-as-int",
        "union-str-or-none-as-list",
        "union-bool-literal-or-int",
        "union-list-generic-matches",
        "union-int-literal-matches",
        "union-in-generic-arg-not-split",
        "union-in-sequence-arg-not-split",
        "union-in-mapping-value-not-split",
        "unrelated-class",
        "literal-has-no-iterable-relationship",
        "unrelated-class-as-str",
        "unrelated-as-dict",
        "scalar-as-list",
        "str-needs-an-encoding-for-bytearray",
        "list-to-set",
        "tuple-to-set",
        "list-to-frozenset",
        "tuple-to-frozenset",
        "sequence-to-set",
        "iterable-to-frozenset",
        "valuesview-to-set",
        "str-to-set",
        "itemsview-to-set",
        "itemsview-with-unhashable-value-to-set",
        "itemsview-to-frozenset",
    ],
)
def test_aggressive_hover_gate(hover_text: str, constructor: str, expected: bool) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.AGGRESSIVE, constructor) is expected


@pytest.mark.parametrize(
    ("hover_text", "constructor", "expected"),
    [
        ("str", "str", True),
        ("frozenset[int]", "frozenset", True),
        ('Literal["hi"]', "str", True),
        ("list[int] | list[str]", "list", True),
        ("dict[str, list[int]]", "str", False),
        ("Path", "str", False),
        ("int | float", "int", False),
    ],
    ids=["exact", "generic", "literal", "union", "unrelated", "structural-only", "union-with-a-non-matching-member"],
)
def test_is_exact_match(hover_text: str, constructor: str, expected: bool) -> None:
    assert is_exact_match(hover_text, constructor) is expected


@pytest.mark.parametrize(
    ("hover_text", "constructor", "expected"),
    [
        ("LiteralString", "str", True),
        ("bool", "int", True),
        ("bool", "float", True),
        ("bytearray", "bytes", True),
        ("frozenset[int]", "set", True),
        ("AbstractSet[int]", "set", True),
        ("set[int]", "frozenset", True),
        ("frozenset[int] | set[str]", "set", True),
        ("Path", "str", False),
        ("PurePath", "str", False),
        ("dict[str, int]", "set", False),
        ("Mapping[str, int]", "set", False),
        ("tuple[int, ...]", "set", False),
        ("list[int]", "set", False),
        ("dict[str, int]", "frozenset", False),
        ("list[int]", "list", False),
        ("tuple[int, ...]", "tuple", False),
        ("Mapping[str, int]", "dict", False),
        ("bytearray", "bytearray", False),
        ("set[int] | dict[str, int]", "set", False),
        ("memoryview", "bytes", False),
        ("int", "float", False),
    ],
    ids=[
        "str-subtype",
        "bool-as-int",
        "bool-as-float",
        "bytearray-as-bytes",
        "frozenset-as-set",
        "abstractset-as-set",
        "set-as-frozenset",
        "union-of-set-family",
        "path-as-str",
        "purepath-as-str",
        "dict-as-set",
        "mapping-as-set",
        "tuple-as-set",
        "list-as-set",
        "dict-as-frozenset",
        "list-has-no-safe-family",
        "tuple-has-no-safe-family",
        "dict-has-no-safe-family",
        "bytearray-has-no-safe-family",
        "one-unsafe-union-member",
        "memoryview-does-not-support-ordering-against-bytes",
        "int-can-lose-precision-as-float",
    ],
)
def test_is_comparison_safe_hover(hover_text: str, constructor: str, expected: bool) -> None:
    assert is_comparison_safe_hover(hover_text, constructor) is expected


@pytest.mark.parametrize(
    ("hover_text", "expected"),
    [
        ("Path", True),
        ("PurePath", True),
        ("PosixPath", True),
        ("WindowsPath", True),
        ("PurePosixPath", True),
        ("PureWindowsPath", True),
        ("Path | None", True),
        ("str", False),
        ("PathLike", False),
        ("MyCustomPath", False),
        ("list[Path]", False),
    ],
    ids=[
        "path",
        "purepath",
        "posixpath",
        "windowspath",
        "pureposixpath",
        "purewindowspath",
        "union",
        "str",
        "unrelated-name-containing-path",
        "custom-subclass-not-recognized-by-name-alone",
        "path-nested-in-a-generic",
    ],
)
def test_is_purepath_hover(hover_text: str, expected: bool) -> None:
    assert is_purepath_hover(hover_text) is expected
