"""Test fixture: Inline ignore comments."""


def get_users() -> list:  # pytriage: TR4
    """Suppressed with inline comment."""
    with open("users.json") as f:
        return f.read()


def get_data():  # pytriage: TR4
    """Also suppressed."""
    return []
