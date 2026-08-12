"""Small environment-backed configuration for Provider selection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values

ProviderName = Literal[
    "offline",
    "openai-compatible",
    "codex-cli",
    "claude-cli",
]

_PROVIDER_NAMES = frozenset(
    {
        "offline",
        "openai-compatible",
        "codex-cli",
        "claude-cli",
    }
)

_DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"


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
    def from_env(
        cls,
        *,
        provider: ProviderName | None = None,
    ) -> Settings:
        file_values = dotenv_values(_DOTENV_PATH)
        provider_value = _env_value("AGENT_PROVIDER", file_values)
        raw_provider = provider or (
            "offline" if provider_value is None else provider_value.strip().lower()
        )
        if raw_provider not in _PROVIDER_NAMES:
            raise ValueError(
                "AGENT_PROVIDER must be offline, openai-compatible, codex-cli, or claude-cli"
            )
        resolved_provider = cast(ProviderName, raw_provider)
        model = _optional_env("AGENT_MODEL", file_values)
        if model is None and resolved_provider == "openai-compatible":
            model = _optional_env("OPENAI_MODEL", file_values)

        try:
            timeout_seconds = float(
                _env_value("AGENT_CLI_TIMEOUT_SECONDS", file_values) or "300"
            )
            max_output_bytes = int(
                _env_value("AGENT_CLI_MAX_OUTPUT_BYTES", file_values)
                or str(4 * 1024 * 1024)
            )
        except ValueError as exc:
            raise ValueError("CLI timeout and output limit settings must be numeric") from exc
        if timeout_seconds <= 0:
            raise ValueError("AGENT_CLI_TIMEOUT_SECONDS must be greater than zero")
        if max_output_bytes < 1:
            raise ValueError("AGENT_CLI_MAX_OUTPUT_BYTES must be greater than zero")

        return cls(
            provider=resolved_provider,
            model=model,
            api_key=_optional_env("OPENAI_API_KEY", file_values),
            base_url=_optional_env("OPENAI_BASE_URL", file_values),
            cli_executable=_optional_env("AGENT_CLI_EXECUTABLE", file_values),
            cli_timeout_seconds=timeout_seconds,
            cli_max_output_bytes=max_output_bytes,
        )


def _env_value(
    name: str,
    file_values: Mapping[str, str | None],
) -> str | None:
    if name in os.environ:
        return os.environ[name]
    return file_values.get(name)


def _optional_env(
    name: str,
    file_values: Mapping[str, str | None],
) -> str | None:
    value = _env_value(name, file_values)
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be blank")
    return normalized
