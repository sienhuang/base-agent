"""Typed persistence errors shared by store adapters."""


class RunNotFoundError(LookupError):
    """Raised when a requested Run does not exist."""


class RunAlreadyExistsError(ValueError):
    """Raised when a store is asked to create the same Run twice."""


class RunNotCancellableError(RuntimeError):
    """Raised when cancellation is requested after a Run reached a terminal state."""


class CheckpointNotFoundError(LookupError):
    """Raised when a suspended Runtime checkpoint cannot be found or was already claimed."""


class AttachmentNotFoundError(LookupError):
    """Raised when an Attachment reference has no stored content."""


class ArtifactNotFoundError(LookupError):
    """Raised when an Artifact reference has no stored content."""
