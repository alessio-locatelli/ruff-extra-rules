from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

type QualifiedName = tuple[str, ...]


class AbstractParameters(Enum):
    NONE = auto()
    ELEMENT = auto()
    MAPPING = auto()


class ConcreteParameters(Enum):
    ELEMENT = auto()
    MAPPING = auto()
    TUPLE = auto()
    COUNTER = auto()


@dataclass(frozen=True, slots=True)
class AbstractType:
    name: str
    parameters: AbstractParameters


@dataclass(frozen=True, slots=True)
class ConcreteType:
    name: str
    qualified_name: QualifiedName
    parameters: ConcreteParameters
    supertypes: frozenset[str]
    augmented_operators: frozenset[type[ast.operator]]
    operand_operators: frozenset[type[ast.operator]] = frozenset()
    operand_types: frozenset[str] = frozenset()


_ABSTRACT_TYPES = (
    AbstractType("Iterable", AbstractParameters.ELEMENT),
    AbstractType("Collection", AbstractParameters.ELEMENT),
    AbstractType("Container", AbstractParameters.ELEMENT),
    AbstractType("Reversible", AbstractParameters.ELEMENT),
    AbstractType("Sequence", AbstractParameters.ELEMENT),
    AbstractType("MutableSequence", AbstractParameters.ELEMENT),
    AbstractType("Set", AbstractParameters.ELEMENT),
    AbstractType("MutableSet", AbstractParameters.ELEMENT),
    AbstractType("Mapping", AbstractParameters.MAPPING),
    AbstractType("MutableMapping", AbstractParameters.MAPPING),
    AbstractType("Sized", AbstractParameters.NONE),
)

TYPING_MODULES = ("typing", "typing_extensions")


def _abstract_names() -> dict[QualifiedName, AbstractType]:
    names: dict[QualifiedName, AbstractType] = {}
    for abstract in _ABSTRACT_TYPES:
        names["collections", "abc", abstract.name] = abstract
        typing_name = "AbstractSet" if abstract.name == "Set" else abstract.name
        for module in TYPING_MODULES:
            names[module, typing_name] = abstract
    return names


ABSTRACT_TYPES: Mapping[QualifiedName, AbstractType] = MappingProxyType(_abstract_names())

PREFILTER_PATTERNS = tuple(sorted({abstract.name for abstract in _ABSTRACT_TYPES} - {"MutableSequence", "MutableSet"}))

_SEQUENCE_SUPERTYPES = frozenset({"Iterable", "Collection", "Container", "Reversible", "Sized", "Sequence"})
_SET_SUPERTYPES = frozenset({"Iterable", "Collection", "Container", "Sized", "Set"})
_MAPPING_SUPERTYPES = frozenset({"Iterable", "Collection", "Container", "Reversible", "Sized", "Mapping"})

_SEQUENCE_OPERATORS = frozenset({ast.Add})
_SET_OPERATORS = frozenset({ast.BitOr, ast.BitAnd, ast.Sub, ast.BitXor})
_SET_OPERANDS = frozenset({"set", "frozenset"})
_MAPPING_OPERATORS = frozenset({ast.BitOr})

LIST = ConcreteType(
    "list",
    ("builtins", "list"),
    ConcreteParameters.ELEMENT,
    supertypes=_SEQUENCE_SUPERTYPES | {"MutableSequence"},
    augmented_operators=_SEQUENCE_OPERATORS,
)
TUPLE = ConcreteType(
    "tuple",
    ("builtins", "tuple"),
    ConcreteParameters.TUPLE,
    supertypes=_SEQUENCE_SUPERTYPES,
    augmented_operators=frozenset(),
    operand_operators=frozenset({ast.Add}),
    operand_types=frozenset({"tuple"}),
)
SET = ConcreteType(
    "set",
    ("builtins", "set"),
    ConcreteParameters.ELEMENT,
    supertypes=_SET_SUPERTYPES | {"MutableSet"},
    augmented_operators=frozenset(),
    operand_operators=_SET_OPERATORS,
    operand_types=_SET_OPERANDS,
)
FROZENSET = ConcreteType(
    "frozenset",
    ("builtins", "frozenset"),
    ConcreteParameters.ELEMENT,
    supertypes=_SET_SUPERTYPES,
    augmented_operators=frozenset(),
    operand_operators=_SET_OPERATORS,
    operand_types=_SET_OPERANDS,
)
DICT = ConcreteType(
    "dict",
    ("builtins", "dict"),
    ConcreteParameters.MAPPING,
    supertypes=_MAPPING_SUPERTYPES | {"MutableMapping"},
    augmented_operators=_MAPPING_OPERATORS,
)

_CONCRETE_TYPES = (
    LIST,
    TUPLE,
    SET,
    FROZENSET,
    DICT,
    ConcreteType(
        "MappingProxyType",
        ("types", "MappingProxyType"),
        ConcreteParameters.MAPPING,
        supertypes=_MAPPING_SUPERTYPES,
        augmented_operators=frozenset(),
    ),
    ConcreteType(
        "OrderedDict",
        ("collections", "OrderedDict"),
        ConcreteParameters.MAPPING,
        supertypes=_MAPPING_SUPERTYPES | {"MutableMapping"},
        augmented_operators=_MAPPING_OPERATORS,
    ),
    ConcreteType(
        "defaultdict",
        ("collections", "defaultdict"),
        ConcreteParameters.MAPPING,
        supertypes=_MAPPING_SUPERTYPES | {"MutableMapping"},
        augmented_operators=_MAPPING_OPERATORS,
    ),
    ConcreteType(
        "Counter",
        ("collections", "Counter"),
        ConcreteParameters.COUNTER,
        supertypes=_MAPPING_SUPERTYPES | {"MutableMapping"},
        augmented_operators=frozenset({ast.Add, ast.Sub, ast.BitOr, ast.BitAnd}),
    ),
    ConcreteType(
        "deque",
        ("collections", "deque"),
        ConcreteParameters.ELEMENT,
        supertypes=_SEQUENCE_SUPERTYPES | {"MutableSequence"},
        augmented_operators=_SEQUENCE_OPERATORS,
    ),
)

CONSTRUCTORS: Mapping[QualifiedName, ConcreteType] = MappingProxyType(
    {concrete.qualified_name: concrete for concrete in _CONCRETE_TYPES}
)

PRODUCERS: Mapping[QualifiedName, ConcreteType] = MappingProxyType(
    {("builtins", "sorted"): LIST, ("builtins", "dict", "fromkeys"): DICT}
)
