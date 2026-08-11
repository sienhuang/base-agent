"""Normalized failures raised at Provider Adapter boundaries."""

from __future__ import annotations


class ProviderError(RuntimeError):
    code = "provider_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class ProviderConfigurationError(ProviderError):
    code = "provider_configuration"


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication"


class ProviderInvalidRequestError(ProviderError):
    code = "provider_invalid_request"


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limit"
    retryable = True


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    retryable = True


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    retryable = True


class InvalidProviderResponseError(ProviderError):
    code = "invalid_provider_response"


class MissingProviderDependencyError(ImportError):
    """Raised when an optional Provider SDK was not installed."""


class CLIProviderError(ProviderError):
    """Base failure raised by a local command-line ModelProvider."""

    code = "cli_provider"

    def __init__(
        self,
        message: str,
        *,
        provider: str = "cli",
    ) -> None:
        super().__init__(message, provider=provider)


class CLIExecutableNotFoundError(CLIProviderError):
    """The configured local model CLI executable was not installed."""

    code = "cli_executable_not_found"


class CLIProcessError(CLIProviderError):
    """A local model CLI exited unsuccessfully or reported an error event."""

    code = "cli_process"


class CLIProcessTimeoutError(CLIProviderError):
    """A local model CLI exceeded its configured execution timeout."""

    code = "cli_process_timeout"
    retryable = True


class CLIOutputLimitError(CLIProviderError):
    """A local model CLI exceeded its bounded stdout or stderr allowance."""

    code = "cli_output_limit"
