"""Immutable values used to prepare one model request's context."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from base_agent.memory import MemorySnapshot
from base_agent.models import Message, ToolSpec


@dataclass(frozen=True, slots=True)
class SystemContextBlock:
    """Application-trusted system context available for policy selection."""

    id: str
    content: str
    required: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("system context block id must not be blank")
        if self.id != self.id.strip():
            raise ValueError("system context block id must not have surrounding whitespace")
        if not self.content.strip():
            raise ValueError("system context block content must not be blank")


@dataclass(frozen=True, slots=True)
class SystemContext:
    """Immutable core instructions plus declared, trusted system blocks."""

    instructions: str
    blocks: tuple[SystemContextBlock, ...] = ()

    def __post_init__(self) -> None:
        if not self.instructions.strip():
            raise ValueError("system context instructions must not be blank")
        block_ids = tuple(block.id for block in self.blocks)
        if len(set(block_ids)) != len(block_ids):
            raise ValueError("system context block ids must be unique")

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(block.id for block in self.blocks)

    @property
    def required_block_ids(self) -> tuple[str, ...]:
        return tuple(block.id for block in self.blocks if block.required)

    def render(self, selected_block_ids: tuple[str, ...] | None = None) -> str:
        selected = self.block_ids if selected_block_ids is None else selected_block_ids
        if len(set(selected)) != len(selected):
            raise ValueError("selected system context block ids must be unique")
        known_ids = set(self.block_ids)
        unknown_ids = set(selected) - known_ids
        if unknown_ids:
            raise ValueError(
                "selected unknown system context blocks: " + ", ".join(sorted(unknown_ids))
            )
        missing_required = set(self.required_block_ids) - set(selected)
        if missing_required:
            raise ValueError(
                "required system context blocks were omitted: "
                + ", ".join(sorted(missing_required))
            )
        selected_set = set(selected)
        canonical_selected = tuple(
            block.id for block in self.blocks if block.id in selected_set
        )
        if selected != canonical_selected:
            raise ValueError(
                "selected system context block ids must follow declaration order"
            )
        sections = [self.instructions.strip()]
        sections.extend(
            block.content.strip() for block in self.blocks if block.id in selected_set
        )
        return "\n\n".join(sections)

    def fingerprint(self, selected_block_ids: tuple[str, ...] | None = None) -> str:
        return _text_fingerprint(self.render(selected_block_ids))


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Application-owned limits available to a ContextPolicy.

    The default is intentionally unbounded. A finite budget is descriptive:
    the selected ContextPolicy remains responsible for estimating and
    enforcing it because tokenization is model/provider specific.
    """

    context_window_tokens: int | None = None
    reserved_output_tokens: int = 0
    safety_margin_tokens: int = 0

    def __post_init__(self) -> None:
        if self.context_window_tokens is not None and self.context_window_tokens < 1:
            raise ValueError("context_window_tokens must be positive when provided")
        if self.reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must be non-negative")
        if self.safety_margin_tokens < 0:
            raise ValueError("safety_margin_tokens must be non-negative")
        if (
            self.context_window_tokens is not None
            and self.reserved_output_tokens + self.safety_margin_tokens
            >= self.context_window_tokens
        ):
            raise ValueError(
                "reserved_output_tokens plus safety_margin_tokens must leave input capacity"
            )

    @property
    def max_input_tokens(self) -> int | None:
        if self.context_window_tokens is None:
            return None
        return (
            self.context_window_tokens
            - self.reserved_output_tokens
            - self.safety_margin_tokens
        )


@dataclass(frozen=True, slots=True)
class ContextInput:
    """Read-only candidates and request overhead for one model turn."""

    agent_id: str
    run_id: str
    model: str
    system_context: SystemContext
    transcript: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    response_schema: Mapping[str, Any] | None
    turn: int
    budget: ContextBudget
    memory: MemorySnapshot

    @property
    def system_prompt(self) -> str:
        return self.system_context.render()

    @property
    def system_prompt_fingerprint(self) -> str:
        return self.system_context.fingerprint()

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("context agent_id must not be blank")
        if not self.run_id.strip():
            raise ValueError("context run_id must not be blank")
        if not self.model.strip():
            raise ValueError("context model must not be blank")
        if self.turn < 1:
            raise ValueError("context turn must be at least 1")
        if not self.transcript:
            raise ValueError("context transcript must not be empty")


@dataclass(frozen=True, slots=True)
class ContextTrace:
    """Non-content selection facts safe to expose through Run events."""

    policy: str
    source_system_prompt_fingerprint: str
    selected_system_prompt_fingerprint: str
    selected_system_block_ids: tuple[str, ...]
    source_message_count: int
    selected_message_count: int
    omitted_message_count: int = 0
    omitted_tool_result_count: int = 0
    selected_message_ids: tuple[str, ...] = ()
    omitted_message_ids: tuple[str, ...] = ()
    memory_candidate_count: int = 0
    selected_memory_ids: tuple[str, ...] = ()
    estimated_input_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.policy.strip():
            raise ValueError("context policy must not be blank")
        fingerprints = (
            self.source_system_prompt_fingerprint,
            self.selected_system_prompt_fingerprint,
        )
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in fingerprints
        ):
            raise ValueError("system prompt fingerprints must be SHA-256 hex digests")
        if len(set(self.selected_system_block_ids)) != len(self.selected_system_block_ids):
            raise ValueError("selected_system_block_ids must not contain duplicates")
        counts = (
            self.source_message_count,
            self.selected_message_count,
            self.omitted_message_count,
            self.omitted_tool_result_count,
            self.memory_candidate_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("context trace counts must be non-negative")
        if len(set(self.selected_message_ids)) != len(self.selected_message_ids):
            raise ValueError("selected_message_ids must not contain duplicates")
        if len(set(self.omitted_message_ids)) != len(self.omitted_message_ids):
            raise ValueError("omitted_message_ids must not contain duplicates")
        if len(set(self.selected_memory_ids)) != len(self.selected_memory_ids):
            raise ValueError("selected_memory_ids must not contain duplicates")
        if self.estimated_input_tokens is not None and self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be non-negative when provided")


@dataclass(frozen=True, slots=True)
class ContextView:
    """The complete request-only system and Message projection for a model call."""

    system_prompt: str
    messages: tuple[Message, ...]
    trace: ContextTrace


class ContextPolicyError(RuntimeError):
    """A ContextPolicy failed or returned an invalid request projection."""


def _text_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
