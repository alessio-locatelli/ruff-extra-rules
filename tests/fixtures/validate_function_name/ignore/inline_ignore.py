"""Test fixture: Inline ignore comments."""


def get_users() -> list:  # pytriage: ignore=TRI004
    """Suppressed with inline comment."""
    with open("users.json") as f:
        return f.read()


def get_data():  # pytriage: ignore=TRI004
    """Also suppressed."""
    return []
