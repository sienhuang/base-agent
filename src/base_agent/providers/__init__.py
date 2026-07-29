"""Model provider extension points."""

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
    UnsupportedAttachmentError,
    UnsupportedMemoryError,
)
from base_agent.providers.openai_chat import OpenAIChatClient, OpenAIChatProvider
from base_agent.providers.protocol import ModelProvider

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
    "OpenAIChatClient",
    "OpenAIChatProvider",
    "UnsupportedAttachmentError",
    "UnsupportedMemoryError",
    "run_cli_process",
]
