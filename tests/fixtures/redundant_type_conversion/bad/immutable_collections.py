from collections.abc import KeysView

_NAMES = frozenset({("abc", "ABC"), ("typing", "Protocol")})


def known(names: frozenset[tuple[str, str]]) -> bool:
    return bool(names & _NAMES)


def count(values: list[int]) -> int:
    frozen_values = tuple(values)
    return sum(frozen_values)


def redundant(names: frozenset[str], values: tuple[int, ...]) -> int:
    same_names = frozenset(names)
    same_values = tuple(values)
    return len(same_names) + len(same_values)


def retains_name(data: dict[str, int], keys: KeysView[str]) -> bool:
    snapshot = frozenset(keys)
    data.clear()
    return "name" in snapshot


def count_mixed(values: tuple[int, ...] | list[int]) -> int:
    snapshot = tuple(values)
    return sum(snapshot)
