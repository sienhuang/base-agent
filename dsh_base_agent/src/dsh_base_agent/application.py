"""Public composition root used by business Agent services."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Self

from fastapi import FastAPI

from dsh_base_agent._dsh import (
    DshRuntimeFactory,
    GeneratedSdkProfilePatches,
    SdkRuntimeConfig,
    SdkRuntimeFactory,
)
from dsh_base_agent.agent import Agent
from dsh_base_agent.app import BaseAgentApp
from dsh_base_agent.context import Context
from dsh_base_agent.definitions import AgentDefinition
from dsh_base_agent.events import EventStore
from dsh_base_agent.governance import ConfirmationPolicy
from dsh_base_agent.memory import Memory
from dsh_base_agent.models import Observer, RunResult
from dsh_base_agent.orchestration import PlannerReActFlow, SequentialFlow
from dsh_base_agent.providers import ProviderCatalog, ProviderConfig
from dsh_base_agent.server.app import create_http_app
from dsh_base_agent.skills import Skill, SkillCatalog
from dsh_base_agent.state import LocalState
from dsh_base_agent.tools import Tool, Toolkit, flatten_tools

type RegisteredFlow = SequentialFlow | PlannerReActFlow


class DshApplication:
    """Own framework infrastructure while business code registers capabilities.

    Business applications provide Context, Memory, Tool, Skill, Agent and Flow
    definitions.  This object owns provider resolution, DSH construction,
    state, lifecycle and the optional FastAPI control plane.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        state_dir: str | Path | None = None,
        memory: Memory | None = None,
        providers: Iterable[ProviderConfig] = (),
        default_provider: str | None = None,
        default_context: Context | None = None,
        runtime_factory: DshRuntimeFactory | None = None,
        dsh_bin: str | None = None,
        initialize_timeout_seconds: float = 30.0,
        request_timeout_seconds: float | None = None,
        shutdown_timeout_seconds: float | None = 5.0,
    ) -> None:
        resolved_workspace = Path(workspace).expanduser().resolve()
        if not resolved_workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {resolved_workspace}")
        resolved_state = (
            Path(state_dir).expanduser().resolve()
            if state_dir is not None
            else resolved_workspace / ".dsh-base-agent" / "state"
        )
        configured_providers = tuple(providers)
        if not configured_providers:
            configured_providers = (ProviderConfig.from_env(),)
        selected_default = default_provider or configured_providers[0].name
        self._providers = {item.name: item for item in configured_providers}
        self._provider_catalog = ProviderCatalog(
            configured_providers,
            default=selected_default,
        )
        resolved_factory = runtime_factory or SdkRuntimeFactory(
            SdkRuntimeConfig(
                workspace=resolved_workspace,
                dsh_bin=dsh_bin,
                initialize_timeout_seconds=initialize_timeout_seconds,
                request_timeout_seconds=request_timeout_seconds,
                shutdown_timeout_seconds=shutdown_timeout_seconds,
            ),
            patch_resolver=GeneratedSdkProfilePatches(),
        )
        self._services = BaseAgentApp(
            state=LocalState(resolved_state),
            runtime_factory=resolved_factory,
            memory=memory,
        )
        initial_context = default_context or Context(name="default")
        self._contexts: dict[str, Context] = {initial_context.name: initial_context}
        self.default_context = initial_context.name
        self._tools: dict[str, Tool] = {}
        self._skills: dict[str, Skill] = {}
        self._agents: dict[str, Agent] = {}
        self._flows: dict[str, RegisteredFlow] = {}
        self._default_agent: str | None = None
        self._http_created = False

    @property
    def event_store(self) -> EventStore:
        return self._services.event_store

    @property
    def providers(self) -> ProviderCatalog:
        return self._provider_catalog

    @property
    def contexts(self) -> Mapping[str, Context]:
        return MappingProxyType(self._contexts)

    @property
    def tools(self) -> Mapping[str, Tool]:
        return MappingProxyType(self._tools)

    @property
    def skills(self) -> Mapping[str, Skill]:
        return MappingProxyType(self._skills)

    @property
    def agents(self) -> Mapping[str, Agent]:
        return MappingProxyType(self._agents)

    @property
    def flows(self) -> Mapping[str, RegisteredFlow]:
        return MappingProxyType(self._flows)

    @property
    def default_agent(self) -> str | None:
        return self._default_agent

    def register_context(self, *contexts: Context, default: bool = False) -> Self:
        for context in contexts:
            if context.name in self._contexts:
                raise ValueError(f"Context '{context.name}' is already registered")
            self._contexts[context.name] = context
        if default:
            if len(contexts) != 1:
                raise ValueError("exactly one Context can be selected as default")
            self.default_context = contexts[0].name
        return self

    def register_tool(self, *items: Tool | Toolkit) -> Self:
        for tool in flatten_tools(items):
            if tool.spec.name in self._tools:
                raise ValueError(f"Tool '{tool.spec.name}' is already registered")
            self._tools[tool.spec.name] = tool
        return self

    def register_skill(self, *skills: Skill) -> Self:
        for skill in skills:
            if skill.name in self._skills:
                raise ValueError(f"Skill '{skill.name}' is already registered")
            self._skills[skill.name] = skill
        return self

    def register_agent(
        self,
        definition: AgentDefinition,
        *,
        context: str | Context | None = None,
        provider: str | None = None,
        confirmation_policy: ConfirmationPolicy | None = None,
        observers: Sequence[Observer] = (),
        allow_native_tools: bool = False,
        default: bool = False,
    ) -> Agent:
        if definition.id in self._agents:
            raise ValueError(f"Agent '{definition.id}' is already registered")
        selected_context = self._resolve_context(context)
        selected_provider = self._provider_catalog.resolve(provider)
        agent = Agent(
            definition,
            app=self._services,
            tools=tuple(self._tools.values()),
            skills=SkillCatalog(self._skills.values()),
            context_policy=selected_context.policy,
            context_blocks=selected_context.blocks,
            memory_scope=selected_context.memory_scope,
            memory_writeback=selected_context.memory_writeback,
            available_context_tokens=selected_context.available_tokens,
            confirmation_policy=confirmation_policy,
            observers=observers,
            provider=selected_provider.provider,
            model=selected_provider.model,
            reasoning_effort=selected_provider.reasoning_effort,
            max_tokens=selected_provider.max_tokens,
            base_url=selected_provider.base_url,
            api_key=selected_provider.api_key,
            allow_native_tools=allow_native_tools,
        )
        self._agents[definition.id] = agent
        if self._default_agent is None or default:
            self._default_agent = definition.id
        return agent

    def register_flow(
        self,
        flow: RegisteredFlow,
        *,
        name: str | None = None,
    ) -> Self:
        flow_name = name or flow.id
        if not flow_name.strip():
            raise ValueError("Flow registration name must not be blank")
        if flow_name in self._flows:
            raise ValueError(f"Flow '{flow_name}' is already registered")
        self._flows[flow_name] = flow
        return self

    def agent(self, name: str) -> Agent:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise ValueError(f"Agent '{name}' is not registered") from exc

    def flow(self, name: str) -> RegisteredFlow:
        try:
            return self._flows[name]
        except KeyError as exc:
            raise ValueError(f"Flow '{name}' is not registered") from exc

    async def run(
        self,
        prompt: str,
        *,
        agent: str | None = None,
    ) -> RunResult:
        return await self.agent(self._resolve_agent_name(agent)).run(prompt)

    async def prepare(self) -> None:
        for agent in self._agents.values():
            await agent.prepare()

    async def close(self) -> None:
        await self._services.close()

    def fastapi(
        self,
        *,
        default_agent: str | None = None,
        title: str = "DSH Base Agent",
        prefix: str = "/v1",
        sse_ping_seconds: int = 15,
    ) -> FastAPI:
        if self._http_created:
            raise RuntimeError("DshApplication.fastapi() can only be called once")
        selected_name = self._resolve_agent_name(default_agent)
        self._http_created = True
        application = create_http_app(
            self._services,
            self._agents[selected_name],
            agents=self._agents,
            flows=self._flows,
            title=title,
            prefix=prefix,
            sse_ping_seconds=sse_ping_seconds,
        )
        application.state.dsh_application = self
        application.state.default_agent_name = selected_name
        return application

    async def __aenter__(self) -> DshApplication:
        await self.prepare()
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.close()

    def _resolve_context(self, context: str | Context | None) -> Context:
        if isinstance(context, Context):
            existing = self._contexts.get(context.name)
            if existing is not None and existing != context:
                raise ValueError(f"Context '{context.name}' is already registered differently")
            self._contexts.setdefault(context.name, context)
            return context
        name = context or self.default_context
        try:
            return self._contexts[name]
        except KeyError as exc:
            raise ValueError(f"Context '{name}' is not registered") from exc

    def _resolve_agent_name(self, name: str | None) -> str:
        selected = name or self._default_agent
        if selected is None:
            raise ValueError("DshApplication requires at least one registered Agent")
        if selected not in self._agents:
            raise ValueError(f"Agent '{selected}' is not registered")
        return selected


__all__ = ["DshApplication", "RegisteredFlow"]
