"""Internal execution Harness used automatically by the public Agent facade."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from base_agent.definitions import AgentDefinition
from base_agent.events import EventStore
from base_agent.governance import (
    Confirmation,
    ConfirmationPolicy,
    ConfirmationRequest,
    DenyConfirmations,
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
    RunFailure,
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
from base_agent.skills import Skill, SkillCatalog, SkillDescriptor
from base_agent.skills.execution import skill_script_tool_name
from base_agent.skills.loading import SKILL_VIEW_TOOL_NAME
from base_agent.tools import Tool, flatten_tools
from base_agent.tools.core import ToolContext
from base_agent.verification import VerificationEvidence

_MAX_OBSERVATION_BYTES = 16_384
_MAX_TURN_OBSERVATION_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class _SkillResourceSource:
    skill: Skill
    resource_type: str


class _EventEmitter:
    def __init__(
        self,
        run_id: str,
        event_store: EventStore,
        observers: Sequence[Observer],
    ) -> None:
        self._run_id = run_id
        self._event_store = event_store
        self._observers = tuple(observers)

    async def events(self) -> tuple[Event, ...]:
        return await self._event_store.list(self._run_id)

    @property
    def run_id(self) -> str:
        return self._run_id

    async def emit(
        self,
        kind: str,
        *,
        turn: int | None = None,
        **payload: Any,
    ) -> None:
        event = await self._event_store.emit(
            self._run_id,
            kind,
            payload,
            turn=turn,
        )
        for observer in self._observers:
            try:
                value = observer(event)
                if inspect.isawaitable(value):
                    await value
            except Exception:
                # Observers report facts. They are deliberately fail-open and
                # have no return channel capable of changing execution.
                continue


class Harness:
    """Bounded model/tool runtime hidden behind ``Agent.run``."""

    def __init__(
        self,
        *,
        definition: AgentDefinition,
        model: ModelProvider,
        tools: tuple[Tool, ...],
        skills: tuple[Skill, ...],
        skill_catalog: SkillCatalog,
        confirmation_policy: ConfirmationPolicy | None,
        observers: Sequence[Observer],
        event_store: EventStore,
    ) -> None:
        self._definition = definition
        self._model = model
        self._tools = tools
        self._skills = skills
        self._skill_catalog = skill_catalog
        self._skill_resources_by_tool = _index_skill_resources(skills)
        self._confirmation_policy = confirmation_policy or DenyConfirmations()
        self._observers = tuple(observers)
        self._event_store = event_store

    async def run(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
        run_id: str | None = None,
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

        resolved_run_id = run_id or str(uuid4())
        run_observers = self._observers if on_event is None else (*self._observers, on_event)
        emitter = _EventEmitter(resolved_run_id, self._event_store, run_observers)
        snapshot = self._create_snapshot()
        await emitter.emit(
            "run.started",
            agent_id=snapshot.agent_id,
            agent_version=snapshot.agent_version,
            snapshot_fingerprint=snapshot.fingerprint,
        )
        await emitter.emit(
            "message.created",
            message_id=str(uuid4()),
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

        readiness_error = await self._check_readiness(emitter)
        if readiness_error is not None:
            return await self._failed(
                emitter,
                snapshot,
                error=readiness_error,
            )

        messages: list[Message] = [Message.user(prompt)]
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
            request = ModelRequest(
                model=snapshot.model_name,
                system_prompt=snapshot.system_prompt,
                messages=tuple(messages),
                tools=request_tools,
                tool_choice=request_tool_choice,
                response_schema=_response_schema(self._definition),
            )
            await emitter.emit(
                "model.requested",
                turn=turn,
                finalization=is_final_turn,
                message_count=len(messages),
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
                await emitter.emit("run.succeeded", turn=turn)
                return RunResult(
                    run_id=resolved_run_id,
                    status=RunStatus.SUCCEEDED,
                    output=output,
                    text=response_content,
                    snapshot=snapshot,
                    events=await emitter.events(),
                    tool_results=tuple(tool_results),
                    evidence=evidence,
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
                observation = _tool_result_observation(result)
                observation_bytes += len(observation.encode("utf-8"))
                if observation_bytes > _MAX_TURN_OBSERVATION_BYTES:
                    return await self._failed(
                        emitter,
                        snapshot,
                        error="Tool observations exceeded the per-Run context budget",
                        tool_results=tool_results,
                        turn=turn,
                    )
                messages.append(Message.tool_result(result.call, observation))
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
        emitter: _EventEmitter,
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
        system_prompt = _build_system_prompt(
            self._definition,
            self._skill_catalog.descriptors,
        )
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
            "max_model_turns": self._definition.max_model_turns,
            "max_tool_calls_per_turn": (self._definition.max_tool_calls_per_turn),
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
            fingerprint=fingerprint,
        )

    async def _check_readiness(self, emitter: _EventEmitter) -> str | None:
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

    async def _execute_tool(
        self,
        call: ToolCall,
        *,
        turn: int,
        tools_by_name: Mapping[str, Tool],
        emitter: _EventEmitter,
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

        await emitter.emit(
            "tool.started",
            turn=turn,
            tool_name=call.name,
            tool_call_id=call.id,
            side_effect=tool.side_effect.value,
        )
        started_at = monotonic()
        try:
            data = await asyncio.wait_for(
                tool.invoke(
                    arguments,
                    ToolContext(
                        run_id=emitter.run_id,
                        agent_id=self._definition.id,
                        tool_call_id=call.id,
                    ),
                ),
                timeout=tool.timeout_seconds,
            )
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
        emitter: _EventEmitter,
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
        )


def _build_system_prompt(
    definition: AgentDefinition,
    skills: Sequence[SkillDescriptor],
) -> str:
    sections = [definition.instructions.strip()]
    if skills:
        skill_lines = []
        for skill in skills:
            detail = f": {skill.description}" if skill.description else ""
            skill_lines.append(f"- `{skill.name}` (version {skill.version}){detail}")
        sections.append(
            "# Available skills\n\n"
            "The Skills below are available for this Run. Before following a "
            "Skill, call `skill_view` with its name to load the complete "
            "instructions. Load supporting files only when the playbook "
            "requires them.\n\n" + "\n".join(skill_lines)
        )
    return "\n\n".join(sections)


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
    emitter: _EventEmitter,
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


def _tool_result_observation(result: ToolResult) -> str:
    payload = {
        "status": result.status.value,
        "data": result.data,
        "error": result.error,
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_OBSERVATION_BYTES:
        return text
    preview_bytes = encoded[: _MAX_OBSERVATION_BYTES - 512]
    preview = preview_bytes.decode("utf-8", errors="ignore")
    return json.dumps(
        {
            "status": result.status.value,
            "data": None,
            "error": result.error,
            "truncated": True,
            "preview": preview,
        },
        ensure_ascii=False,
    )


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
