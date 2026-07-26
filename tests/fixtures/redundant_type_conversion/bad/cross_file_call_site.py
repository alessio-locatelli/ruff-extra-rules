from pkg.callee import takes_iterable, takes_list


def caller(bar: list[int], names: list[str]) -> None:
    takes_list(list(bar))
    takes_iterable(list(names))
