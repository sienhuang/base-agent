"""Select an offline or OpenAI-compatible Model Provider."""

from __future__ import annotations

import json

from base_agent import MessageRole, ModelRequest, ModelResponse, ToolCall
from base_agent.providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    ModelProvider,
    OpenAIChatProvider,
)

from agent_app.config import Settings


class OfflineModel:
    """Reusable deterministic Provider for local runs, tests, and onboarding."""

    name = "starter-offline"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        last = request.messages[-1]
        react_enabled = any(
            "Use an iterative ReAct process" in (message.content or "")
            for message in request.messages
        )
        if last.role is MessageRole.TOOL:
            payload = json.loads(last.content or "{}")
            counts = payload.get("data", {})
            result = (
                "Offline starter completed the Tool loop: "
                f"{counts.get('words', 0)} words, "
                f"{counts.get('characters', 0)} characters."
            )
            if react_enabled:
                return ModelResponse(
                    content=json.dumps(
                        {"success": True, "result": result, "attachments": []}
                    )
                )
            return ModelResponse(
                content=result
            )
        prompt = last.content or ""
        if prompt.startswith("Create a concise execution plan"):
            return ModelResponse(
                content=json.dumps(
                    {
                        "id": "offline-plan",
                        "title": "Offline planned task",
                        "steps": [
                            {
                                "id": "execute",
                                "description": "Complete the requested task",
                                "executor": "model",
                                "dependencies": [],
                            }
                        ],
                    }
                )
            )
        if prompt.startswith("Execute exactly one step"):
            return ModelResponse(content="Offline planned step completed.")
        if prompt.startswith("Update the execution plan"):
            return ModelResponse(content=json.dumps({"steps": []}))
        if prompt.startswith("The execution plan is complete"):
            return ModelResponse(content="Offline planned Run completed.")
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
    if settings.provider == "codex-cli":
        return CodexCLIProvider(
            model=settings.model,
            executable=settings.cli_executable or "codex",
            timeout_seconds=settings.cli_timeout_seconds,
            max_output_bytes=settings.cli_max_output_bytes,
        )
    if settings.provider == "claude-cli":
        return ClaudeCLIProvider(
            model=settings.model,
            executable=settings.cli_executable or "claude",
            timeout_seconds=settings.cli_timeout_seconds,
            max_output_bytes=settings.cli_max_output_bytes,
        )
    if settings.model is None:
        raise ValueError("AGENT_MODEL is required for the openai Provider")
    return OpenAIChatProvider(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
    )
