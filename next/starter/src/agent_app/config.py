"""Small environment-backed configuration for Provider selection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values

ProviderName = Literal[
    "offline",
    "openai-compatible",
    "codex-cli",
    "claude-cli",
]
StructuredOutputMode = Literal[
    "json-schema",
    "json-object",
    "prompt",
]
PlanReactRole = Literal["planner", "executor"]

_PROVIDER_NAMES = frozenset(
    {
        "offline",
        "openai-compatible",
        "codex-cli",
        "claude-cli",
    }
)
_STRUCTURED_OUTPUT_MODES = frozenset(
    {
        "json-schema",
        "json-object",
        "prompt",
    }
)

_APPLICATION_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATH = _APPLICATION_ROOT / ".env"


@dataclass(frozen=True, slots=True)
class Settings:
    provider: ProviderName = "offline"
    model: str | None = None
    plan_react_planner_model: str | None = None
    plan_react_executor_model: str | None = None
    plan_react_planner_api_key: str | None = None
    plan_react_executor_api_key: str | None = None
    plan_react_planner_base_url: str | None = None
    plan_react_executor_base_url: str | None = None
    plan_react_planner_structured_output_mode: StructuredOutputMode | None = None
    plan_react_executor_structured_output_mode: StructuredOutputMode | None = None
    api_key: str | None = None
    base_url: str | None = None
    structured_output_mode: StructuredOutputMode = "prompt"
    cli_executable: str | None = None
    cli_timeout_seconds: float = 300.0
    cli_max_output_bytes: int = 4 * 1024 * 1024
    artifact_root: Path = _APPLICATION_ROOT / ".artifacts"

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
        plan_react_planner_model = _optional_env(
            "PLAN_REACT_PLANNER_MODEL",
            file_values,
        )
        plan_react_executor_model = _optional_env(
            "PLAN_REACT_EXECUTOR_MODEL",
            file_values,
        )
        plan_react_planner_api_key = _optional_env(
            "PLAN_REACT_PLANNER_API_KEY",
            file_values,
        )
        plan_react_executor_api_key = _optional_env(
            "PLAN_REACT_EXECUTOR_API_KEY",
            file_values,
        )
        plan_react_planner_base_url = _optional_env(
            "PLAN_REACT_PLANNER_BASE_URL",
            file_values,
        )
        plan_react_executor_base_url = _optional_env(
            "PLAN_REACT_EXECUTOR_BASE_URL",
            file_values,
        )

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

        raw_structured_output_mode = (
            _env_value("AGENT_STRUCTURED_OUTPUT_MODE", file_values) or "prompt"
        ).strip().lower()
        if raw_structured_output_mode not in _STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                "AGENT_STRUCTURED_OUTPUT_MODE must be json-schema, json-object, or prompt"
            )
        planner_structured_output_mode = _optional_structured_output_mode(
            "PLAN_REACT_PLANNER_STRUCTURED_OUTPUT_MODE",
            file_values,
        )
        executor_structured_output_mode = _optional_structured_output_mode(
            "PLAN_REACT_EXECUTOR_STRUCTURED_OUTPUT_MODE",
            file_values,
        )
        artifact_root_value = _env_value("AGENT_ARTIFACT_ROOT", file_values) or ".artifacts"
        artifact_root = Path(artifact_root_value).expanduser()
        if not artifact_root.is_absolute():
            artifact_root = _APPLICATION_ROOT / artifact_root

        return cls(
            provider=resolved_provider,
            model=model,
            plan_react_planner_model=plan_react_planner_model,
            plan_react_executor_model=plan_react_executor_model,
            plan_react_planner_api_key=plan_react_planner_api_key,
            plan_react_executor_api_key=plan_react_executor_api_key,
            plan_react_planner_base_url=plan_react_planner_base_url,
            plan_react_executor_base_url=plan_react_executor_base_url,
            plan_react_planner_structured_output_mode=planner_structured_output_mode,
            plan_react_executor_structured_output_mode=executor_structured_output_mode,
            api_key=_optional_env("OPENAI_API_KEY", file_values),
            base_url=_optional_env("OPENAI_BASE_URL", file_values),
            structured_output_mode=cast(
                StructuredOutputMode,
                raw_structured_output_mode,
            ),
            cli_executable=_optional_env("AGENT_CLI_EXECUTABLE", file_values),
            cli_timeout_seconds=timeout_seconds,
            cli_max_output_bytes=max_output_bytes,
            artifact_root=artifact_root.resolve(),
        )

    def for_plan_react_role(self, role: PlanReactRole) -> Settings:
        """Overlay one Plan-ReAct role's endpoint settings on the shared defaults."""
        if role == "planner":
            return replace(
                self,
                model=self.plan_react_planner_model or self.model,
                api_key=self.plan_react_planner_api_key or self.api_key,
                base_url=self.plan_react_planner_base_url or self.base_url,
                structured_output_mode=(
                    self.plan_react_planner_structured_output_mode
                    or self.structured_output_mode
                ),
            )
        if role == "executor":
            return replace(
                self,
                model=self.plan_react_executor_model or self.model,
                api_key=self.plan_react_executor_api_key or self.api_key,
                base_url=self.plan_react_executor_base_url or self.base_url,
                structured_output_mode=(
                    self.plan_react_executor_structured_output_mode
                    or self.structured_output_mode
                ),
            )
        raise ValueError("Plan-ReAct role must be planner or executor")


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


def _optional_structured_output_mode(
    name: str,
    file_values: Mapping[str, str | None],
) -> StructuredOutputMode | None:
    value = _optional_env(name, file_values)
    if value is None:
        return None
    normalized = value.lower()
    if normalized not in _STRUCTURED_OUTPUT_MODES:
        raise ValueError(f"{name} must be json-schema, json-object, or prompt")
    return cast(StructuredOutputMode, normalized)
