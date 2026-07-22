"""Typed failures raised at optional Provider adapter boundaries."""


class MissingProviderDependencyError(ImportError):
    """Raised when an optional Provider SDK was not installed."""


class InvalidProviderResponseError(ValueError):
    """Raised when a Provider returns a response that cannot enter the core Runtime."""


class UnsupportedAttachmentError(ValueError):
    """Raised when a Provider adapter cannot safely map structured attachments."""


class UnsupportedMemoryError(ValueError):
    """Raised when a Provider adapter cannot safely map retrieved memories."""
