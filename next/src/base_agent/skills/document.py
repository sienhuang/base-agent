"""Parsing for standard ``SKILL.md`` YAML frontmatter and playbook bodies."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import yaml


def parse_skill_document(
    content: str,
    *,
    source: str,
) -> tuple[Mapping[str, Any], str]:
    """Return immutable frontmatter and the complete playbook body."""
    normalized = content.strip()
    lines = normalized.splitlines()
    if not lines or lines[0].strip() != "---":
        return MappingProxyType({}), normalized

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        raise ValueError(f"Skill frontmatter is not closed in {source}")

    encoded_frontmatter = "\n".join(lines[1:closing_index])
    try:
        loaded = yaml.safe_load(encoded_frontmatter)
    except yaml.YAMLError as exc:
        raise ValueError(f"Skill frontmatter is invalid in {source}: {exc}") from exc
    if loaded is None:
        metadata: dict[str, Any] = {}
    elif isinstance(loaded, Mapping):
        if any(not isinstance(key, str) for key in loaded):
            raise ValueError(f"Skill frontmatter keys must be strings in {source}")
        metadata = dict(loaded)
    else:
        raise ValueError(f"Skill frontmatter must be a mapping in {source}")

    playbook = "\n".join(lines[closing_index + 1 :]).strip()
    if not playbook:
        raise ValueError(f"Skill playbook is empty after frontmatter in {source}")
    return MappingProxyType(metadata), playbook
