"""Test fixture: Python code with meaningless names suppressed by ignore comments."""


def legacy_code():
    """Legacy code with necessary suppressions."""
    # New code - should use descriptive names
    user_records = fetch_users()

    # Legacy code - suppressed because refactoring is risky
    data = transform(user_records)  # maintainability: ignore[meaningless-variable-name]
    result = validate(data)  # MAINTAINABILITY: IGNORE[MEANINGLESS-VARIABLE-NAME]

    return result


def fetch_users():
    """Fetch users."""
    return []


def transform(records):
    """Transform records."""
    return records


def validate(records):
    """Validate records."""
    return records


def test_parser():
    """Test function with suppressed violation."""
    data = "test input"  # maintainability: ignore[meaningless-variable-name]
    return data


def multiple_suppressions(x, y):
    """Multiple variables on one line with suppression."""
    data, result = x, y  # maintainability: ignore[meaningless-variable-name]
    return data + result
