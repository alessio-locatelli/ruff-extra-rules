from __future__ import annotations

import ast

import pytest

from pre_commit_hooks.ast_checks.redundant_dict_get.candidates import find_candidates
from pre_commit_hooks.ast_checks.redundant_dict_get.local import ProofLevel, find_proofs


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        ("config = {'port': 5432}\nvalue = config.get('port')\n", [2]),
        (
            (
                "def f(config: dict[str, int], key: str) -> int | None:\n"
                "    if key in config:\n"
                "        return config.get(key)\n"
                "    return None\n"
            ),
            [3],
        ),
        (
            (
                "def f(config: dict[str, int], key: str) -> int | None:\n"
                "    if key not in config:\n"
                "        raise KeyError(key)\n"
                "    return config.get(key)\n"
            ),
            [4],
        ),
        (
            ("class Settings:\n    config = {'port': 5432}\n    port = config.get('port')\n"),
            [3],
        ),
        (
            "config = {'port': 5432}\nport: int\nvalue = config.get('port')\n",
            [3],
        ),
        (
            ("with open('config.json'):\n    config = {'port': 5432}\n    value = config.get('port')\n"),
            [3],
        ),
        (
            (
                "try:\n"
                "    config = {'port': 5432}\n"
                "    value = config.get('port')\n"
                "except OSError:\n"
                "    config = {'port': 5432}\n"
                "    value = config.get('port')\n"
                "finally:\n"
                "    config = {'port': 5432}\n"
                "    value = config.get('port')\n"
            ),
            [3, 6, 9],
        ),
    ],
)
def test_find_proofs_reports_the_supported_local_invariants(source: str, expected_lines: list[int]) -> None:
    tree = ast.parse(source)

    proofs = find_proofs(tree, find_candidates(tree), level=ProofLevel.AGGRESSIVE)

    assert [proof.candidate.call.lineno for proof in proofs] == expected_lines


@pytest.mark.parametrize(
    "source",
    [
        "config = {'port': 5432}\nconfig['host'] = 'localhost'\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nconfig['port'] += 1\nvalue = config.get('port')\n",
        "config = {'port': 5432}\ndel config['port']\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nconsume(config)\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nalias = config\nalias.pop('port')\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nholder.config = config\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nconfig, other = build()\nvalue = config.get('port')\n",
        (
            "def f(config: dict[str, int], key: str) -> int | None:\n"
            "    if key in config:\n"
            "        key = 'other'\n"
            "        return config.get(key)\n"
            "    return None\n"
        ),
        ("config = {'port': 5432}\nif condition:\n    config.pop('port')\nvalue = config.get('port')\n"),
        (
            "config = {'port': 5432}\n"
            "if key not in config:\n"
            "    raise KeyError(key)\n"
            "else:\n"
            "    config.pop(key)\n"
            "value = config.get(key)\n"
        ),
        "config = {'port': 5432}\nvalue = (config.pop('port'), config.get('port'))\n",
        "config = {'port': 5432}\nif config.pop('port'):\n    value = config.get('port')\n",
        ("config = {'port': 5432}\n@mutate(config)\ndef decorated() -> None:\n    pass\nvalue = config.get('port')\n"),
        ("config = {'port': 5432}\nclass Derived(mutate(config)):\n    pass\nvalue = config.get('port')\n"),
        (
            "config = {'port': 5432}\n"
            "match subject:\n"
            "    case _:\n"
            "        del config['port']\n"
            "        value = config.get('port')\n"
            "later_value = config.get('port')\n"
        ),
        "config = {'port': 5432}\nalias = (alias := config)\ndel alias['port']\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nremove()\nvalue = config.get('port')\n",
        ("def values() -> int | None:\n    config = {'port': 5432}\n    yield config\n    return config.get('port')\n"),
        (
            "def generic[dict](config: dict[str, int], key: str) -> int | None:\n"
            "    if key in config:\n"
            "        return config.get(key)\n"
            "    return None\n"
        ),
        (
            "def outer(dict: object) -> None:\n"
            "    def inner(config: dict[str, int], key: str) -> int | None:\n"
            "        if key in config:\n"
            "            return config.get(key)\n"
            "        return None\n"
        ),
        (
            "def f(config: dict[str, int], key: str) -> int | None:\n"
            "    if key in config:\n"
            "        return (key := 'other', config.get(key))[1]\n"
            "    return None\n"
        ),
        (
            "def f(config: dict[str, int], key: str) -> int | None:\n"
            "    if key in config and enabled:\n"
            "        return config.get(key)\n"
            "    return None\n"
        ),
        (
            "def f(config: dict[str, int]) -> int | None:\n"
            "    if 'port' in config:\n"
            "        return config.get('port')\n"
            "    return None\n"
        ),
        (
            "def f(config: dict[str, int], key: str) -> int | None:\n"
            "    if key == config:\n"
            "        return config.get(key)\n"
            "    return None\n"
        ),
        (
            "def f(config: dict[str, int], key: str) -> int | None:\n"
            "    if key in config:\n"
            "        return None\n"
            "    return config.get(key)\n"
        ),
        (
            "def f(config, key: str) -> int | None:\n"
            "    if key in config:\n"
            "        return config.get(key)\n"
            "    return None\n"
        ),
        (
            "dict = CustomMapping\n"
            "def f(config: dict[str, int], key: str) -> int | None:\n"
            "    if key in config:\n"
            "        return config.get(key)\n"
            "    return None\n"
        ),
        (
            "def f(config: dict[str, int]) -> list[int | None]:\n"
            "    config = {'port': 5432}\n"
            "    return [config.get('port') for _ in range(1)]\n"
        ),
        ("def f(*config: dict[str, int]) -> int | None:\n    return config.get('port')\n"),
        ("def f(**config: dict[str, int]) -> int | None:\n    return config.get('port')\n"),
        "config = {'port': 5432}\nconfig, *other = build()\nvalue = config.get('port')\n",
    ],
)
def test_find_proofs_rejects_mutation_escape_alias_and_non_dominating_cases(source: str) -> None:
    tree = ast.parse(source)

    assert find_proofs(tree, find_candidates(tree)) == []


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        (
            "config = {'port': 5432}\nalias = config\nvalue = alias.get('port')\n",
            [3],
        ),
        (
            "config = {'port': 5432}\nif key in config and other in config:\n    value = config.get(key)\n",
            [3],
        ),
        (
            (
                "if enabled:\n"
                "    config = {'port': 5432}\n"
                "else:\n"
                "    config = {'port': 5433}\n"
                "value = config.get('port')\n"
            ),
            [5],
        ),
        (
            "config = {'port': 5432}\nif True:\n    pass\nvalue = config.get('port')\n",
            [4],
        ),
        (
            (
                "required = {'port'}\n"
                "config = {'port': 5432}\n"
                "if required <= config.keys():\n"
                "    for key in required:\n"
                "        if key in config:\n"
                "            value = config.get(key)\n"
            ),
            [6],
        ),
        (
            (
                "required = {'port'}\n"
                "config = {'port': 5432}\n"
                "if required <= config.keys():\n"
                "    for key in required:\n"
                "        value = config.get(key)\n"
            ),
            [5],
        ),
        (
            (
                "required = {'port'}\n"
                "config = {'port': 5432}\n"
                "if not all(key in config for key in required):\n"
                "    raise ValueError\n"
                "for key in required:\n"
                "    if key in config:\n"
                "        value = config.get(key)\n"
            ),
            [7],
        ),
        (
            (
                "from typing import NotRequired, TypedDict\n"
                "class Settings(TypedDict):\n"
                "    present: int | None\n"
                "    absent: NotRequired[int]\n"
                "def read(settings: Settings) -> int | None:\n"
                "    first = settings.get('present')\n"
                "    second = settings.get('absent')\n"
            ),
            [6],
        ),
    ],
)
def test_find_proofs_reports_extended_conservative_proofs(source: str, expected_lines: list[int]) -> None:
    tree = ast.parse(source)

    proofs = find_proofs(tree, find_candidates(tree))

    assert [proof.candidate.call.lineno for proof in proofs] == expected_lines


@pytest.mark.parametrize(
    "source",
    [
        "config = {'port': 5432}\nalias = config\ndel alias['port']\nvalue = config.get('port')\n",
        "config = {'a': 1}\nalias = config\nconfig = {'b': 2}\nvalue = alias.get('b')\n",
        (
            "from typing import TypedDict\n"
            "total = False\n"
            "class Settings(TypedDict, total=total):\n"
            "    port: int\n"
            "def read(settings: Settings) -> int | None:\n"
            "    return settings.get('port')\n"
        ),
        (
            "required = {'port'}\n"
            "config = {'port': 5432}\n"
            "if not required <= config.keys():\n"
            "    raise ValueError\n"
            "required |= {'missing'}\n"
            "for key in required:\n"
            "    value = config.get(key)\n"
        ),
        "config = {'port': 5432}\nif 'port' in config and flag:\n    value = config.get('port')\n",
        ("config = {'port': 5432}\nfor _ in items:\n    del config['port']\nvalue = config.get('port')\n"),
        ("config = {'port': 5432}\nfor _ in items:\n    del config['port']\n    break\nvalue = config.get('port')\n"),
        ("config = {}\nmatch subject:\n    case 1:\n        config = {'port': 5432}\nvalue = config.get('port')\n"),
        ("config = {}\nwith manager:\n    config = {'port': 5432}\nvalue = config.get('port')\n"),
        "config = {'port': 5432}\nfrom settings import config\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nfrom settings import *\nvalue = config.get('port')\n",
        "config = {'port': 5432}\ndel config\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nholder.value += config\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nvalue = (config := {}, config.get('port'))[1]\n",
        "config = {'port': 5432}\n(config := {})\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nif consume():\n    pass\nvalue = config.get('port')\n",
        ("config = {'port': 5432}\nmatch consume():\n    case _:\n        pass\nvalue = config.get('port')\n"),
        ("config = {'port': 5432}\nmatch subject:\n    case 1 as config:\n        pass\nvalue = config.get('port')\n"),
        (
            "config = {'port': 5432}\n"
            "match subject:\n"
            "    case _ as config if enabled:\n"
            "        pass\n"
            "value = config.get('port')\n"
        ),
        (
            "config = {'port': 5432}\n"
            "try:\n"
            "    pass\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    pass\n"
            "value = config.get('port')\n"
        ),
        "for _ in values:\n    break\nelse:\n    pass\n",
        "def read():\n    for _ in values:\n        return\n",
        "import os\n",
        ("config = {'port': 5432}\nfor _ in values:\n    consume()\n    value = config.get('port')\n"),
        ("config = {'port': 5432}\nfor _ in values:\n    other += 1\nvalue = config.get('port')\n"),
        (
            "def read():\n"
            "    config = {'port': 5432}\n"
            "    for _ in values:\n"
            "        yield None\n"
            "    return config.get('port')\n"
        ),
        ("config = {'port': 5432}\nfor _ in values:\n    config = {}\nvalue = config.get('port')\n"),
        ("config = {'port': 5432}\nfor _ in values:\n    from settings import config\nvalue = config.get('port')\n"),
        ("config = {'port': 5432}\nfor _ in values:\n    def config():\n        pass\nvalue = config.get('port')\n"),
        (
            "from typing import TypedDict\n"
            "class Settings(TypedDict):\n"
            "    port: int\n"
            "def read(settings: other.Settings) -> int | None:\n"
            "    return settings.get('port')\n"
        ),
        (
            "required = {'port'}\n"
            "alias = required\n"
            "config = {'port': 5432}\n"
            "if not required <= config.keys():\n"
            "    raise ValueError\n"
            "alias |= {'missing'}\n"
            "if key in required:\n"
            "    value = config.get(key)\n"
        ),
        "config = {'port': 5432}\nwhile enabled:\n    value = config.get('port')\n",
        "config = {'port': 5432}\nfor key in unknown:\n    value = config.get('port')\nelse:\n    pass\n",
        "config = {'port': 5432}\nfor key in range(1):\n    value = config.get('port')\n",
        "config = {'port': 5432}\nconfig['port'] += 1\nvalue = config.get('port')\n",
        "required = {'port'}\nconfig = {'port': 5432}\nif required <= config.values():\n    pass\n",
        (
            "required = {'port'}\n"
            "config = {'port': 5432}\n"
            "if all(key in config for key in required if enabled):\n"
            "    pass\n"
        ),
        "import typing\nclass Settings(typing.TypedDict):\n    port: typing.Required[int]\n",
        "def read(config: module.Settings) -> None:\n    pass\n",
        "from typing import List\n",
        "def read() -> None:\n    if enabled:\n        return\n    raise ValueError\n",
        "def read() -> None:\n    if enabled:\n        return\n    else:\n        raise ValueError\n",
        (
            "def read() -> None:\n"
            "    try:\n"
            "        return\n"
            "    except ValueError:\n"
            "        return\n"
            "    finally:\n"
            "        pass\n"
        ),
        "counter = 0\ncounter += 1\n",
        "config = {'port': 5432}\ndel config[consume()]\n",
        "required = unknown\nconfig = {'port': 5432}\nif required <= config.keys():\n    pass\n",
        "config = {'port': 5432}\nif key in config or enabled:\n    pass\n",
        (
            "first = {'first'}\n"
            "second = {'second'}\n"
            "config = {'first': 1, 'second': 2}\n"
            "if not first <= config.keys():\n"
            "    raise ValueError\n"
            "if not second <= config.keys():\n"
            "    raise ValueError\n"
            "for key in first:\n"
            "    pass\n"
        ),
        "config = {'port': 5432}\nif all((key in config for key, other in required)):\n    pass\n",
        "config = {'port': 5432}\nif all(0 for key in required):\n    pass\n",
        "pass\n" * 1_001,
    ],
)
def test_find_proofs_rejects_reviewed_false_positive_paths(source: str) -> None:
    tree = ast.parse(source)

    assert find_proofs(tree, find_candidates(tree)) == []
