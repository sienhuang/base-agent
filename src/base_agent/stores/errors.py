"""Typed persistence errors shared by store adapters."""


class RunNotFoundError(LookupError):
    """Raised when a requested Run does not exist."""


class RunAlreadyExistsError(ValueError):
    """Raised when a store is asked to create the same Run twice."""


class RunNotCancellableError(RuntimeError):
    """Raised when cancellation is requested after a Run reached a terminal state."""


class ConversationNotFoundError(LookupError):
    """Raised when a requested Conversation does not exist."""


class ConversationAlreadyExistsError(ValueError):
    """Raised when a Conversation with the same ID already exists."""


class ConversationBusyError(RuntimeError):
    """Raised when a Conversation already has an active Run."""


class ConversationProfileMismatchError(ValueError):
    """Raised when an Agent profile does not own the Conversation."""


class ConversationTurnNotFoundError(LookupError):
    """Raised when a Conversation Turn cannot be found by Run ID."""


class CheckpointNotFoundError(LookupError):
    """Raised when a suspended Runtime checkpoint cannot be found or was already claimed."""


class AttachmentNotFoundError(LookupError):
    """Raised when an Attachment reference has no stored content."""


class ArtifactNotFoundError(LookupError):
    """Raised when an Artifact reference has no stored content."""
