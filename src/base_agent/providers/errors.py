"""Typed failures raised at optional Provider adapter boundaries."""


class MissingProviderDependencyError(ImportError):
    """Raised when an optional Provider SDK was not installed."""


class InvalidProviderResponseError(ValueError):
    """Raised when a Provider returns a response that cannot enter the core Runtime."""


class UnsupportedAttachmentError(ValueError):
    """Raised when a Provider adapter cannot safely map structured attachments."""


class UnsupportedMemoryError(ValueError):
    """Raised when a Provider adapter cannot safely map retrieved memories."""


class CLIProviderError(RuntimeError):
    """Base failure raised by a local command-line ModelProvider."""


class CLIExecutableNotFoundError(CLIProviderError):
    """The configured local model CLI executable was not installed."""


class CLIProcessError(CLIProviderError):
    """A local model CLI exited unsuccessfully or reported an error event."""


class CLIProcessTimeoutError(CLIProviderError):
    """A local model CLI exceeded its configured execution timeout."""


class CLIOutputLimitError(CLIProviderError):
    """A local model CLI exceeded its bounded stdout or stderr allowance."""
