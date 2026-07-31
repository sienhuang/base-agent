"""Environment-backed application configuration without secret side effects."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

ProviderName = Literal["offline", "openai", "codex-cli", "claude-cli"]
MtbiEngineName = Literal["PRESTO", "SPARK", "DORIS"]
_PROVIDER_NAMES = frozenset({"offline", "openai", "codex-cli", "claude-cli"})
_CLI_PROVIDERS = frozenset({"codex-cli", "claude-cli"})
_MTBI_ENGINES = frozenset({"PRESTO", "SPARK", "DORIS"})


@dataclass(frozen=True, slots=True)
class Settings:
    provider: ProviderName = "offline"
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    cli_executable: str | None = None
    cli_timeout_seconds: float = 300.0
    cli_max_output_bytes: int = 4 * 1024 * 1024
    enable_coding: bool = False
    sandbox_image: str | None = None
    enable_web_search: bool = False
    brave_search_api_key: str | None = None
    enable_mtbi: bool = False
    mtbi_cli_executable: str = "mtbi-cli"
    mtbi_engine: MtbiEngineName = "PRESTO"
    mtbi_region: str | None = None
    raft_profile: str | None = None
    raft_agent_id: UUID | None = None
    raft_agent_handle: str | None = None
    raft_cli_executable: str = "raft"
    raft_state_dir: Path = Path(".base-agent/raft")
    raft_max_reply_chars: int = 8_000
    raft_bridge_poll_interval_ms: int = 5_000

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
        sandbox_image = _optional_env("AGENT_SANDBOX_IMAGE")
        brave_search_api_key = _optional_env("BRAVE_SEARCH_API_KEY")
        mtbi_cli_executable = _optional_env("AGENT_MTBI_CLI_EXECUTABLE") or "mtbi-cli"
        raw_mtbi_engine = os.getenv("AGENT_MTBI_ENGINE", "PRESTO").strip().upper()
        if raw_mtbi_engine not in _MTBI_ENGINES:
            raise ValueError("AGENT_MTBI_ENGINE must be PRESTO, SPARK, or DORIS")
        mtbi_engine = cast(MtbiEngineName, raw_mtbi_engine)
        raw_raft_agent_id = _optional_env("RAFT_AGENT_ID")
        try:
            raft_agent_id = (
                UUID(raw_raft_agent_id) if raw_raft_agent_id is not None else None
            )
            raft_max_reply_chars = int(
                os.getenv("RAFT_MAX_REPLY_CHARS", "8000")
            )
            raft_bridge_poll_interval_ms = int(
                os.getenv("RAFT_BRIDGE_POLL_INTERVAL_MS", "5000")
            )
        except ValueError as exc:
            raise ValueError(
                "RAFT_AGENT_ID must be a UUID and Raft numeric settings "
                "must be integers"
            ) from exc
        if raft_max_reply_chars < 256:
            raise ValueError("RAFT_MAX_REPLY_CHARS must be at least 256")
        if raft_bridge_poll_interval_ms < 250:
            raise ValueError("RAFT_BRIDGE_POLL_INTERVAL_MS must be at least 250")
        return cls(
            provider=resolved_provider,
            model=model,
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            cli_executable=cli_executable.strip() if cli_executable else None,
            cli_timeout_seconds=cli_timeout_seconds,
            cli_max_output_bytes=cli_max_output_bytes,
            enable_coding=_env_flag("AGENT_ENABLE_CODING"),
            sandbox_image=sandbox_image,
            enable_web_search=_env_flag("AGENT_ENABLE_WEB_SEARCH"),
            brave_search_api_key=brave_search_api_key,
            enable_mtbi=_env_flag("AGENT_ENABLE_MTBI"),
            mtbi_cli_executable=mtbi_cli_executable,
            mtbi_engine=mtbi_engine,
            mtbi_region=_optional_env("AGENT_MTBI_REGION"),
            raft_profile=_optional_env("RAFT_PROFILE"),
            raft_agent_id=raft_agent_id,
            raft_agent_handle=_optional_env("RAFT_AGENT_HANDLE"),
            raft_cli_executable=(
                _optional_env("RAFT_CLI_EXECUTABLE") or "raft"
            ),
            raft_state_dir=Path(
                _optional_env("RAFT_STATE_DIR") or ".base-agent/raft"
            ),
            raft_max_reply_chars=raft_max_reply_chars,
            raft_bridge_poll_interval_ms=raft_bridge_poll_interval_ms,
        )


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be blank")
    return normalized


def _env_flag(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")
