from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._base import CheckResult, FixOutcome
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks.overly_broad_return_type import OverlyBroadReturnTypeCheck
from pre_commit_hooks.ast_checks.overly_broad_return_type.analysis import OverlyBroadReturnTypeLevel

ABC_IMPORTS = "from collections.abc import Iterable, Mapping, MutableSequence, Sequence, Set, Sized\n"


def _check(source: str, level: OverlyBroadReturnTypeLevel = OverlyBroadReturnTypeLevel.CONSERVATIVE) -> CheckResult:
    return OverlyBroadReturnTypeCheck(level).check(Path("test.py"), ast.parse(source), source)


def _advice(message: str) -> str:
    return message.split("; ", 1)[1].split(". Or add", 1)[0]


@pytest.mark.parametrize(
    ("source", "advice"),
    [
        (
            (
                "from collections.abc import Mapping\nfrom types import MappingProxyType\n\n"
                "def config() -> Mapping[str, int]:\n    return MappingProxyType({'timeout': 30})\n"
            ),
            "use `MappingProxyType[str, int]`",
        ),
        (
            (
                "import types\nfrom collections.abc import Mapping\n\n"
                "def config() -> Mapping[str, int]:\n    return types.MappingProxyType({})\n"
            ),
            "use `MappingProxyType[str, int]`",
        ),
        (
            ABC_IMPORTS
            + "\ndef names(items: list[str]) -> Sequence[str]:\n    return [item.upper() for item in items]\n",
            "use `list[str]`",
        ),
        (
            ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    return 1, 2\n",
            "use `tuple[int, ...]`",
        ),
        (
            "import typing\n\ndef pair() -> typing.Sequence[int]:\n    return [1, 2]\n",
            "use `list[int]`",
        ),
        (
            "import collections.abc as cabc\n\ndef table() -> cabc.Mapping[str, int]:\n    return {}\n",
            "use `dict[str, int]`",
        ),
        (
            "from typing import AbstractSet\n\ndef ids() -> AbstractSet[int]:\n    return frozenset()\n",
            "use `frozenset[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef maybe(flag: bool) -> Sequence[int] | None:\n"
            "    if flag:\n        return None\n    return [1]\n",
            "use `list[int]`",
        ),
        (
            "from typing import Optional\n" + ABC_IMPORTS + "\ndef maybe(flag: bool) -> Optional[Iterable[int]]:\n"
            "    if flag:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            "from typing import Union\n" + ABC_IMPORTS + "\ndef maybe(flag: bool) -> Union[None, Set[int]]:\n"
            "    if flag:\n        return\n    return {1}\n",
            "use `set[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef ordered(items: set[str]) -> Sequence[str]:\n    return sorted(items)\n",
            "use `list[str]`",
        ),
        (
            ABC_IMPORTS + "\ndef flags(names: list[str]) -> Mapping[str, bool]:\n"
            "    return dict.fromkeys(names, False)\n",
            "use `dict[str, bool]`",
        ),
        (
            ABC_IMPORTS + "\ndef single(value: int, flag: bool) -> Iterable[int]:\n"
            "    return [value] if flag else []\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef keys() -> Iterable[str]:\n    return {'a': 1}\n",
            "use a `dict` annotation",
        ),
        (
            "from collections import Counter\n" + ABC_IMPORTS + "\ndef tally(words: list[str]) -> Mapping[str, int]:\n"
            "    return Counter(words)\n",
            "use `Counter[str]`",
        ),
        (
            "from collections import Counter\n" + ABC_IMPORTS + "\ndef tally(words: list[str]) -> Iterable[str]:\n"
            "    return Counter(words)\n",
            "use `Counter[str]`",
        ),
        (
            ABC_IMPORTS + "\ndef collect(items: list[int]) -> Sequence[int]:\n"
            "    out = []\n    for item in items:\n        out.append(item)\n    return out\n",
            "use a `list` annotation",
        ),
        (
            ABC_IMPORTS + "\ndef collect(items: list[int]) -> MutableSequence[int]:\n"
            "    out = [1]\n    out += items\n    return out\n",
            "use a `list` annotation",
        ),
        (
            ABC_IMPORTS + "\ndef collect() -> Sequence:\n    out = []\n    return out\n",
            "use a `list` annotation",
        ),
        (
            ABC_IMPORTS + "\ndef collect() -> Sequence[int]:\n    out = (1,)\n    out += (2,)\n    return out\n",
            "use a `tuple` annotation",
        ),
        (
            ABC_IMPORTS + "\ndef collect() -> Set[int]:\n    out = {1}\n    out |= {2}\n    return out\n",
            "use a `set` annotation",
        ),
        (
            ABC_IMPORTS
            + "\ndef collect() -> Sequence[int]:\n    out: list[int] = []\n    copy = out\n    return copy\n",
            "use a `list` annotation",
        ),
        (
            ABC_IMPORTS + "\ndef collect(flag: bool) -> Sequence[int]:\n    first = (inner := [1])\n    second = []\n"
            "    if flag:\n        return first\n    return second\n",
            "use a `list` annotation",
        ),
        (
            ABC_IMPORTS + "\ndef outer() -> None:\n    def init(arg=(factory := list)) -> None:\n        pass\n\n"
            "    def replace() -> None:\n        nonlocal factory\n        factory = tuple\n\n"
            "    def items() -> Sequence[int]:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef outer() -> None:\n    factory = list\n\n    def middle() -> None:\n"
            "        def inner() -> None:\n            nonlocal factory\n            factory = tuple\n\n"
            "    def items() -> Sequence[int]:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef collect() -> Sequence[int]:\n    return (out := [])\n",
            "use `list[int]`",
        ),
        (
            "from collections.abc import Sequence as Seq\n\ndef pair() -> Seq[int]:\n    return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\nasync def fetch() -> Sequence[int]:\n    return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef outer() -> None:\n    def inner() -> Sequence[int]:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef measure() -> Sized:\n    return frozenset()\n",
            "use a `frozenset` annotation",
        ),
        (
            "from typing import final\n" + ABC_IMPORTS + "\n@final\nclass Final:\n"
            "    def items(self) -> Sequence[int]:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            "from typing import final\n" + ABC_IMPORTS + "\nclass Plain:\n    @final\n"
            "    def items(self) -> Sequence[int]:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\nclass Plain:\n    def __items(self) -> Sequence[int]:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS
            + "\nclass Plain:\n    list = None\n\n    def __items(self) -> Sequence[int]:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef config() -> Mapping[str, int]:\n"
            "    from types import MappingProxyType\n\n    return MappingProxyType({})\n",
            "use `MappingProxyType[str, int]`",
        ),
        (
            "import builtins\n" + ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    return [1]\n",
            "use `list[int]`",
        ),
        (
            "import functools\n" + ABC_IMPORTS + "\n@functools.cache\ndef pair() -> Sequence[int]:\n    return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\n@app.get('/')\ndef pair() -> Sequence[int]:\n    return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef pair(flag: bool) -> Sequence[int]:\n"
            "    try:\n        return [int(flag)]\n    except ValueError:\n        return []\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef pair(value: int) -> Sequence[int]:\n"
            "    match value:\n        case 1:\n            return [1]\n        case _:\n            return []\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef pair(values: list[int]) -> Sequence[int]:\n"
            "    while True:\n        if values:\n            return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef pair(rows: list[int]) -> Sequence[int]:\n    while True:\n"
            "        for row in rows:\n            break\n\n        def inner() -> None:\n"
            "            while True:\n                break\n\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    if (out := []) is not None:\n        pass\n"
            "    return out\n",
            "use a `list` annotation",
        ),
        (
            ABC_IMPORTS + "\ndef pair[list]() -> Sequence[int]:\n    return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS
            + "\ndef pair(*args: int, **kwargs: int) -> Sequence[int]:\n    return [*args, *kwargs.values()]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ntry:\n    import fast\nexcept ImportError:\n"
            "    def pair() -> Sequence[int]:\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\nmatch MODE:\n    case 1:\n        def pair() -> Sequence[int]:\n            return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    with open('x'):\n        return [1]\n",
            "use `list[int]`",
        ),
        (
            ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    try:\n        pass\n    finally:\n        return [1]\n",
            "use `list[int]`",
        ),
    ],
)
def test_check_reports_a_wider_return_annotation(source: str, advice: str) -> None:
    violations = _check(source)

    assert [_advice(violation.message) for violation in violations] == [advice]


@pytest.mark.parametrize(
    "source",
    [
        ABC_IMPORTS + "\ndef pair() -> list[int]:\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair(items: list[int]) -> Sequence[int]:\n    return items\n",
        ABC_IMPORTS + "\ndef pair(flag: bool) -> Sequence[int]:\n    if flag:\n        return [1]\n    return (1,)\n",
        ABC_IMPORTS + "\ndef pair() -> Iterable[int]:\n    yield 1\n",
        ABC_IMPORTS + "\ndef pair() -> Mapping[str, int]:\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair(flag: bool) -> Sequence[int]:\n    if flag:\n        return [1]\n",
        ABC_IMPORTS + "\ndef pair(flag: bool) -> Sequence[int]:\n    while True:\n        if flag:\n"
        "            break\n        return [1]\n",
        ABC_IMPORTS + "\ndef pair(value: int) -> Sequence[int]:\n    match value:\n        case 1:\n"
        "            return [1]\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    return\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    ...\n",
        ABC_IMPORTS + "\nclass Box:\n    def items(self) -> Sequence[int]:\n        return self._items\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    return build()\n",
        ABC_IMPORTS + "\ndict = OrderedDict\n\ndef pair() -> Mapping[str, int]:\n    return dict()\n",
        "from collections.abc import *\n\ndef pair() -> Sequence[int]:\n    return [1]\n",
        ABC_IMPORTS + "from helpers import *\n\ndef pair() -> Sequence[int]:\n    return list()\n",
        "from typing import overload\n" + ABC_IMPORTS + "\n@overload\ndef pair(value: int) -> list[int]: ...\n"
        "@overload\ndef pair(value: str) -> list[str]: ...\n"
        "def pair(value: int | str) -> Sequence[int | str]:\n    return [value]\n",
        "import sys\n" + ABC_IMPORTS + "\nif sys.platform == 'linux':\n"
        "    def pair() -> Sequence[int]:\n        return [1]\nelse:\n"
        "    def pair() -> Sequence[int]:\n        return (1,)\n",
        "from abc import abstractmethod\n" + ABC_IMPORTS + "\nclass Base:\n    @abstractmethod\n"
        "    def items(self) -> Sequence[int]:\n        return []\n",
        "from typing import Protocol, final\n" + ABC_IMPORTS + "\n@final\nclass Proto(Protocol):\n"
        "    def items(self) -> Sequence[int]:\n        return []\n",
        "from abc import ABC\nfrom typing import final\n" + ABC_IMPORTS + "\n@final\nclass Base(ABC):\n"
        "    def items(self) -> Sequence[int]:\n        return [1]\n",
        "from abc import ABCMeta\nfrom typing import final\n"
        + ABC_IMPORTS
        + "\n@final\nclass Base(metaclass=ABCMeta):\n"
        "    def items(self) -> Sequence[int]:\n        return [1]\n",
        "from abc import abstractmethod\nfrom typing import final\n" + ABC_IMPORTS + "\n@final\nclass Base:\n"
        "    def items(self) -> Sequence[int]:\n        return [1]\n\n"
        "    @abstractmethod\n    def other(self) -> None: ...\n",
        ABC_IMPORTS + "\nclass Plain:\n    def items(self) -> Sequence[int]:\n        return [1]\n",
        "from typing import final\n" + ABC_IMPORTS + "\n@final\nclass Final:\n"
        "    def __dir__(self) -> Iterable[str]:\n        return ['x']\n",
        ABC_IMPORTS + "\ndef pair() -> 'Sequence[int]':\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int] | Mapping[str, int]:\n    return [1]\n",
        "from typing import Optional\n"
        + ABC_IMPORTS
        + "\ndef pair() -> Optional[Sequence[int], None]:\n    return [1]\n",
        "from typing import Annotated\n"
        + ABC_IMPORTS
        + "\ndef pair() -> Annotated[Sequence[int], 1]:\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int, str]:\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int,]:\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[*Ts]:\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair() -> Mapping[str, *Ts]:\n    return {}\n",
        ABC_IMPORTS + "\ndef pair(box: object) -> Sequence[int]:\n    return box.items\n",
        "import os\n" + ABC_IMPORTS + "\ndef pair() -> Sequence[str]:\n    return os.listdir()\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    out, other = [], []\n    return out\n",
        ABC_IMPORTS + "\nclass Rebind:\n    global list\n    list = tuple\n\n"
        "def pair() -> Sequence[int]:\n    return list()\n",
        ABC_IMPORTS + "\ndef pair(flag: bool) -> Sequence[int]:\n    while True:\n        for item in []:\n"
        "            break\n        else:\n            break\n        return [1]\n",
        ABC_IMPORTS + "\ndef pair() -> Sized[int]:\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair[Sequence]() -> Sequence[int]:\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair(rows: list[list[int]]) -> Sequence[int]:\n"
        "    for out in rows:\n        pass\n    return out\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    global out\n    out = []\n    return out\n",
        ABC_IMPORTS + "\ndef pair(items: list[int]) -> Sequence[int]:\n    items = list(items)\n    return items\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    out: Sequence[int] = []\n    return out\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    out: Sequence[int]\n    out = []\n    return out\n",
        ABC_IMPORTS + "\ndef pair(other: object) -> Sequence[int]:\n    out = []\n    out *= other\n    return out\n",
        ABC_IMPORTS + "\ndef pair(other: object) -> Set[int]:\n    out = set()\n    out |= other\n    return out\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    out = []\n    out = out\n    return out\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    out = (1,)\n    out += [2]\n    return out\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    out += [2]\n    return out\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    out = []\n\n    def grow() -> None:\n"
        "        nonlocal out\n        out = ()\n\n    grow()\n    return out\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    out = []\n    del out\n    return out\n",
        ABC_IMPORTS + "\nout = []\n\ndef pair() -> Sequence[int]:\n    return out\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    def inner() -> list[int]:\n        return [1]\n\n"
        "    return inner()\n",
        "from collections.abc import Iterator\n\ndef pair() -> Iterator[int]:\n    return iter([1])\n",
        "from typing import Set\n\ndef pair() -> Set[int]:\n    return {1}\n",
        "from .abc import Sequence\n\ndef pair() -> Sequence[int]:\n    return [1]\n",
        (
            "try:\n    from typing import Sequence\nexcept ImportError:\n    Sequence = list\n\n"
            "def pair() -> Sequence[int]:\n    return [1]\n"
        ),
        (
            "from collections.abc import Mapping\nfrom typing import Sequence as Mapping\n\n"
            "def pair() -> Mapping[int]:\n    return [1]\n"
        ),
        ABC_IMPORTS + "\ndef pair(flag: bool) -> Sequence[int]:\n    return [1] if flag else (1,)\n",
        ABC_IMPORTS + "\ndef pair(flag: bool) -> Sequence[int]:\n    return [1] if flag else build()\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    return list\n",
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    return build\n",
        (
            "from collections.abc import Mapping\nfrom types import MappingProxyType\n"
            "import types as MappingProxyType\n\n"
            "def pair() -> Mapping[str, int]:\n    return MappingProxyType({})\n"
        ),
        "from collections import deque as factory\n" + ABC_IMPORTS + "\ndef outer() -> None:\n"
        "    from collections import deque as factory\n\n    def replace() -> None:\n        nonlocal factory\n"
        "        factory = list\n\n    def items() -> Sequence[int]:\n        return factory([1])\n",
        "import builtins\n" + ABC_IMPORTS + "\nbuiltins.list = tuple\n\n"
        "def pair() -> Sequence[int]:\n    return builtins.list((1, 2))\n",
        "import types\n" + ABC_IMPORTS + "\ntypes.MappingProxyType = dict\n\n"
        "def pair() -> Mapping[str, int]:\n    return types.MappingProxyType({})\n",
        ABC_IMPORTS + "\ndef outer() -> None:\n    from collections import deque as factory\n\n    class Rebind:\n"
        "        nonlocal factory\n        factory = list\n\n    def items() -> Sequence[int]:\n"
        "        return factory([1])\n",
        ABC_IMPORTS + "\ndef items() -> Sequence[int]:\n    out = []\n\n    def inner(arg=(out := ())) -> None:\n"
        "        pass\n\n    return out\n",
    ],
)
def test_check_leaves_unproven_or_intentional_annotations_alone(source: str) -> None:
    assert _check(source) == []


@pytest.mark.parametrize(
    ("source", "reported"),
    [
        (ABC_IMPORTS + "\nclass Plain:\n    def items(self) -> Sequence[int]:\n        return [1]\n", True),
        (
            "from typing import Generic, TypeVar\n" + ABC_IMPORTS + "\nT = TypeVar('T')\n\nclass Box(Generic[T]):\n"
            "    def items(self) -> Sequence[T]:\n        return []\n\n    def more(self) -> Sequence[T]:\n"
            "        out = []\n        return out\n",
            True,
        ),
        (
            ABC_IMPORTS
            + "\nclass Plain:\n    def items(self) -> Sequence[int]:\n        '''Docs.'''\n        return []\n",
            False,
        ),
        (ABC_IMPORTS + "\nclass Plain:\n    def items(self) -> Mapping[str, int]:\n        return dict()\n", False),
        (
            ABC_IMPORTS + "\nclass Base:\n    def items(self) -> Sequence[int]:\n        return [1]\n\n"
            "class Child(Base):\n    def items(self) -> Sequence[int]:\n        return [2]\n",
            False,
        ),
        (
            ABC_IMPORTS + "\nclass Base:\n    def items(self) -> Sequence[int]:\n        return [1]\n\n"
            "class Child(Base):\n    pass\n\nclass GrandChild(Child):\n    items = None\n",
            False,
        ),
        (
            ABC_IMPORTS + "\nclass Base:\n    def other(self) -> None: ...\n\n"
            "class Child(Base):\n    def items(self) -> Sequence[int]:\n        return [2]\n",
            True,
        ),
        (
            ABC_IMPORTS + "\nclass Child(External):\n    def items(self) -> Sequence[int]:\n        return [2]\n",
            False,
        ),
        (
            ABC_IMPORTS + "\nclass Child(Base, Mixin):\n    def items(self) -> Sequence[int]:\n        return [2]\n\n"
            "class Base:\n    pass\n",
            False,
        ),
        (
            ABC_IMPORTS + "\nclass Child(make_base()):\n    def items(self) -> Sequence[int]:\n        return [2]\n",
            False,
        ),
        (
            ABC_IMPORTS + "\nclass Root:\n    pass\n\nclass Left(Root):\n    pass\n\nclass Right(Root):\n    pass\n\n"
            "class Leaf(Left, Right):\n    def items(self) -> Sequence[int]:\n        return [2]\n\n"
            "    def more(self) -> Sequence[int]:\n        return [3]\n",
            True,
        ),
        (
            ABC_IMPORTS + "\nclass Root:\n    def items(self) -> Sequence[int]:\n        return [2]\n\n"
            "class Left(Root):\n    pass\n\nclass Right(Root):\n    pass\n\nclass Leaf(Left, Right):\n    pass\n",
            True,
        ),
        (
            ABC_IMPORTS + "\nclass Base:\n    def items(self) -> Sequence[int]:\n        return [1]\n\n"
            "Alias = Base\n\nclass Child(Alias):\n    def items(self) -> Sequence[int]:\n        return (1,)\n",
            False,
        ),
        (
            ABC_IMPORTS + "\nclass Left:\n    def items(self) -> Sequence[int]:\n        return [1]\n\n"
            "class Right:\n    def items(self) -> Sequence[int]:\n        return (1,)\n\n"
            "class Both(Left, Right):\n    pass\n",
            False,
        ),
        (
            "from mixins import Mixin\n" + ABC_IMPORTS + "\nclass Base:\n    def items(self) -> Sequence[int]:\n"
            "        return [1]\n\nclass Child(Base, Mixin):\n    pass\n",
            False,
        ),
        (
            ABC_IMPORTS + "\nclass Child(metaclass=Meta):\n    def items(self) -> Sequence[int]:\n        return [2]\n",
            False,
        ),
        (
            ABC_IMPORTS + "\nclass Child(object):\n    @property\n    def items(self) -> Sequence[int]:\n"
            "        return [2]\n\n    @items.setter\n    def items(self, value: Sequence[int]) -> None:\n"
            "        pass\n",
            True,
        ),
    ],
)
def test_aggressive_level_also_checks_methods_without_known_overrides(source: str, *, reported: bool) -> None:
    assert _check(source) == []
    assert bool(_check(source, OverlyBroadReturnTypeLevel.AGGRESSIVE)) is reported


def test_message_mentions_none_returns() -> None:
    source = (
        ABC_IMPORTS
        + "\ndef pair(flag: bool) -> Sequence[int] | None:\n    if flag:\n        return None\n    return [1]\n"
    )

    (violation,) = _check(source)

    assert "the only type this function returns besides `None`; use `list[int]`" in violation.message


@pytest.mark.parametrize(
    "chain",
    [
        "".join(f"    a{index} = a{index - 1} if flag else a{index - 1}\n" for index in range(1, 60)),
        "".join(f"    a{index} = a{index - 1}\n" for index in range(1, 3000)),
    ],
    ids=["branching", "long"],
)
def test_variable_chains_are_proven_once(chain: str) -> None:
    last = chain.rsplit("\n", 2)[-2].split(" = ", maxsplit=1)[0].strip()
    source = (
        "from collections import deque\n" + ABC_IMPORTS + "\ndef pair(flag: bool) -> MutableSequence[int]:\n"
        f"    a0 = deque([1])\n{chain}    return {last}\n"
    )

    (violation,) = _check(source)

    assert _advice(violation.message) == "use a `deque` annotation"


def test_aggressive_level_handles_long_inheritance_chains() -> None:
    classes = "".join(
        f"class C{index}({'C' + str(index - 1) if index else 'object'}):\n"
        f"    def items{index}(self) -> Sequence[int]:\n        return [{index}]\n\n"
        for index in range(300)
    )

    assert len(_check(ABC_IMPORTS + classes, OverlyBroadReturnTypeLevel.AGGRESSIVE)) == 300


def test_violation_points_at_the_abstract_part_of_the_annotation() -> None:
    source = ABC_IMPORTS + "\ndef pair(label: str = 'é') -> Sequence[int] | None:\n    return [1]\n"

    (violation,) = _check(source)

    assert (violation.line, violation.col) == (3, source.splitlines()[2].index("Sequence"))
    assert violation.message == (
        "Return type `Sequence[int]` is broader than `list`, the only type this function returns; "
        "use `list[int]`. Or add '# pytriage: TR13' to suppress."
    )


@pytest.mark.parametrize(
    "source",
    [
        ABC_IMPORTS + "\ndef pair() -> Sequence[int]:  # pytriage: TR13\n    return [1]\n",
        ABC_IMPORTS + "\ndef pair(\n    value: int,\n) -> Sequence[int]:  # pytriage: TR13\n    return [value]\n",
        ABC_IMPORTS + "\ndef pair(  # pytriage: TR13\n    value: int,\n) -> Sequence[int]:\n    return [value]\n",
        ABC_IMPORTS + "\n# fmt: off\ndef pair() -> Sequence[int]:\n    return [1]\n# fmt: on\n",
    ],
    ids=["def-line", "annotation-line", "multi-line-def-line", "fmt-off"],
)
def test_inline_suppression(source: str) -> None:
    check_result = _check(source)

    assert list(check_result) == []
    assert len(check_result.suppression_usages) == (0 if "fmt: off" in source else 1)


def test_fix_declines_every_violation(tmp_path: Path) -> None:
    source = ABC_IMPORTS + "\ndef pair() -> Sequence[int]:\n    return [1]\n"
    filepath = tmp_path / "test.py"
    filepath.write_text(source)
    tree = ast.parse(source)
    check = OverlyBroadReturnTypeCheck()
    violations = check.check(filepath, tree, source)

    assert [violation.fixable for violation in violations] == [False]
    assert check.fix(filepath, violations, source, tree).outcomes == (FixOutcome.DECLINED,)
    assert filepath.read_text() == source


def test_cli_level_option_reaches_the_check(git_worktree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    filepath = git_worktree / "module.py"
    filepath.write_text(ABC_IMPORTS + "\nclass Plain:\n    def items(self) -> Sequence[int]:\n        return [1]\n")

    assert main(["--isolated", "--select=overly-broad-return-type", str(filepath)]) == 0
    assert (
        main(
            [
                "--isolated",
                "--select=overly-broad-return-type",
                "--overly-broad-return-type-level=aggressive",
                str(filepath),
            ]
        )
        == 1
    )
    assert "TR13" in capsys.readouterr().err
