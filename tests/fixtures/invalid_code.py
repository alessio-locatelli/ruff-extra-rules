"""Test fixture: Python code with meaningless variable names (should fail)."""


def process():
    """Process data."""
    data = fetch()
    result = transform(data)
    return result


def fetch():
    """Fetch data."""
    return None


def transform(data):
    """Transform data."""
    result = data
    return result


def calculate(x, y):
    """Calculate something."""
    data = x + y
    return data


def get_info(*, result=None):
    """Get info with keyword-only argument."""
    return result


def varargs_example(*data):
    """Example with *args using meaningless name."""
    return data


def kwargs_example(**result):
    """Example with **kwargs using meaningless name."""
    return result
