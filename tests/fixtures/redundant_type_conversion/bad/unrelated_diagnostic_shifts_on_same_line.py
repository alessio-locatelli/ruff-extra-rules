def takes_two(a: str, b: int) -> None:
    return None


def caller(x: str) -> None:
    takes_two(str(x), "not-an-int")
