"""Environment configuration for the real DSH FastAPI service."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class DshAgentConfigError(ValueError):
    """Raised when the configured Agent registry cannot be loaded."""


class DshAgentDefinition(BaseModel):
    """One Agent entry in the FastAPI service registry."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    instructions: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class DshAgentRegistry(BaseModel):
    """Declarative configuration for all Agents hosted by one service process."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    default_agent: str = Field(min_length=1)
    agents: dict[str, DshAgentDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> Self:
        blank_names = [name for name in self.agents if not name.strip()]
        if blank_names:
            raise ValueError("Agent names must not be blank")
        if self.default_agent not in self.agents:
            raise ValueError(f"default_agent '{self.default_agent}' is not present in agents")
        return self


def load_dsh_agent_registry(path: Path) -> DshAgentRegistry:
    """Load and strictly validate an Agent registry YAML file."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DshAgentConfigError(f"cannot load Agent config '{path}': {exc}") from exc
    try:
        return DshAgentRegistry.model_validate(document)
    except ValidationError as exc:
        raise DshAgentConfigError(f"invalid Agent config '{path}': {exc}") from exc


@dataclass(frozen=True, slots=True)
class DshFastApiSettings:
    state_dir: Path
    workspace: Path
    host: str = "127.0.0.1"
    port: int = 8000
    agent_config_path: Path | None = None
    agent_name: str = "assistant"
    instructions: str = "You are a helpful assistant."
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    context_policy: str = "compacting"
    compaction_threshold_ratio: float = 0.8
    compaction_retain_ratio: float = 0.16
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    dsh_bin: str | None = None
    initialize_timeout_seconds: float = 30.0
    request_timeout_seconds: float | None = None
    shutdown_timeout_seconds: float | None = 5.0

    def __post_init__(self) -> None:
        for value, name in (
            (self.agent_name, "agent_name"),
            (self.instructions, "instructions"),
            (self.provider, "provider"),
            (self.model, "model"),
            (self.host, "host"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if not 1 <= self.port <= 65_535:
            raise ValueError("port must be between 1 and 65535")
        if self.context_policy not in {"keep-all", "compacting"}:
            raise ValueError("context_policy must be 'keep-all' or 'compacting'")
        if not self.workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {self.workspace}")
        if self.agent_config_path is not None and not self.agent_config_path.is_file():
            raise ValueError(f"Agent config must be an existing file: {self.agent_config_path}")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
    ) -> DshFastApiSettings:
        if environ is None:
            _load_service_environment(env_file)
            values: Mapping[str, str] = os.environ
        else:
            values = environ
        workspace = Path(values.get("BASE_AGENT_WORKSPACE", Path.cwd())).expanduser().resolve()
        state_dir = (
            Path(values.get("BASE_AGENT_STATE_DIR", workspace / ".base-agent" / "state"))
            .expanduser()
            .resolve()
        )
        return cls(
            state_dir=state_dir,
            workspace=workspace,
            host=values.get("BASE_AGENT_HOST", "127.0.0.1"),
            port=_int(values, "BASE_AGENT_PORT", 8000),
            agent_config_path=_optional_path(values, "BASE_AGENT_CONFIG"),
            agent_name=values.get("BASE_AGENT_NAME", "assistant"),
            instructions=values.get(
                "BASE_AGENT_INSTRUCTIONS",
                "You are a helpful assistant.",
            ),
            provider=values.get("DSH_PROVIDER", "deepseek-official"),
            model=values.get("DSH_MODEL", "deepseek-v4-flash"),
            context_policy=values.get("BASE_AGENT_CONTEXT_POLICY", "compacting"),
            compaction_threshold_ratio=_float(
                values,
                "BASE_AGENT_COMPACTION_THRESHOLD_RATIO",
                0.8,
            ),
            compaction_retain_ratio=_float(
                values,
                "BASE_AGENT_COMPACTION_RETAIN_RATIO",
                0.16,
            ),
            reasoning_effort=_optional(values, "DSH_REASONING_EFFORT"),
            max_tokens=_optional_int(values, "DSH_MAX_TOKENS"),
            dsh_bin=_optional(values, "DSH_BIN"),
            initialize_timeout_seconds=_float(
                values,
                "DSH_INITIALIZE_TIMEOUT_SECONDS",
                30.0,
            ),
            request_timeout_seconds=_optional_float(
                values,
                "DSH_REQUEST_TIMEOUT_SECONDS",
            ),
            shutdown_timeout_seconds=_optional_float(
                values,
                "DSH_SHUTDOWN_TIMEOUT_SECONDS",
                default=5.0,
            ),
        )


def _load_service_environment(env_file: str | Path | None) -> None:
    configured_path = env_file or os.environ.get("BASE_AGENT_ENV_FILE") or ".env"
    load_dotenv(dotenv_path=Path(configured_path).expanduser(), override=False)


def _optional(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    return value if value is not None and value.strip() else None


def _optional_path(values: Mapping[str, str], name: str) -> Path | None:
    value = _optional(values, name)
    return None if value is None else Path(value).expanduser().resolve()


def _float(values: Mapping[str, str], name: str, default: float) -> float:
    value = values.get(name)
    return default if value is None else float(value)


def _int(values: Mapping[str, str], name: str, default: int) -> int:
    value = values.get(name)
    return default if value is None else int(value)


def _optional_float(
    values: Mapping[str, str],
    name: str,
    *,
    default: float | None = None,
) -> float | None:
    value = values.get(name)
    return default if value is None or not value.strip() else float(value)


def _optional_int(values: Mapping[str, str], name: str) -> int | None:
    value = values.get(name)
    return None if value is None or not value.strip() else int(value)
