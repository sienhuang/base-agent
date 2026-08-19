"""Internal execution Harness used automatically by the public Agent facade."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from base_agent.artifacts import Artifact, ArtifactManager, ArtifactStore
from base_agent.context import (
    ContextBudget,
    ContextInput,
    ContextPolicy,
    ContextPolicyError,
    ContextView,
    SystemContext,
)
from base_agent.definitions import AgentDefinition
from base_agent.events import EventEmitter, EventStore
from base_agent.governance import (
    Confirmation,
    ConfirmationPolicy,
    ConfirmationRequest,
    DenyConfirmations,
)
from base_agent.memory import (
    MemoryObservation,
    MemoryProvider,
    MemoryRecallRequest,
    MemoryRecord,
    MemoryScope,
    MemorySnapshot,
    MemoryWriter,
)
from base_agent.models import (
    Event,
    HarnessSnapshot,
    Message,
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelToolCallDelta,
    ResourceSnapshot,
    RunFailure,
    RunInvocation,
    RunResult,
    RunStatus,
    SkillSnapshot,
    ToolCall,
    ToolChoice,
    ToolResult,
    ToolResultStatus,
)
from base_agent.observability import Observer
from base_agent.providers import ModelProvider, StreamingModelProvider
from base_agent.providers.errors import (
    InvalidProviderResponseError,
    ProviderError,
)
from base_agent.resources import ResourceContext, ResourceManager, ResourceSpec
from base_agent.skills import Skill
from base_agent.skills.execution import skill_script_tool_name
from base_agent.skills.loading import SKILL_VIEW_TOOL_NAME
from base_agent.tools import Tool, flatten_tools
from base_agent.tools.core import ToolContext
from base_agent.verification import VerificationEvidence


@dataclass(frozen=True, slots=True)
class _SkillResourceSource:
    skill: Skill
    resource_type: str


class Harness:
    """Bounded model/tool runtime hidden behind ``Agent.run``."""

    def __init__(
        self,
        *,
        definition: AgentDefinition,
        model: ModelProvider,
        tools: tuple[Tool, ...],
        skills: tuple[Skill, ...],
        confirmation_policy: ConfirmationPolicy | None,
        context_policy: ContextPolicy,
        context_budget: ContextBudget,
        system_context: SystemContext,
        memory_provider: MemoryProvider,
        memory_writer: MemoryWriter,
        memory_recall_limit: int,
        memory_recall_timeout_seconds: float,
        observers: Sequence[Observer],
        event_store: EventStore,
        artifact_store: ArtifactStore,
        resources: tuple[ResourceSpec, ...],
    ) -> None:
        self._definition = definition
        self._model = model
        self._tools = tools
        self._skills = skills
        self._skill_resources_by_tool = _index_skill_resources(skills)
        self._confirmation_policy = confirmation_policy or DenyConfirmations()
        self._context_policy = context_policy
        self._context_policy_name = context_policy.name
        self._context_budget = context_budget
        self._system_context = system_context
        self._memory_provider = memory_provider
        self._memory_provider_name = memory_provider.name
        self._memory_enabled = memory_provider.enabled
        self._memory_writer = memory_writer
        self._memory_writer_name = memory_writer.name
        self._memory_write_enabled = memory_writer.enabled
        self._memory_recall_limit = memory_recall_limit
        self._memory_recall_timeout_seconds = memory_recall_timeout_seconds
        self._observers = tuple(observers)
        self._event_store = event_store
        self._artifact_store = artifact_store
        self._resources = resources

    async def run(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
        run_id: str | None = None,
        memory_scope: MemoryScope | None = None,
        invocation: RunInvocation | None = None,
    ) -> RunResult:
        resolved_run_id = run_id or str(uuid4())
        resolved_invocation = invocation or RunInvocation()
        run_observers = self._observers if on_event is None else (*self._observers, on_event)
        emitter = EventEmitter(resolved_run_id, self._event_store, run_observers)
        artifact_manager = ArtifactManager(
            self._artifact_store,
            run_id=resolved_run_id,
            agent_id=self._definition.id,
            emitter=emitter,
            flow_run_id=resolved_invocation.flow_run_id,
            invocation_id=resolved_invocation.invocation_id,
            stage=resolved_invocation.stage,
            step_id=resolved_invocation.step_id,
            attempt=resolved_invocation.attempt,
            granted_artifact_ids=resolved_invocation.artifact_ids,
        )
        resource_manager = ResourceManager(
            self._resources,
            context=ResourceContext(
                run_id=resolved_run_id,
                agent_id=self._definition.id,
                flow_run_id=resolved_invocation.flow_run_id,
                invocation_id=resolved_invocation.invocation_id,
                stage=resolved_invocation.stage,
                step_id=resolved_invocation.step_id,
                attempt=resolved_invocation.attempt,
            ),
            emitter=emitter,
        )
        try:
            result = await self._run(
                prompt,
                stream=stream,
                memory_scope=memory_scope,
                emitter=emitter,
                artifact_manager=artifact_manager,
                resource_manager=resource_manager,
            )
        except BaseException:
            try:
                await resource_manager.close()
            except Exception:
                pass
            raise
        try:
            await resource_manager.close()
        except Exception:
            # The main Run has already failed; preserve that primary failure.
            # Successful Runs close Resources before committing run.succeeded.
            if result.status is RunStatus.SUCCEEDED:
                raise
        return result

    async def _run(
        self,
        prompt: str,
        *,
        stream: bool = False,
        memory_scope: MemoryScope | None = None,
        emitter: EventEmitter,
        artifact_manager: ArtifactManager,
        resource_manager: ResourceManager,
    ) -> RunResult:
        if not prompt.strip():
            raise ValueError("run prompt must not be blank")
        if stream and not self._model.info.capabilities.streaming:
            raise ValueError(f"Provider '{self._model.info.name}' does not support streaming")
        if stream and not isinstance(self._model, StreamingModelProvider):
            raise ValueError(
                f"Provider '{self._model.info.name}' declares streaming but "
                "does not implement stream()"
            )
        if (self._memory_enabled or self._memory_write_enabled) and memory_scope is None:
            raise ValueError(
                "enabled Memory requires an explicit MemoryScope"
            )

        resolved_run_id = emitter.run_id
        snapshot = self._create_snapshot()
        await emitter.emit(
            "run.started",
            agent_id=snapshot.agent_id,
            agent_version=snapshot.agent_version,
            snapshot_fingerprint=snapshot.fingerprint,
            artifact_store=snapshot.artifact_store,
            resources=tuple(resource.name for resource in snapshot.resources),
        )
        user_message = Message.user(prompt, message_id=str(uuid4()))
        await emitter.emit(
            "message.created",
            message_id=user_message.message_id,
            role="user",
            content=prompt,
        )
        for skill in snapshot.skills:
            await emitter.emit(
                "skill.selected",
                skill_name=skill.name,
                skill_version=skill.version,
                package_fingerprint=skill.package_fingerprint,
            )

        try:
            await resource_manager.start()
        except Exception as exc:
            return await self._failed(
                emitter,
                snapshot,
                error=f"resource acquisition failed: {type(exc).__name__}: {exc}",
                failure=RunFailure(
                    code="resource_acquisition_failed",
                    message=str(exc),
                ),
            )

        try:
            await artifact_manager.validate_inputs()
        except Exception as exc:
            return await self._failed(
                emitter,
                snapshot,
                error=f"artifact input validation failed: {type(exc).__name__}: {exc}",
                failure=RunFailure(
                    code="artifact_input_invalid",
                    message=str(exc),
                ),
            )

        readiness_error = await self._check_readiness(emitter)
        if readiness_error is not None:
            return await self._failed(
                emitter,
                snapshot,
                error=readiness_error,
            )

        memory = await self._recall_memory(
            prompt,
            scope=memory_scope,
            run_id=resolved_run_id,
            emitter=emitter,
        )
        messages: list[Message] = [user_message]
        tool_results: list[ToolResult] = []
        observation_bytes = 0
        called_any_tool = False
        tools_by_name = {tool.spec.name: tool for tool in self._tools}

        for turn in range(1, self._definition.max_model_turns + 1):
            assistant_message_id = str(uuid4())
            is_final_turn = turn == self._definition.max_model_turns or not snapshot.tool_specs
            request_tools = () if is_final_turn else snapshot.tool_specs
            request_tool_choice = _tool_choice_for_turn(
                self._definition,
                called_any_tool=called_any_tool,
                is_final_turn=is_final_turn,
            )
            response_schema = _response_schema(self._definition)
            context_input = ContextInput(
                agent_id=snapshot.agent_id,
                run_id=resolved_run_id,
                model=snapshot.model_name,
                system_context=self._system_context,
                transcript=tuple(messages),
                tools=request_tools,
                response_schema=response_schema,
                turn=turn,
                budget=self._context_budget,
                memory=memory,
            )
            try:
                context_view = _select_context(
                    self._context_policy,
                    context_input,
                    policy_name=self._context_policy_name,
                )
            except ContextPolicyError as exc:
                error = f"context preparation failed: {exc}"
                await emitter.emit(
                    "context.failed",
                    turn=turn,
                    policy=self._context_policy_name,
                    error_type=type(exc.__cause__ or exc).__name__,
                )
                return await self._failed(
                    emitter,
                    snapshot,
                    error=error,
                    failure=RunFailure(
                        code="context_preparation_failed",
                        message=error,
                        details={"policy": self._context_policy_name},
                    ),
                    tool_results=tool_results,
                    turn=turn,
                )
            trace = context_view.trace
            await emitter.emit(
                "context.prepared",
                turn=turn,
                policy=trace.policy,
                source_system_prompt_fingerprint=(
                    trace.source_system_prompt_fingerprint
                ),
                selected_system_prompt_fingerprint=(
                    trace.selected_system_prompt_fingerprint
                ),
                system_prompt_changed=(
                    trace.source_system_prompt_fingerprint
                    != trace.selected_system_prompt_fingerprint
                ),
                selected_system_block_ids=trace.selected_system_block_ids,
                source_message_count=trace.source_message_count,
                selected_message_count=trace.selected_message_count,
                omitted_message_count=trace.omitted_message_count,
                omitted_tool_result_count=trace.omitted_tool_result_count,
                selected_message_ids=trace.selected_message_ids,
                omitted_message_ids=trace.omitted_message_ids,
                memory_enabled=memory.enabled,
                memory_provider=memory.provider,
                memory_candidate_count=trace.memory_candidate_count,
                selected_memory_ids=trace.selected_memory_ids,
                estimated_input_tokens=trace.estimated_input_tokens,
                max_input_tokens=self._context_budget.max_input_tokens,
            )
            request = ModelRequest(
                model=snapshot.model_name,
                system_prompt=context_view.system_prompt,
                messages=context_view.messages,
                tools=request_tools,
                tool_choice=request_tool_choice,
                response_schema=response_schema,
            )
            await emitter.emit(
                "model.requested",
                turn=turn,
                finalization=is_final_turn,
                message_count=len(context_view.messages),
                tool_count=len(request_tools),
            )
            started_at = monotonic()
            try:
                response = await self._complete_model_turn(
                    request,
                    stream=stream,
                    emitter=emitter,
                    turn=turn,
                    message_id=assistant_message_id,
                )
            except Exception as exc:
                failure = _model_failure(exc)
                await emitter.emit(
                    "model.failed",
                    turn=turn,
                    error_type=type(exc).__name__,
                    error_code=failure.code,
                    retryable=failure.retryable,
                )
                return await self._failed(
                    emitter,
                    snapshot,
                    error=failure.message,
                    failure=failure,
                    tool_results=tool_results,
                    turn=turn,
                )
            await emitter.emit(
                "model.responded",
                turn=turn,
                finalization=is_final_turn,
                duration_ms=int((monotonic() - started_at) * 1000),
                finish_reason=response.finish_reason.value,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                tool_call_count=len(response.tool_calls),
            )
            await emitter.emit(
                "message.completed",
                message_id=assistant_message_id,
                role="assistant",
                turn=turn,
                content=response.content or "",
                tool_call_count=len(response.tool_calls),
            )

            if is_final_turn and response.tool_calls:
                return await self._failed(
                    emitter,
                    snapshot,
                    error="final model turn requested Tools while Tool use was disabled",
                    tool_results=tool_results,
                    turn=turn,
                )

            if not response.tool_calls:
                if self._definition.tool_choice.value == "required" and not called_any_tool:
                    return await self._failed(
                        emitter,
                        snapshot,
                        error="model completed without the required Tool call",
                        tool_results=tool_results,
                        turn=turn,
                    )
                response_content = response.content or ""
                output, evidence, validation_error = self._validate_output(response_content)
                if validation_error is not None:
                    return await self._failed(
                        emitter,
                        snapshot,
                        error=validation_error,
                        tool_results=tool_results,
                        evidence=evidence,
                        turn=turn,
                    )
                try:
                    await resource_manager.close()
                except Exception as exc:
                    await emitter.emit(
                        "resource.release_failed",
                        turn=turn,
                        error_type=type(exc).__name__,
                    )
                    return await self._failed(
                        emitter,
                        snapshot,
                        error=f"resource cleanup failed: {type(exc).__name__}: {exc}",
                        failure=RunFailure(
                            code="resource_cleanup_failed",
                            message=str(exc),
                        ),
                        tool_results=tool_results,
                        evidence=evidence,
                        turn=turn,
                    )
                await self._queue_memory_write(
                    scope=memory_scope,
                    run_id=resolved_run_id,
                    messages=(
                        *messages,
                        Message.assistant(
                            response_content,
                            message_id=assistant_message_id,
                        ),
                    ),
                    output=response_content,
                    turn=turn,
                    emitter=emitter,
                )
                await emitter.emit(
                    "run.succeeded",
                    turn=turn,
                    output=_event_output(output),
                    text=response_content,
                )
                return RunResult(
                    run_id=resolved_run_id,
                    status=RunStatus.SUCCEEDED,
                    output=output,
                    text=response_content,
                    snapshot=snapshot,
                    events=await emitter.events(),
                    tool_results=tuple(tool_results),
                    evidence=evidence,
                    artifacts=await artifact_manager.list_outputs(),
                    artifact_inputs=await artifact_manager.list_inputs(),
                )

            if self._definition.tool_choice.value == "none":
                return await self._failed(
                    emitter,
                    snapshot,
                    error="model requested a Tool while tool_choice is 'none'",
                    tool_results=tool_results,
                    turn=turn,
                )

            called_any_tool = True
            messages.append(
                Message.assistant(
                    response.content or "",
                    message_id=assistant_message_id,
                    tool_calls=response.tool_calls,
                )
            )
            accepted_calls = response.tool_calls[: self._definition.max_tool_calls_per_turn]
            overflow_calls = response.tool_calls[self._definition.max_tool_calls_per_turn :]
            if overflow_calls:
                await emitter.emit(
                    "tool.batch_limited",
                    turn=turn,
                    requested=len(response.tool_calls),
                    accepted=len(accepted_calls),
                    skipped=len(overflow_calls),
                    limit=self._definition.max_tool_calls_per_turn,
                )

            turn_results: list[ToolResult] = []
            for call in accepted_calls:
                result = await self._execute_tool(
                    call,
                    turn=turn,
                    tools_by_name=tools_by_name,
                    emitter=emitter,
                    artifact_manager=artifact_manager,
                    resource_manager=resource_manager,
                )
                turn_results.append(result)
            for call in overflow_calls:
                result = ToolResult(
                    call=call,
                    status=ToolResultStatus.LIMIT_EXCEEDED,
                    error=(
                        "Tool call was not executed because the model turn "
                        f"exceeded max_tool_calls_per_turn="
                        f"{self._definition.max_tool_calls_per_turn}"
                    ),
                )
                await emitter.emit(
                    "tool.completed",
                    turn=turn,
                    tool_name=call.name,
                    tool_call_id=call.id,
                    status=result.status.value,
                )
                turn_results.append(result)

            for result in turn_results:
                tool_results.append(result)
                created_artifacts = tuple(
                    artifact
                    for artifact in await artifact_manager.list_outputs()
                    if artifact.tool_call_id == result.call.id
                )
                observation = _tool_result_observation(
                    result,
                    artifacts_created=created_artifacts,
                    max_bytes=self._definition.max_tool_observation_bytes,
                )
                observation_bytes += len(observation.encode("utf-8"))
                if observation_bytes > self._definition.max_run_observation_bytes:
                    return await self._failed(
                        emitter,
                        snapshot,
                        error=(
                            "Tool observations exceeded the per-Run context budget "
                            f"({observation_bytes} > "
                            f"{self._definition.max_run_observation_bytes} bytes)"
                        ),
                        failure=RunFailure(
                            code="tool_observation_budget_exceeded",
                            message="Tool observations exceeded the per-Run context budget",
                            details={
                                "observed_bytes": observation_bytes,
                                "max_run_observation_bytes": (
                                    self._definition.max_run_observation_bytes
                                ),
                                "max_tool_observation_bytes": (
                                    self._definition.max_tool_observation_bytes
                                ),
                            },
                        ),
                        tool_results=tool_results,
                        turn=turn,
                    )
                messages.append(
                    Message.tool_result(
                        result.call,
                        observation,
                        message_id=str(uuid4()),
                    )
                )
                if (
                    result.status is ToolResultStatus.SUCCESS
                    and result.call.name == SKILL_VIEW_TOOL_NAME
                ):
                    await _emit_skill_view_event(
                        result,
                        turn=turn,
                        emitter=emitter,
                    )

        return await self._failed(
            emitter,
            snapshot,
            error="Agent exhausted max_model_turns without a final answer",
            tool_results=tool_results,
            turn=self._definition.max_model_turns,
        )

    async def _complete_model_turn(
        self,
        request: ModelRequest,
        *,
        stream: bool,
        emitter: EventEmitter,
        turn: int,
        message_id: str,
    ) -> ModelResponse:
        if not stream:
            return await self._model.complete(request)

        if not isinstance(self._model, StreamingModelProvider):
            raise RuntimeError("streaming Provider invariant was not validated")

        text_parts: list[str] = []
        completed: ModelStreamCompleted | None = None
        async for event in self._model.stream(request):
            if completed is not None:
                raise InvalidProviderResponseError(
                    "streaming Provider emitted data after completion",
                    provider=self._model.info.name,
                )
            if isinstance(event, ModelTextDelta):
                text_parts.append(event.text)
                await emitter.emit(
                    "message.delta",
                    message_id=message_id,
                    role="assistant",
                    turn=turn,
                    delta=event.text,
                )
                continue
            if isinstance(event, ModelToolCallDelta):
                await emitter.emit(
                    "model.tool_call.delta",
                    turn=turn,
                    index=event.index,
                    tool_call_id=event.id,
                    tool_name=event.name,
                    argument_bytes=len(event.arguments_delta.encode("utf-8")),
                )
                continue
            if isinstance(event, ModelStreamCompleted):
                completed = event
                continue
            raise InvalidProviderResponseError(
                f"streaming Provider emitted unsupported event '{type(event).__name__}'",
                provider=self._model.info.name,
            )

        if completed is None:
            raise InvalidProviderResponseError(
                "streaming Provider ended without a completed response",
                provider=self._model.info.name,
            )
        streamed_text = "".join(text_parts)
        if text_parts and completed.response.content != streamed_text:
            raise InvalidProviderResponseError(
                "streamed text does not match the completed response",
                provider=self._model.info.name,
            )
        return completed.response

    def _create_snapshot(self) -> HarnessSnapshot:
        system_prompt = self._system_context.render()
        skill_snapshots = tuple(_skill_snapshot(skill) for skill in self._skills)
        tool_specs = (
            ()
            if self._definition.tool_choice.value == "none"
            else tuple(tool.spec for tool in self._tools)
        )
        model_name = self._definition.model or self._model.info.default_model
        if model_name is None:  # Agent construction validates this invariant.
            raise RuntimeError("Run has no resolved model")
        payload = {
            "agent_id": self._definition.id,
            "agent_version": self._definition.version,
            "model_name": model_name,
            "provider_name": self._model.info.name,
            "permissions": sorted(self._definition.permissions),
            "skills": [
                {
                    "name": skill.name,
                    "version": skill.version,
                    "description": skill.description,
                    "package_fingerprint": skill.package_fingerprint,
                    "required_tools": skill.required_tools,
                    "optional_tools": skill.optional_tools,
                    "provided_tools": skill.provided_tools,
                    "required_permissions": skill.required_permissions,
                    "optional_permissions": skill.optional_permissions,
                    "scripts": skill.scripts,
                    "script_runtimes": skill.script_runtimes,
                    "script_permission": skill.script_permission,
                    "script_timeout_seconds": skill.script_timeout_seconds,
                    "script_max_output_bytes": skill.script_max_output_bytes,
                    "references": skill.references,
                    "templates": skill.templates,
                    "assets": skill.assets,
                }
                for skill in skill_snapshots
            ],
            "system_prompt": system_prompt,
            "system_context_blocks": [
                {
                    "id": block.id,
                    "content": block.content,
                    "required": block.required,
                }
                for block in self._system_context.blocks
            ],
            "max_model_turns": self._definition.max_model_turns,
            "max_tool_calls_per_turn": (self._definition.max_tool_calls_per_turn),
            "max_tool_observation_bytes": (
                self._definition.max_tool_observation_bytes
            ),
            "max_run_observation_bytes": self._definition.max_run_observation_bytes,
            "context_policy": self._context_policy_name,
            "context_budget": {
                "context_window_tokens": self._context_budget.context_window_tokens,
                "reserved_output_tokens": self._context_budget.reserved_output_tokens,
                "safety_margin_tokens": self._context_budget.safety_margin_tokens,
            },
            "memory_provider": self._memory_provider_name,
            "memory_enabled": self._memory_enabled,
            "memory_writer": self._memory_writer_name,
            "memory_write_enabled": self._memory_write_enabled,
            "memory_recall_limit": self._memory_recall_limit,
            "memory_recall_timeout_seconds": self._memory_recall_timeout_seconds,
            "artifact_store": type(self._artifact_store).__name__,
            "resources": [
                {
                    "name": spec.name,
                    "scope": spec.scope.value,
                    "eager": spec.eager,
                }
                for spec in self._resources
            ],
            "tool_specs": [
                {
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                    "name": spec.name,
                }
                for spec in tool_specs
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return HarnessSnapshot(
            agent_id=self._definition.id,
            agent_version=self._definition.version,
            system_prompt=system_prompt,
            model_name=model_name,
            provider_name=self._model.info.name,
            tool_specs=tool_specs,
            skills=skill_snapshots,
            permissions=self._definition.permissions,
            max_model_turns=self._definition.max_model_turns,
            max_tool_calls_per_turn=(self._definition.max_tool_calls_per_turn),
            max_tool_observation_bytes=(
                self._definition.max_tool_observation_bytes
            ),
            max_run_observation_bytes=self._definition.max_run_observation_bytes,
            context_policy=self._context_policy_name,
            context_window_tokens=self._context_budget.context_window_tokens,
            context_reserved_output_tokens=self._context_budget.reserved_output_tokens,
            context_safety_margin_tokens=self._context_budget.safety_margin_tokens,
            system_context_block_ids=self._system_context.block_ids,
            required_system_context_block_ids=self._system_context.required_block_ids,
            memory_provider=self._memory_provider_name,
            memory_writer=self._memory_writer_name,
            memory_recall_limit=self._memory_recall_limit,
            memory_recall_timeout_seconds=self._memory_recall_timeout_seconds,
            artifact_store=type(self._artifact_store).__name__,
            resources=tuple(
                ResourceSnapshot(
                    name=spec.name,
                    scope=spec.scope.value,
                    eager=spec.eager,
                )
                for spec in self._resources
            ),
            fingerprint=fingerprint,
        )

    async def _check_readiness(self, emitter: EventEmitter) -> str | None:
        for tool in self._tools:
            try:
                readiness = await tool.check_readiness()
            except Exception as exc:
                await emitter.emit(
                    "tool.not_ready",
                    tool_name=tool.spec.name,
                    detail=f"readiness check failed: {type(exc).__name__}",
                )
                return f"Tool '{tool.spec.name}' readiness check failed: {exc}"
            if not readiness.ready:
                await emitter.emit(
                    "tool.not_ready",
                    tool_name=tool.spec.name,
                    detail=readiness.detail,
                )
                detail = f": {readiness.detail}" if readiness.detail else ""
                return f"Tool '{tool.spec.name}' is not ready{detail}"
        await emitter.emit("run.ready", tool_count=len(self._tools))
        return None

    async def _recall_memory(
        self,
        query: str,
        *,
        scope: MemoryScope | None,
        run_id: str,
        emitter: EventEmitter,
    ) -> MemorySnapshot:
        if not self._memory_enabled:
            return MemorySnapshot.disabled()
        if scope is None:  # run() validates this before emitting Run facts.
            raise RuntimeError("enabled Memory has no scope")

        try:
            async with asyncio.timeout(self._memory_recall_timeout_seconds):
                records = await self._memory_provider.recall(
                    MemoryRecallRequest(
                        agent_id=self._definition.id,
                        run_id=run_id,
                        query=query,
                        scope=scope,
                        limit=self._memory_recall_limit,
                    )
                )
            if not isinstance(records, tuple) or not all(
                isinstance(record, MemoryRecord) for record in records
            ):
                raise TypeError("MemoryProvider.recall() must return tuple[MemoryRecord, ...]")
            if len(records) > self._memory_recall_limit:
                raise ValueError(
                    "MemoryProvider returned more records than the requested recall limit"
                )
            memory = MemorySnapshot(
                enabled=True,
                provider=self._memory_provider_name,
                scope=scope,
                records=records,
            )
        except Exception as exc:
            await emitter.emit(
                "memory.recall.failed",
                turn=1,
                provider=self._memory_provider_name,
                error_type=type(exc).__name__,
            )
            return MemorySnapshot(
                enabled=True,
                provider=self._memory_provider_name,
                scope=scope,
            )

        await emitter.emit(
            "memory.recalled",
            turn=1,
            provider=self._memory_provider_name,
            candidate_count=len(memory.records),
        )
        return memory

    async def _queue_memory_write(
        self,
        *,
        scope: MemoryScope | None,
        run_id: str,
        messages: tuple[Message, ...],
        output: str,
        turn: int,
        emitter: EventEmitter,
    ) -> None:
        if not self._memory_write_enabled:
            return
        if scope is None:  # run() validates this before emitting Run facts.
            raise RuntimeError("enabled Memory writer has no scope")
        try:
            self._memory_writer.enqueue(
                MemoryObservation(
                    agent_id=self._definition.id,
                    run_id=run_id,
                    scope=scope,
                    messages=messages,
                    output=output,
                )
            )
        except Exception as exc:
            await emitter.emit(
                "memory.write.failed",
                turn=turn,
                writer=self._memory_writer_name,
                error_type=type(exc).__name__,
            )
            return
        await emitter.emit(
            "memory.write.queued",
            turn=turn,
            writer=self._memory_writer_name,
            message_count=len(messages),
        )

    async def _execute_tool(
        self,
        call: ToolCall,
        *,
        turn: int,
        tools_by_name: Mapping[str, Tool],
        emitter: EventEmitter,
        artifact_manager: ArtifactManager,
        resource_manager: ResourceManager,
    ) -> ToolResult:
        tool = tools_by_name.get(call.name)
        if tool is None:
            result = ToolResult(
                call=call,
                status=ToolResultStatus.NOT_FOUND,
                error=f"Tool '{call.name}' is not enabled for this Agent",
            )
            await emitter.emit(
                "tool.completed",
                turn=turn,
                tool_name=call.name,
                tool_call_id=call.id,
                status=result.status.value,
            )
            return result

        missing_permissions = tool.permissions - self._definition.permissions
        if missing_permissions:
            result = ToolResult(
                call=call,
                status=ToolResultStatus.DENIED,
                error=f"missing permissions: {', '.join(sorted(missing_permissions))}",
            )
            await emitter.emit(
                "tool.completed",
                turn=turn,
                tool_name=call.name,
                tool_call_id=call.id,
                status=result.status.value,
            )
            return result

        try:
            arguments = tool.validate_arguments(call.arguments)
        except Exception as exc:
            result = ToolResult(
                call=call,
                status=ToolResultStatus.INVALID_ARGUMENTS,
                error=str(exc),
            )
            await emitter.emit(
                "tool.completed",
                turn=turn,
                tool_name=call.name,
                tool_call_id=call.id,
                status=result.status.value,
            )
            return result

        if tool.confirmation is Confirmation.REQUIRED:
            try:
                approved = await self._confirmation_policy.confirm(
                    ConfirmationRequest(
                        run_id=emitter.run_id,
                        agent_id=self._definition.id,
                        tool_name=call.name,
                        arguments=arguments,
                        side_effect=tool.side_effect,
                    )
                )
            except Exception:
                # Governance is fail-closed. An unavailable confirmation
                # channel must never become implicit approval.
                approved = False
            await emitter.emit(
                "tool.confirmation",
                turn=turn,
                tool_name=call.name,
                tool_call_id=call.id,
                approved=approved,
            )
            if not approved:
                result = ToolResult(
                    call=call,
                    status=ToolResultStatus.CONFIRMATION_DENIED,
                    error="Tool execution was not confirmed",
                )
                await emitter.emit(
                    "tool.completed",
                    turn=turn,
                    tool_name=call.name,
                    tool_call_id=call.id,
                    status=result.status.value,
                )
                return result

        skill_resource = self._skill_resources_by_tool.get(call.name)
        if skill_resource is not None:
            resource = call.name
            if skill_resource.resource_type == "script":
                script = arguments.get("script")
                if isinstance(script, str):
                    resource = script
            await emitter.emit(
                "skill.resource.used",
                turn=turn,
                skill_name=skill_resource.skill.name,
                skill_version=skill_resource.skill.version,
                package_fingerprint=(
                    skill_resource.skill.package.fingerprint
                    if skill_resource.skill.package is not None
                    else None
                ),
                resource_type=skill_resource.resource_type,
                resource=resource,
                tool_name=call.name,
                tool_call_id=call.id,
            )

        started_at = monotonic()
        tool_started = False
        try:
            await emitter.emit(
                "tool.started",
                turn=turn,
                tool_name=call.name,
                tool_call_id=call.id,
                side_effect=tool.side_effect.value,
            )
            tool_started = True
            data = await asyncio.wait_for(
                tool.invoke(
                    arguments,
                    ToolContext(
                        run_id=emitter.run_id,
                        agent_id=self._definition.id,
                        tool_call_id=call.id,
                        artifacts=artifact_manager.for_tool(call.id),
                        resources=resource_manager,
                    ),
                ),
                timeout=tool.timeout_seconds,
            )
        except asyncio.CancelledError:
            if tool_started or await _has_open_tool_event(emitter, call.id):
                await emitter.emit(
                    "tool.completed",
                    turn=turn,
                    tool_name=call.name,
                    tool_call_id=call.id,
                    status=ToolResultStatus.CANCELLED.value,
                    duration_ms=int((monotonic() - started_at) * 1000),
                )
            raise
        except TimeoutError:
            result = ToolResult(
                call=call,
                status=ToolResultStatus.TIMEOUT,
                error=f"Tool exceeded {tool.timeout_seconds:g}s timeout",
            )
        except Exception as exc:
            result = ToolResult(
                call=call,
                status=ToolResultStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = ToolResult(
                call=call,
                status=ToolResultStatus.SUCCESS,
                data=data,
            )
        await emitter.emit(
            "tool.completed",
            turn=turn,
            tool_name=call.name,
            tool_call_id=call.id,
            status=result.status.value,
            duration_ms=int((monotonic() - started_at) * 1000),
        )
        return result

    def _validate_output(
        self,
        content: str,
    ) -> tuple[Any, tuple[VerificationEvidence, ...], str | None]:
        response_model = self._definition.response_model
        if response_model is None:
            return content, (), None
        try:
            output = response_model.model_validate_json(content)
        except ValidationError as exc:
            evidence = (
                VerificationEvidence(
                    verifier="response_schema",
                    scope="final_output",
                    status="failed",
                    detail=str(exc),
                ),
            )
            return None, evidence, f"final output failed response schema validation: {exc}"
        evidence = (
            VerificationEvidence(
                verifier="response_schema",
                scope="final_output",
                status="passed",
            ),
        )
        return output, evidence, None

    async def _failed(
        self,
        emitter: EventEmitter,
        snapshot: HarnessSnapshot,
        *,
        error: str,
        tool_results: Sequence[ToolResult] = (),
        evidence: tuple[VerificationEvidence, ...] = (),
        failure: RunFailure | None = None,
        turn: int | None = None,
    ) -> RunResult:
        await emitter.emit("run.failed", turn=turn, error=error)
        return RunResult(
            run_id=emitter.run_id,
            status=RunStatus.FAILED,
            output=None,
            text="",
            snapshot=snapshot,
            events=await emitter.events(),
            tool_results=tuple(tool_results),
            evidence=evidence,
            error=error,
            failure=failure
            or RunFailure(
                code="run_failed",
                message=error,
            ),
            artifacts=await self._artifact_store.list(run_id=emitter.run_id),
        )

    async def cancelled_result(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> RunResult:
        """Persist the terminal result for a cancelled background Run."""
        emitter = EventEmitter(run_id, self._event_store, self._observers)
        existing_events = await emitter.events()
        if existing_events and existing_events[-1].kind in {
            "run.succeeded",
            "run.failed",
            "run.cancelled",
        }:
            return await self._result_from_terminal_events(run_id, existing_events)
        turn = next(
            (event.turn for event in reversed(existing_events) if event.turn is not None),
            None,
        )
        await emitter.emit("run.cancelled", turn=turn, reason=reason)
        return RunResult(
            run_id=run_id,
            status=RunStatus.CANCELLED,
            output=None,
            text="",
            snapshot=self._create_snapshot(),
            events=await emitter.events(),
            error=reason,
            artifacts=await self._artifact_store.list(run_id=run_id),
        )

    async def _result_from_terminal_events(
        self,
        run_id: str,
        events: tuple[Event, ...],
    ) -> RunResult:
        terminal = events[-1]
        snapshot = self._create_snapshot()
        if terminal.kind == "run.succeeded":
            completed = next(
                (
                    event
                    for event in reversed(events)
                    if event.kind == "message.completed"
                ),
                None,
            )
            text = completed.payload.get("content", "") if completed is not None else ""
            resolved_text = text if isinstance(text, str) else str(text)
            output, evidence, validation_error = self._validate_output(resolved_text)
            if validation_error is not None:
                raise RuntimeError(
                    f"Run '{run_id}' committed success with invalid output: "
                    f"{validation_error}"
                )
            return RunResult(
                run_id=run_id,
                status=RunStatus.SUCCEEDED,
                output=output,
                text=resolved_text,
                snapshot=snapshot,
                events=events,
                evidence=evidence,
                artifacts=await self._artifact_store.list(run_id=run_id),
            )
        error = terminal.payload.get("error") or terminal.payload.get("reason")
        resolved_error = str(error or terminal.kind)
        status = (
            RunStatus.FAILED
            if terminal.kind == "run.failed"
            else RunStatus.CANCELLED
        )
        return RunResult(
            run_id=run_id,
            status=status,
            output=None,
            text="",
            snapshot=snapshot,
            events=events,
            error=resolved_error,
            failure=(
                RunFailure(code="run_failed", message=resolved_error)
                if status is RunStatus.FAILED
                else None
            ),
            artifacts=await self._artifact_store.list(run_id=run_id),
        )


def _event_output(output: Any) -> Any:
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    return output


def _skill_snapshot(skill: Skill) -> SkillSnapshot:
    package = skill.package
    script_policy = skill.script_execution
    provided_tools = tuple(tool.spec.name for tool in flatten_tools(skill.contributions.tools))
    return SkillSnapshot(
        name=skill.name,
        version=skill.version,
        description=skill.description,
        package_fingerprint=(package.fingerprint if package is not None else None),
        required_tools=skill.dependencies.required_tools,
        optional_tools=skill.dependencies.optional_tools,
        provided_tools=provided_tools,
        required_permissions=tuple(sorted(skill.permissions.required)),
        optional_permissions=tuple(sorted(skill.permissions.optional)),
        scripts=package.script_files if package is not None else (),
        script_runtimes=tuple(sorted(runtime.value for runtime in script_policy.runtimes)),
        script_permission=(script_policy.permission if script_policy.enabled else None),
        script_timeout_seconds=(script_policy.timeout_seconds if script_policy.enabled else None),
        script_max_output_bytes=(script_policy.max_output_bytes if script_policy.enabled else None),
        references=package.references if package is not None else (),
        templates=package.templates if package is not None else (),
        assets=package.assets if package is not None else (),
    )


async def _emit_skill_view_event(
    result: ToolResult,
    *,
    turn: int,
    emitter: EventEmitter,
) -> None:
    data = result.data
    if not isinstance(data, Mapping):
        return
    name = data.get("name")
    version = data.get("version")
    file_path = data.get("file")
    if not isinstance(name, str) or not isinstance(file_path, str):
        return
    payload = {
        "skill_name": name,
        "skill_version": version,
        "package_fingerprint": data.get("package_fingerprint"),
        "file": file_path,
        "tool_call_id": result.call.id,
    }
    await emitter.emit(
        "skill.loaded" if file_path == "SKILL.md" else "skill.file.loaded",
        turn=turn,
        **payload,
    )


def _response_schema(definition: AgentDefinition) -> Mapping[str, Any] | None:
    if definition.response_model is None:
        return None
    return definition.response_model.model_json_schema()


def _select_context(
    policy: ContextPolicy,
    context: ContextInput,
    *,
    policy_name: str,
) -> ContextView:
    try:
        view = policy.select(context)
    except Exception as exc:
        raise ContextPolicyError(
            f"policy '{policy_name}' raised {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(view, ContextView):
        raise ContextPolicyError(
            f"policy '{policy_name}' must return ContextView, got {type(view).__name__}"
        )
    if view.trace.policy != policy_name:
        raise ContextPolicyError(
            f"policy trace name '{view.trace.policy}' does not match '{policy_name}'"
        )
    if view.trace.source_system_prompt_fingerprint != context.system_prompt_fingerprint:
        raise ContextPolicyError(
            "policy trace source_system_prompt_fingerprint does not match SystemContext"
        )
    try:
        expected_system_prompt = context.system_context.render(
            view.trace.selected_system_block_ids
        )
        expected_system_prompt_fingerprint = context.system_context.fingerprint(
            view.trace.selected_system_block_ids
        )
    except ValueError as exc:
        raise ContextPolicyError(f"invalid system context selection: {exc}") from exc
    if view.system_prompt != expected_system_prompt:
        raise ContextPolicyError(
            "ContextView.system_prompt must be rendered from the selected SystemContext blocks"
        )
    if (
        view.trace.selected_system_prompt_fingerprint
        != expected_system_prompt_fingerprint
    ):
        raise ContextPolicyError(
            "policy trace selected_system_prompt_fingerprint does not match ContextView"
        )
    if view.trace.source_message_count != len(context.transcript):
        raise ContextPolicyError("policy trace source_message_count does not match transcript")
    if view.trace.selected_message_count != len(view.messages):
        raise ContextPolicyError("policy trace selected_message_count does not match ContextView")
    if view.trace.omitted_message_count > len(context.transcript):
        raise ContextPolicyError("policy trace omitted_message_count exceeds transcript")
    selected_message_ids = tuple(
        message.message_id for message in view.messages if message.message_id is not None
    )
    if view.trace.selected_message_ids != selected_message_ids:
        raise ContextPolicyError("policy trace selected_message_ids does not match ContextView")
    source_message_ids = tuple(
        message.message_id for message in context.transcript if message.message_id is not None
    )
    if len(source_message_ids) == len(context.transcript):
        source_id_set = set(source_message_ids)
        selected_source_ids = source_id_set.intersection(view.trace.selected_message_ids)
        omitted_id_set = set(view.trace.omitted_message_ids)
        if not omitted_id_set.issubset(source_id_set):
            raise ContextPolicyError("policy trace omitted_message_ids are not in transcript")
        if selected_source_ids.intersection(omitted_id_set):
            raise ContextPolicyError("policy trace selected and omitted Message ids overlap")
        if selected_source_ids.union(omitted_id_set) != source_id_set:
            raise ContextPolicyError("policy trace does not account for every source Message id")
        if len(view.trace.omitted_message_ids) != view.trace.omitted_message_count:
            raise ContextPolicyError(
                "policy trace omitted_message_ids does not match omitted_message_count"
            )
    if view.trace.memory_candidate_count != len(context.memory.records):
        raise ContextPolicyError("policy trace memory_candidate_count does not match Memory")
    candidate_memory_ids = {record.id for record in context.memory.records}
    if not set(view.trace.selected_memory_ids).issubset(candidate_memory_ids):
        raise ContextPolicyError("policy selected a Memory id that was not recalled")
    max_input_tokens = context.budget.max_input_tokens
    if (
        max_input_tokens is not None
        and view.trace.estimated_input_tokens is not None
        and view.trace.estimated_input_tokens > max_input_tokens
    ):
        raise ContextPolicyError(
            "selected context exceeds max_input_tokens: "
            f"{view.trace.estimated_input_tokens} > {max_input_tokens}"
        )
    _validate_context_messages(view.messages)
    return view


def _validate_context_messages(messages: tuple[Message, ...]) -> None:
    if not messages:
        raise ContextPolicyError("ContextView messages must not be empty")
    if not any(message.role == "user" for message in messages):
        raise ContextPolicyError("ContextView must retain a user message")

    pending_tool_call_ids: set[str] = set()
    for message in messages:
        if message.role not in {"user", "assistant", "tool"}:
            raise ContextPolicyError(
                f"ContextView contains unsupported Message role '{message.role}'"
            )
        if pending_tool_call_ids:
            if message.role != "tool" or message.tool_call_id not in pending_tool_call_ids:
                raise ContextPolicyError(
                    "ContextView split an assistant ToolCall from its Tool result group"
                )
            pending_tool_call_ids.remove(message.tool_call_id)
            continue
        if message.role == "tool":
            raise ContextPolicyError("ContextView contains an orphan Tool result")
        if message.role == "assistant" and message.tool_calls:
            tool_call_ids = {call.id for call in message.tool_calls}
            if len(tool_call_ids) != len(message.tool_calls):
                raise ContextPolicyError("ContextView contains duplicate ToolCall ids")
            pending_tool_call_ids = tool_call_ids

    if pending_tool_call_ids:
        raise ContextPolicyError("ContextView is missing one or more Tool results")


async def _has_open_tool_event(emitter: EventEmitter, tool_call_id: str) -> bool:
    for event in reversed(await emitter.events()):
        if event.payload.get("tool_call_id") != tool_call_id:
            continue
        if event.kind == "tool.completed":
            return False
        if event.kind == "tool.started":
            return True
    return False


def _index_skill_resources(
    skills: tuple[Skill, ...],
) -> dict[str, _SkillResourceSource]:
    resources: dict[str, _SkillResourceSource] = {}
    for skill in skills:
        for tool in flatten_tools(skill.contributions.tools):
            resources[tool.spec.name] = _SkillResourceSource(
                skill=skill,
                resource_type="tool",
            )
        if skill.script_execution.enabled:
            resources[skill_script_tool_name(skill)] = _SkillResourceSource(
                skill=skill,
                resource_type="script",
            )
    return resources


def _tool_choice_for_turn(
    definition: AgentDefinition,
    *,
    called_any_tool: bool,
    is_final_turn: bool,
) -> ToolChoice:
    if is_final_turn or definition.tool_choice.value == "none":
        return ToolChoice.NONE
    if definition.tool_choice.value == "required" and called_any_tool:
        return ToolChoice.AUTO
    return definition.tool_choice


def _tool_result_observation(
    result: ToolResult,
    *,
    artifacts_created: Sequence[Artifact] = (),
    max_bytes: int,
) -> str:
    artifact_receipts = [
        {
            "artifact_id": artifact.id,
            "name": artifact.name,
            "media_type": artifact.media_type,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "source_artifact_ids": list(artifact.source_artifact_ids),
        }
        for artifact in artifacts_created
    ]
    payload = {
        "status": result.status.value,
        "data": result.data,
        "error": result.error,
        "artifacts_created": artifact_receipts,
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    visible_receipts = list(artifact_receipts)
    omitted_receipts = 0
    preview_bytes = encoded[: max(0, max_bytes - 512)]
    while True:
        preview = preview_bytes.decode("utf-8", errors="ignore")
        truncated_payload = {
            "status": result.status.value,
            "data": None,
            "error": None,
            "artifacts_created": visible_receipts,
            "truncated": True,
            "original_bytes": len(encoded),
            "preview": preview,
        }
        if omitted_receipts:
            truncated_payload["artifacts_created_omitted"] = omitted_receipts
        truncated = json.dumps(truncated_payload, ensure_ascii=False)
        overflow = len(truncated.encode("utf-8")) - max_bytes
        if overflow <= 0:
            return truncated
        if preview_bytes:
            preview_bytes = preview_bytes[: max(0, len(preview_bytes) - overflow - 64)]
            continue
        if visible_receipts:
            visible_receipts.pop()
            omitted_receipts += 1
            continue
        raise RuntimeError("max_tool_observation_bytes is too small for observation metadata")


def _model_failure(exc: Exception) -> RunFailure:
    if isinstance(exc, ProviderError):
        details: dict[str, Any] = {"provider": exc.provider}
        if exc.status_code is not None:
            details["status_code"] = exc.status_code
        return RunFailure(
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            details=details,
        )
    return RunFailure(
        code="model_error",
        message=f"model request failed: {exc}",
    )
