from __future__ import annotations

import ast

import pytest

from pre_commit_hooks.ast_checks.redundant_dict_get.candidates import find_candidates


@pytest.mark.parametrize(
    (
        "source",
        "expected",
    ),
    [
        ("value = config.get('port')\n", [("config", "port", None)]),
        ("value = config.get(key)\n", [("config", None, "key")]),
        ("value = config.get('port', None)\n", []),
        ("value = config.get(key='port')\n", []),
        ("value = factory().get('port')\n", []),
        ("value = config.get(prefix + 'port')\n", []),
    ],
)
def test_find_candidates_accepts_only_the_supported_get_shapes(
    source: str, expected: list[tuple[str, str | None, str | None]]
) -> None:
    candidates = find_candidates(ast.parse(source))

    assert [(candidate.receiver, candidate.literal_key, candidate.name_key) for candidate in candidates] == expected
