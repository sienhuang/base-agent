"""Environment-backed application configuration without secret side effects."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, cast

ProviderName = Literal["offline", "openai", "codex-cli", "claude-cli"]
_PROVIDER_NAMES = frozenset({"offline", "openai", "codex-cli", "claude-cli"})
_CLI_PROVIDERS = frozenset({"codex-cli", "claude-cli"})


@dataclass(frozen=True, slots=True)
class Settings:
    provider: ProviderName = "offline"
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    cli_executable: str | None = None
    cli_timeout_seconds: float = 300.0
    cli_max_output_bytes: int = 4 * 1024 * 1024

    @classmethod
    def from_env(cls, *, provider: ProviderName | None = None) -> Settings:
        raw_provider = (
            provider or os.getenv("AGENT_PROVIDER", "offline").strip().lower()
        )
        if raw_provider not in _PROVIDER_NAMES:
            raise ValueError(
                "AGENT_PROVIDER must be offline, openai, codex-cli, or claude-cli"
            )
        resolved_provider = cast(ProviderName, raw_provider)
        configured_model = os.getenv("AGENT_MODEL")
        if configured_model is None:
            model = None if resolved_provider in _CLI_PROVIDERS else "gpt-4.1-mini"
        else:
            model = configured_model.strip()
            if not model:
                raise ValueError("AGENT_MODEL must not be blank")
        cli_executable = os.getenv("AGENT_CLI_EXECUTABLE")
        if cli_executable is not None and not cli_executable.strip():
            raise ValueError("AGENT_CLI_EXECUTABLE must not be blank")
        try:
            cli_timeout_seconds = float(
                os.getenv("AGENT_CLI_TIMEOUT_SECONDS", "300")
            )
            cli_max_output_bytes = int(
                os.getenv("AGENT_CLI_MAX_OUTPUT_BYTES", str(4 * 1024 * 1024))
            )
        except ValueError as exc:
            raise ValueError(
                "CLI timeout and output limit settings must be numeric"
            ) from exc
        if cli_timeout_seconds <= 0:
            raise ValueError("AGENT_CLI_TIMEOUT_SECONDS must be greater than zero")
        if cli_max_output_bytes < 1:
            raise ValueError("AGENT_CLI_MAX_OUTPUT_BYTES must be greater than zero")
        return cls(
            provider=resolved_provider,
            model=model,
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            cli_executable=cli_executable.strip() if cli_executable else None,
            cli_timeout_seconds=cli_timeout_seconds,
            cli_max_output_bytes=cli_max_output_bytes,
        )
