from __future__ import annotations

import ast

import pytest

from pre_commit_hooks.ast_checks.redundant_dict_get.candidates import find_candidates
from pre_commit_hooks.ast_checks.redundant_dict_get.local import find_proofs


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

    proofs = find_proofs(tree, find_candidates(tree))

    assert [proof.candidate.call.lineno for proof in proofs] == expected_lines


@pytest.mark.parametrize(
    "source",
    [
        "config = {'port': 5432}\nconfig['host'] = 'localhost'\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nconfig['port'] += 1\nvalue = config.get('port')\n",
        "config = {'port': 5432}\ndel config['port']\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nconsume(config)\nvalue = config.get('port')\n",
        "config = {'port': 5432}\nalias = config\nvalue = alias.get('port')\n",
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
        (
            "def f() -> int | None:\n"
            "    config = {'port': 5432}\n"
            "    for _ in range(1):\n"
            "        return config.get('port')\n"
            "    return None\n"
        ),
        ("def f(*config: dict[str, int]) -> int | None:\n    return config.get('port')\n"),
        ("def f(**config: dict[str, int]) -> int | None:\n    return config.get('port')\n"),
        "config = {'port': 5432}\nconfig, *other = build()\nvalue = config.get('port')\n",
    ],
)
def test_find_proofs_rejects_mutation_escape_alias_and_non_dominating_cases(source: str) -> None:
    tree = ast.parse(source)

    assert find_proofs(tree, find_candidates(tree)) == []
