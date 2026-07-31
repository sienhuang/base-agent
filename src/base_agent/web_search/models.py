"""Provider-neutral Web Search request and result values."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DOMAIN_PATTERN = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)


class WebSearchFreshness(StrEnum):
    """Portable freshness windows supported by the first-party Tool."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class WebSearchQuery(BaseModel):
    """One bounded, provider-neutral Web Search request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=400)
    limit: int = Field(default=5, ge=1, le=20)
    domains: tuple[str, ...] = Field(default_factory=tuple, max_length=5)
    freshness: WebSearchFreshness | None = None
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    search_language: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?$",
    )

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("search query must not be blank")
        if len(normalized.split()) > 50:
            raise ValueError("search query must contain at most 50 words")
        return normalized

    @field_validator("domains")
    @classmethod
    def normalize_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for domain in value:
            candidate = domain.strip().rstrip(".").lower()
            if not candidate or not _DOMAIN_PATTERN.fullmatch(candidate):
                raise ValueError(f"invalid search domain '{domain}'")
            if candidate not in normalized:
                normalized.append(candidate)
        return tuple(normalized)

    @field_validator("country", mode="before")
    @classmethod
    def normalize_country(cls, value: str | None) -> str | None:
        return value.strip().upper() if value is not None else None

    @field_validator("search_language", mode="before")
    @classmethod
    def normalize_search_language(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class WebSearchResult(BaseModel):
    """One bounded search result suitable for model context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=4_096)
    snippet: str = Field(default="", max_length=4_000)
    source: str | None = Field(default=None, max_length=255)
    published: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("search result URL must be an absolute HTTP or HTTPS URL")
        return self


class WebSearchResponse(BaseModel):
    """Normalized results returned by a WebSearchProvider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1, max_length=128)
    results: tuple[WebSearchResult, ...] = Field(default_factory=tuple, max_length=20)
    has_more: bool = False
