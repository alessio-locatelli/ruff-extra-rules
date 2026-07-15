"""Module with properly placed inline comments."""


def example_function():
    """Function with inline comment on expression line."""
    result = function_call(arg1, arg2)  # Comment explaining the call
    return result


def another_example():
    """Function with complex expression."""
    value = (
        some_long_function_name(
            argument_one,
            argument_two,
            argument_three,  # This closes the function call
        )
    )
    return value
