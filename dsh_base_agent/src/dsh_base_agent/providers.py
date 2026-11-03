"""Provider profiles selected by business Agents but configured by the service."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """A named DSH model route with optional explicit credentials.

    ``name`` is the application-facing profile name. ``provider`` and
    ``model`` are sent to DSH. Secrets are excluded from repr and never enter
    Events or Agent snapshots.
    """

    name: str
    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    reasoning_effort: str | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.name or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "ProviderConfig name must contain only letters, numbers, '.', '_' or '-'"
            )
        for required_value, label in (
            (self.provider, "provider"),
            (self.model, "model"),
        ):
            if not required_value.strip():
                raise ValueError(f"ProviderConfig {label} must not be blank")
        for optional_value, label in (
            (self.base_url, "base_url"),
            (self.api_key, "api_key"),
            (self.reasoning_effort, "reasoning_effort"),
        ):
            if optional_value is not None and not optional_value.strip():
                raise ValueError(f"ProviderConfig {label} must not be blank when provided")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("ProviderConfig max_tokens must be positive")

    @classmethod
    def from_env(
        cls,
        *,
        name: str = "default",
        environ: Mapping[str, str] | None = None,
        provider: str | None = None,
        model: str | None = None,
        prefix: str = "DSH_",
    ) -> ProviderConfig:
        values = os.environ if environ is None else environ
        return cls(
            name=name,
            provider=provider or values.get(f"{prefix}PROVIDER", "deepseek-official"),
            model=model or values.get(f"{prefix}MODEL", "deepseek-v4-flash"),
            base_url=_optional(values, f"{prefix}BASE_URL")
            or _optional(values, "DEEPSEEK_BASE_URL"),
            api_key=_optional(values, f"{prefix}API_KEY")
            or _optional(values, "DEEPSEEK_API_KEY"),
            reasoning_effort=_optional(values, f"{prefix}REASONING_EFFORT"),
            max_tokens=_optional_int(values, f"{prefix}MAX_TOKENS"),
        )


class ProviderCatalog:
    """Immutable provider profiles with one explicit default."""

    def __init__(
        self,
        providers: Iterable[ProviderConfig],
        *,
        default: str,
    ) -> None:
        registered = tuple(providers)
        by_name = {provider.name: provider for provider in registered}
        if not registered:
            raise ValueError("ProviderCatalog requires at least one ProviderConfig")
        if len(by_name) != len(registered):
            raise ValueError("registered ProviderConfigs must have unique names")
        if default not in by_name:
            raise ValueError(f"default ProviderConfig '{default}' is not registered")
        self._providers = registered
        self._by_name = MappingProxyType(by_name)
        self.default = default

    @property
    def providers(self) -> tuple[ProviderConfig, ...]:
        return self._providers

    def resolve(self, name: str | None = None) -> ProviderConfig:
        selected = name or self.default
        try:
            return self._by_name[selected]
        except KeyError as exc:
            raise ValueError(f"ProviderConfig '{selected}' is not registered") from exc


def _optional(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    return value.strip() if value is not None and value.strip() else None


def _optional_int(values: Mapping[str, str], name: str) -> int | None:
    value = _optional(values, name)
    return None if value is None else int(value)


__all__ = ["ProviderCatalog", "ProviderConfig"]
