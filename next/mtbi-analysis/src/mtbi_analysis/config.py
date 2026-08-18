"""Environment-backed application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values

StructuredOutputMode = Literal["json-schema", "json-object", "prompt"]
ProviderName = Literal["codex-cli", "openai-compatible"]

_APPLICATION_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATH = _APPLICATION_ROOT / ".env"
_STRUCTURED_OUTPUT_MODES = frozenset({"json-schema", "json-object", "prompt"})
_PROVIDER_NAMES = frozenset({"codex-cli", "openai-compatible"})


@dataclass(frozen=True, slots=True)
class Settings:
    provider: ProviderName = "codex-cli"
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    structured_output_mode: StructuredOutputMode = "prompt"
    cli_executable: str = "codex"
    cli_timeout_seconds: float = 300.0
    cli_max_output_bytes: int = 4 * 1024 * 1024
    max_tool_observation_bytes: int = 32 * 1024
    max_run_observation_bytes: int = 512 * 1024
    mtbi_cli_bin: str = "mtbi-cli"
    workspace: Path = _APPLICATION_ROOT / "workspace"
    mtbi_cli_timeout_seconds: float = 600.0
    max_artifact_bytes: int = 10 * 1024 * 1024
    max_csv_rows: int = 100_000

    def __post_init__(self) -> None:
        if not self.cli_executable.strip():
            raise ValueError("cli_executable must not be blank")
        if self.cli_timeout_seconds <= 0:
            raise ValueError("cli_timeout_seconds must be greater than zero")
        if self.cli_max_output_bytes < 1:
            raise ValueError("cli_max_output_bytes must be positive")
        if self.max_tool_observation_bytes < 1_024:
            raise ValueError("max_tool_observation_bytes must be at least 1024")
        if self.max_run_observation_bytes < self.max_tool_observation_bytes:
            raise ValueError(
                "max_run_observation_bytes must be at least "
                "max_tool_observation_bytes"
            )
        if not self.mtbi_cli_bin.strip():
            raise ValueError("mtbi_cli_bin must not be blank")
        if self.mtbi_cli_timeout_seconds <= 0:
            raise ValueError("mtbi_cli_timeout_seconds must be greater than zero")
        if self.max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        if self.max_csv_rows < 1:
            raise ValueError("max_csv_rows must be positive")

    @classmethod
    def from_env(cls) -> Settings:
        file_values = dotenv_values(_DOTENV_PATH)
        raw_provider = (
            _env_value("AGENT_PROVIDER", file_values) or "codex-cli"
        ).strip().lower()
        if raw_provider not in _PROVIDER_NAMES:
            raise ValueError(
                "AGENT_PROVIDER must be codex-cli or openai-compatible"
            )
        raw_mode = (
            _env_value("MTBI_STRUCTURED_OUTPUT_MODE", file_values) or "prompt"
        ).strip().lower()
        if raw_mode not in _STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                "MTBI_STRUCTURED_OUTPUT_MODE must be json-schema, json-object, or prompt"
            )

        workspace_value = _env_value("MTBI_WORKSPACE", file_values) or "workspace"
        workspace = Path(workspace_value).expanduser()
        if not workspace.is_absolute():
            workspace = _APPLICATION_ROOT / workspace

        try:
            timeout = float(
                _env_value("MTBI_CLI_TIMEOUT_SECONDS", file_values) or "600"
            )
            max_bytes = int(
                _env_value("MTBI_MAX_ARTIFACT_BYTES", file_values)
                or str(10 * 1024 * 1024)
            )
            max_rows = int(
                _env_value("MTBI_MAX_CSV_ROWS", file_values) or "100000"
            )
            cli_timeout = float(
                _env_value("AGENT_CLI_TIMEOUT_SECONDS", file_values) or "300"
            )
            cli_max_output = int(
                _env_value("AGENT_CLI_MAX_OUTPUT_BYTES", file_values)
                or str(4 * 1024 * 1024)
            )
            max_tool_observation = int(
                _env_value("AGENT_MAX_TOOL_OBSERVATION_BYTES", file_values)
                or str(32 * 1024)
            )
            max_run_observation = int(
                _env_value("AGENT_MAX_RUN_OBSERVATION_BYTES", file_values)
                or str(512 * 1024)
            )
        except ValueError as exc:
            raise ValueError("CLI and MTBI numeric limits must contain valid numbers") from exc

        return cls(
            provider=cast(ProviderName, raw_provider),
            model=(
                _optional_env("AGENT_MODEL", file_values)
                or (
                    _optional_env("OPENAI_MODEL", file_values)
                    if raw_provider == "openai-compatible"
                    else None
                )
            ),
            api_key=_optional_env("OPENAI_API_KEY", file_values),
            base_url=_optional_env("OPENAI_BASE_URL", file_values),
            structured_output_mode=cast(StructuredOutputMode, raw_mode),
            cli_executable=(
                _env_value("AGENT_CLI_EXECUTABLE", file_values) or "codex"
            ).strip(),
            cli_timeout_seconds=cli_timeout,
            cli_max_output_bytes=cli_max_output,
            max_tool_observation_bytes=max_tool_observation,
            max_run_observation_bytes=max_run_observation,
            mtbi_cli_bin=(
                _env_value("MTBI_CLI_BIN", file_values) or "mtbi-cli"
            ).strip(),
            workspace=workspace.resolve(),
            mtbi_cli_timeout_seconds=timeout,
            max_artifact_bytes=max_bytes,
            max_csv_rows=max_rows,
        )


def _env_value(
    name: str,
    file_values: Mapping[str, str | None],
) -> str | None:
    return os.environ[name] if name in os.environ else file_values.get(name)


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
