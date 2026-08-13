"""Provider extension boundary."""

from base_agent.providers.cli import (
    ClaudeCLIProvider,
    CLIModelProvider,
    CLIProcessOutput,
    CLIProcessRunner,
    CodexCLIProvider,
    run_cli_process,
)
from base_agent.providers.errors import (
    CLIExecutableNotFoundError,
    CLIOutputLimitError,
    CLIProcessError,
    CLIProcessTimeoutError,
    CLIProviderError,
    InvalidProviderResponseError,
    MissingProviderDependencyError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from base_agent.providers.models import ProviderCapabilities, ProviderInfo
from base_agent.providers.openai_chat import (
    OpenAIChatProvider,
    OpenAIStructuredOutputMode,
)
from base_agent.providers.protocol import ModelProvider, StreamingModelProvider

__all__ = [
    "CLIExecutableNotFoundError",
    "CLIModelProvider",
    "CLIOutputLimitError",
    "CLIProcessError",
    "CLIProcessOutput",
    "CLIProcessRunner",
    "CLIProcessTimeoutError",
    "CLIProviderError",
    "ClaudeCLIProvider",
    "CodexCLIProvider",
    "InvalidProviderResponseError",
    "MissingProviderDependencyError",
    "ModelProvider",
    "OpenAIChatProvider",
    "OpenAIStructuredOutputMode",
    "ProviderAuthenticationError",
    "ProviderCapabilities",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderInfo",
    "ProviderInvalidRequestError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "StreamingModelProvider",
    "run_cli_process",
]
