"""Application-owned Provider selection for the first SDK capability test."""

import asyncio
import json
import re
from collections.abc import AsyncIterator

from base_agent.models import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
    ModelToolCallDelta,
    ToolCall,
)
from base_agent.providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    ModelProvider,
    OpenAIChatProvider,
    ProviderCapabilities,
    ProviderInfo,
)

from agent_app.config import PlanReactRole, Settings


class OfflineProvider:
    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="starter-offline",
            default_model="starter-offline",
            capabilities=ProviderCapabilities(
                tools=True,
                structured_output=True,
                streaming=True,
            ),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        user_messages = [message.content for message in request.messages if message.role == "user"]
        if not user_messages:
            raise ValueError("OfflineProvider requires one user message")
        tool_messages = [message for message in request.messages if message.role == "tool"]
        response_schema_title = (
            request.response_schema.get("title")
            if request.response_schema is not None
            else None
        )
        if response_schema_title == "PlannerDecision":
            return ModelResponse(
                content=json.dumps(
                    _offline_planner_decision(user_messages[-1]),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        if response_schema_title == "StepOutcome":
            if not tool_messages:
                requested_calls = _requested_offline_tool_calls(user_messages[-1])
                if requested_calls is not None and request.tools:
                    return _offline_tool_call_response(
                        user_messages[-1],
                        requested_calls=requested_calls,
                    )
            return ModelResponse(
                content=json.dumps(
                    _offline_step_outcome(user_messages[-1], tool_messages),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        if tool_messages:
            if _show_offline_observations(user_messages[-1]):
                return ModelResponse(content=_offline_observation_details(tool_messages))
            statuses = [_observation_status(message.content) for message in tool_messages]
            return ModelResponse(
                content=(
                    f"executed={statuses.count('success')}, "
                    f"not_found={statuses.count('not_found')}, "
                    f"denied={statuses.count('denied')}, "
                    f"confirmation_denied="
                    f"{statuses.count('confirmation_denied')}, "
                    f"invalid_arguments="
                    f"{statuses.count('invalid_arguments')}, "
                    f"error={statuses.count('error')}, "
                    f"timeout={statuses.count('timeout')}, "
                    f"limit_exceeded={statuses.count('limit_exceeded')}"
                )
            )

        requested_calls = _requested_offline_tool_calls(user_messages[-1])
        if requested_calls is not None and request.tools:
            return _offline_tool_call_response(
                user_messages[-1],
                requested_calls=requested_calls,
            )
        return ModelResponse(content=f"Starter Agent received: {user_messages[-1]}")

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        response = await self.complete(request)
        if response.content:
            for offset in range(0, len(response.content), 8):
                await asyncio.sleep(0)
                yield ModelTextDelta(response.content[offset : offset + 8])
        for index, call in enumerate(response.tool_calls):
            encoded_arguments = json.dumps(
                dict(call.arguments),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            split_at = max(1, len(encoded_arguments) // 2)
            fragments = (
                encoded_arguments[:split_at],
                encoded_arguments[split_at:],
            )
            await asyncio.sleep(0)
            yield ModelToolCallDelta(
                index=index,
                id=call.id,
                name=call.name,
                arguments_delta=fragments[0],
            )
            if fragments[1]:
                await asyncio.sleep(0)
                yield ModelToolCallDelta(
                    index=index,
                    arguments_delta=fragments[1],
                )
        yield ModelStreamCompleted(response)


def build_provider(
    settings: Settings | None = None,
) -> ModelProvider:
    """Build one Provider without exposing credentials to the Agent definition."""
    resolved = settings or Settings()
    if resolved.provider == "offline":
        return OfflineProvider()
    if resolved.provider == "codex-cli":
        return CodexCLIProvider(
            model=resolved.model,
            executable=resolved.cli_executable or "codex",
            timeout_seconds=resolved.cli_timeout_seconds,
            max_output_bytes=resolved.cli_max_output_bytes,
        )
    if resolved.provider == "claude-cli":
        return ClaudeCLIProvider(
            model=resolved.model,
            executable=resolved.cli_executable or "claude",
            timeout_seconds=resolved.cli_timeout_seconds,
            max_output_bytes=resolved.cli_max_output_bytes,
        )
    if resolved.model is None:
        raise ValueError("OpenAI-compatible mode requires --model, AGENT_MODEL, or OPENAI_MODEL")
    if resolved.api_key is None:
        raise ValueError(
            "OpenAI-compatible mode requires OPENAI_API_KEY; "
            "use a placeholder value if the local server does not authenticate"
        )

    return OpenAIChatProvider(
        default_model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        timeout_seconds=60,
        structured_output_mode=resolved.structured_output_mode,
    )


def build_plan_react_providers(
    settings: Settings | None = None,
    *,
    default_provider: ModelProvider | None = None,
) -> tuple[ModelProvider, ModelProvider]:
    """Build Planner and Executor Providers, reusing identical effective routes."""
    resolved = settings or Settings()
    providers: dict[Settings, ModelProvider] = {}
    if default_provider is not None:
        providers[resolved] = default_provider

    def provider_for(role: PlanReactRole) -> ModelProvider:
        route = resolved.for_plan_react_role(role)
        provider = providers.get(route)
        if provider is None:
            provider = build_provider(route)
            providers[route] = provider
        return provider

    return provider_for("planner"), provider_for("executor")


def _requested_offline_tool_calls(prompt: str) -> int | None:
    matched = re.search(r"\bcalls\s*=\s*(\d+)\b", prompt, flags=re.IGNORECASE)
    if matched is None:
        return None
    requested = int(matched.group(1))
    if requested < 1:
        raise ValueError("calls must be at least 1")
    return requested


def _offline_tool_call_response(
    prompt: str,
    *,
    requested_calls: int,
) -> ModelResponse:
    tool_name = _offline_tool_name(prompt)
    explicit_arguments = _offline_tool_arguments(prompt)
    return ModelResponse(
        tool_calls=tuple(
            ToolCall(
                id=f"call-{index}",
                name=tool_name,
                arguments=(
                    dict(explicit_arguments)
                    if explicit_arguments is not None
                    else _default_tool_arguments(tool_name, index)
                ),
            )
            for index in range(1, requested_calls + 1)
        ),
        finish_reason=FinishReason.TOOL_CALLS,
    )


def _offline_planner_decision(prompt: str) -> dict[str, object]:
    if prompt.startswith("Create a concise executable Plan"):
        user_request = _prompt_section(
            prompt,
            start="User request:\n",
            end="\n\nFixed Executor capabilities:",
        )
        return {
            "action": "create",
            "goal": user_request,
            "steps": [
                {
                    "description": (
                        "Complete the user request with appropriate Tools and evidence: "
                        f"{user_request}"
                    )
                }
            ],
            "reason": "The offline Planner uses one deterministic executable Step.",
            "final_output": None,
        }

    plan = _prompt_json_after(prompt, "Current Plan:\n")
    steps = plan.get("steps")
    if isinstance(steps, list) and any(
        isinstance(step, dict) and step.get("status") == "pending" for step in steps
    ):
        return {
            "action": "continue",
            "goal": None,
            "steps": [],
            "reason": "The current Plan still has a pending Step.",
            "final_output": None,
        }

    outcome = _prompt_json_after(prompt, "Latest outcome:\n")
    output = outcome.get("output")
    summary = outcome.get("summary")
    final_output = output if isinstance(output, str) and output else summary
    if not isinstance(final_output, str) or not final_output:
        final_output = "Offline Planner-ReAct completed the Plan."
    return {
        "action": "complete",
        "goal": None,
        "steps": [],
        "reason": "All deterministic Plan Steps are terminal.",
        "final_output": final_output,
    }


def _offline_step_outcome(
    prompt: str,
    tool_messages: list[Message],
) -> dict[str, object]:
    step = _prompt_section(prompt, start="Current Step:\n")
    if tool_messages:
        output = _offline_observation_details(tool_messages)
        summary = f"Completed the Step with {len(tool_messages)} Tool observation(s)."
    else:
        output = f"Offline Executor completed: {step}"
        summary = "Completed the Step with the deterministic offline Provider."
    return {
        "success": True,
        "summary": summary,
        "output": output,
        "error": None,
        "artifact_ids": _created_artifact_ids(tool_messages),
    }


def _created_artifact_ids(tool_messages: list[Message]) -> list[str]:
    artifact_ids: list[str] = []
    for message in tool_messages:
        receipts = _observation_payload(message.content).get("artifacts_created")
        if not isinstance(receipts, list):
            continue
        for receipt in receipts:
            if not isinstance(receipt, dict):
                continue
            artifact_id = receipt.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id not in artifact_ids:
                artifact_ids.append(artifact_id)
    return artifact_ids


def _prompt_section(
    prompt: str,
    *,
    start: str,
    end: str | None = None,
) -> str:
    start_index = prompt.find(start)
    if start_index < 0:
        raise ValueError(f"OfflineProvider expected prompt section {start!r}")
    content = prompt[start_index + len(start) :]
    if end is not None:
        end_index = content.find(end)
        if end_index < 0:
            raise ValueError(f"OfflineProvider expected prompt section terminator {end!r}")
        content = content[:end_index]
    return content.strip()


def _prompt_json_after(prompt: str, marker: str) -> dict[str, object]:
    encoded = _prompt_section(prompt, start=marker)
    try:
        value, _ = json.JSONDecoder().raw_decode(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"OfflineProvider expected JSON after {marker!r}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"OfflineProvider expected a JSON object after {marker!r}")
    return value


def _offline_tool_name(prompt: str) -> str:
    matched = re.search(
        r"\btool\s*=\s*([A-Za-z0-9_.-]+)\b",
        prompt,
        flags=re.IGNORECASE,
    )
    return matched.group(1) if matched is not None else "record_value"


def _default_tool_arguments(
    tool_name: str,
    call_index: int,
) -> dict[str, object]:
    if tool_name == "record_value":
        return {"value": call_index}
    return {}


def _offline_tool_arguments(prompt: str) -> dict[str, object] | None:
    marker = "tool_args="
    marker_index = prompt.find(marker)
    if marker_index < 0:
        return None
    encoded = prompt[marker_index + len(marker) :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("tool_args must contain a valid JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("tool_args must contain a JSON object")
    return value


def _show_offline_observations(prompt: str) -> bool:
    return (
        re.search(
            r"\bshow_observations\s*=\s*true\b",
            prompt,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _offline_observation_details(messages: list[Message]) -> str:
    observations: list[dict[str, object]] = []
    for message in messages:
        if message.tool_call_id is None or message.name is None:
            raise ValueError("Tool observation must identify its ToolCall and Tool")
        observations.append(
            {
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "content": _observation_payload(message.content),
            }
        )
    return json.dumps(
        {"observations": observations},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _observation_payload(content: str) -> dict[str, object]:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Tool observation must be a JSON object")
    return value


def _observation_status(content: str) -> str:
    value = _observation_payload(content)
    status = value.get("status")
    if not isinstance(status, str):
        raise ValueError("Tool observation must contain a string status")
    return status
