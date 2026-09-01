from __future__ import annotations

import pytest

from pre_commit_hooks.ast_checks.redundant_type_conversion.confidence import (
    ALL_CONSTRUCTORS,
    IMMUTABLE_CONSTRUCTORS,
    MUTABLE_CONSTRUCTORS,
    ConfidenceLevel,
    eligible_constructors,
    hover_passes_gate,
    is_exact_match,
    is_purepath_hover,
)


def test_immutable_and_mutable_constructors_partition_all_eleven() -> None:
    assert IMMUTABLE_CONSTRUCTORS | MUTABLE_CONSTRUCTORS == ALL_CONSTRUCTORS
    assert frozenset() == IMMUTABLE_CONSTRUCTORS & MUTABLE_CONSTRUCTORS
    assert len(ALL_CONSTRUCTORS) == 11


def test_conservative_is_immutable_only() -> None:
    assert eligible_constructors(ConfidenceLevel.CONSERVATIVE) == IMMUTABLE_CONSTRUCTORS


def test_aggressive_is_all_eleven() -> None:
    assert eligible_constructors(ConfidenceLevel.AGGRESSIVE) == ALL_CONSTRUCTORS


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
        ("bool", "int"),
        ("Iterable[str]", "list"),
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
        ("Literal[True]", "int"),
        ("Literal[False]", "int"),
        ("Literal[1]", "bool"),
        ('Literal["hi"]', "bytes"),
        ('Literal[b"hi"]', "str"),
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
    assert hover_passes_gate(hover_text, ConfidenceLevel.CONSERVATIVE, constructor) is False


@pytest.mark.parametrize(
    "hover_text",
    ["Iterable[str]", "list[int]", "dict[str, int]", 'Literal["hi"]', "SomeCustomClass"],
)
def test_aggressive_accepts_any_resolved_non_union_type(hover_text: str) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.AGGRESSIVE, "list") is True


@pytest.mark.parametrize(
    ("hover_text", "constructor"),
    [
        ("int | float", "int"),
        ("str | None", "list"),
        ("Literal[True] | int", "bool"),
    ],
    ids=["int-or-float", "str-or-none-as-list", "bool-literal-or-int"],
)
def test_aggressive_rejects_a_union_with_a_non_matching_member(hover_text: str, constructor: str) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.AGGRESSIVE, constructor) is False


@pytest.mark.parametrize(
    "hover_text",
    ["Iterable[str | int]", "Mapping[str, int | float]", "Callable[[int | str], None]"],
    ids=["union-in-generic-arg", "union-in-mapping-value", "union-in-callable-arg"],
)
def test_aggressive_does_not_split_a_union_nested_inside_brackets(hover_text: str) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.AGGRESSIVE, "list") is True


@pytest.mark.parametrize(
    ("hover_text", "constructor"),
    [
        ("list[int] | list[str]", "list"),
        ("Literal[1] | Literal[2]", "int"),
    ],
    ids=["list-generic-union", "int-literal-union"],
)
def test_aggressive_accepts_a_union_whose_every_member_matches(hover_text: str, constructor: str) -> None:
    assert hover_passes_gate(hover_text, ConfidenceLevel.AGGRESSIVE, constructor) is True


@pytest.mark.parametrize(
    ("hover_text", "constructor"),
    [("str", "str"), ("frozenset[int]", "frozenset"), ('Literal["hi"]', "str"), ("list[int] | list[str]", "list")],
    ids=["exact", "generic", "literal", "union"],
)
def test_is_exact_match_accepts_a_genuine_match(hover_text: str, constructor: str) -> None:
    assert is_exact_match(hover_text, constructor) is True


@pytest.mark.parametrize(
    ("hover_text", "constructor"),
    [("dict[str, list[int]]", "str"), ("Path", "str"), ("int | float", "int")],
    ids=["unrelated", "structural-only", "union-with-a-non-matching-member"],
)
def test_is_exact_match_rejects_a_structural_or_unrelated_type(hover_text: str, constructor: str) -> None:
    assert is_exact_match(hover_text, constructor) is False


@pytest.mark.parametrize(
    "hover_text",
    ["Path", "PurePath", "PosixPath", "WindowsPath", "PurePosixPath", "PureWindowsPath", "Path | None"],
    ids=["path", "purepath", "posixpath", "windowspath", "pureposixpath", "purewindowspath", "union"],
)
def test_is_purepath_hover_accepts_every_pathlib_class(hover_text: str) -> None:
    assert is_purepath_hover(hover_text) is True


@pytest.mark.parametrize(
    "hover_text",
    ["str", "PathLike", "MyCustomPath", "list[Path]"],
    ids=[
        "str",
        "unrelated-name-containing-path",
        "custom-subclass-not-recognized-by-name-alone",
        "path-nested-in-a-generic",
    ],
)
def test_is_purepath_hover_rejects_everything_else(hover_text: str) -> None:
    assert is_purepath_hover(hover_text) is False
