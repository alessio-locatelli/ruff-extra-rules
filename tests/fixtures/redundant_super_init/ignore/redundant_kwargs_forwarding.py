"""Module with redundant kwargs forwarding, suppressed via inline ignore."""


class Base:
    """Base class that does not accept kwargs."""

    def __init__(self):
        """Initialize base class."""
        self.initialized = True


class Child(Base):
    """Child class that redundantly forwards kwargs."""

    def __init__(self, value, **kwargs):  # pytriage: ignore=TRI003
        """Initialize child."""
        self.value = value
        super().__init__(**kwargs)  # VIOLATION suppressed by inline ignore above
