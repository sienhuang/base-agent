"""Environment-backed application configuration without secret side effects."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, cast

ProviderName = Literal["offline", "openai"]


@dataclass(frozen=True, slots=True)
class Settings:
    provider: ProviderName = "offline"
    model: str = "gpt-4.1-mini"
    api_key: str | None = None
    base_url: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        raw_provider = os.getenv("AGENT_PROVIDER", "offline").strip().lower()
        if raw_provider not in {"offline", "openai"}:
            raise ValueError("AGENT_PROVIDER must be 'offline' or 'openai'")
        model = os.getenv("AGENT_MODEL", "gpt-4.1-mini").strip()
        if not model:
            raise ValueError("AGENT_MODEL must not be blank")
        return cls(
            provider=cast(ProviderName, raw_provider),
            model=model,
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
        )
