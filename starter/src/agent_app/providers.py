"""Select an offline or OpenAI-compatible Model Provider."""

from __future__ import annotations

import json

from base_agent import MessageRole, ModelRequest, ModelResponse, ToolCall
from base_agent.providers import ModelProvider, OpenAIChatProvider

from agent_app.config import Settings


class OfflineModel:
    """Reusable deterministic Provider for local runs, tests, and onboarding."""

    name = "starter-offline"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        last = request.messages[-1]
        if last.role is MessageRole.TOOL:
            payload = json.loads(last.content or "{}")
            counts = payload.get("data", {})
            return ModelResponse(
                content=(
                    "Offline starter completed the Tool loop: "
                    f"{counts.get('words', 0)} words, "
                    f"{counts.get('characters', 0)} characters."
                )
            )
        prompt = last.content or ""
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    id="offline-word-count",
                    name="word_count",
                    arguments={"text": prompt},
                ),
            )
        )


def build_provider(settings: Settings) -> ModelProvider:
    if settings.provider == "offline":
        return OfflineModel()
    return OpenAIChatProvider(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
    )
