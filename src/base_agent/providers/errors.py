"""Typed failures raised at optional Provider adapter boundaries."""


class MissingProviderDependencyError(ImportError):
    """Raised when an optional Provider SDK was not installed."""


class InvalidProviderResponseError(ValueError):
    """Raised when a Provider returns a response that cannot enter the core Runtime."""
