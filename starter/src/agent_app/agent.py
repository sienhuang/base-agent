"""Single composition root for the application Agent."""

from pathlib import Path

from base_agent import (
    Agent,
    AgentDefinition,
    AgentRuntime,
    BraveWebSearchProvider,
    MtbiCliDataSource,
    ReActStrategy,
    SkillRegistry,
    Tool,
    data_source_bundle,
    docker_coding_bundle,
    web_search_bundle,
)
from base_agent.logging import configure_file_logging
from base_agent.resources import ResourceSpec

from agent_app.config import Settings
from agent_app.providers import build_provider
from agent_app.tools import ENABLED_PERMISSIONS, ENABLED_TOOL_NAMES, REGISTERED_TOOLS

SKILLS_ROOT = Path(__file__).parent / "skills"


def build_agent(
    settings: Settings | None = None,
    *,
    react: bool = False,
) -> Agent:
    resolved = settings or Settings.from_env()
    configure_file_logging()
    registry = SkillRegistry.from_directory(SKILLS_ROOT)
    registered_tools: list[Tool] = list(REGISTERED_TOOLS)
    enabled_tool_names = list(ENABLED_TOOL_NAMES)
    enabled_permissions = set(ENABLED_PERMISSIONS)
    resources: list[ResourceSpec] = []

    if resolved.enable_coding:
        if resolved.sandbox_image is None:
            raise ValueError(
                "AGENT_SANDBOX_IMAGE is required when Coding is enabled"
            )
        from base_agent.sandbox.docker import DockerSandboxConfig

        coding = docker_coding_bundle(
            DockerSandboxConfig(image=resolved.sandbox_image)
        )
        registered_tools.extend(coding.tools)
        enabled_tool_names.extend(coding.tool_names)
        enabled_permissions.update(coding.required_permissions)
        resources.extend(coding.resources)

    if resolved.enable_web_search:
        if resolved.brave_search_api_key is None:
            raise ValueError(
                "BRAVE_SEARCH_API_KEY is required when Web Search is enabled"
            )
        search = web_search_bundle(
            BraveWebSearchProvider(resolved.brave_search_api_key)
        )
        registered_tools.extend(search.tools)
        enabled_tool_names.extend(search.tool_names)
        enabled_permissions.update(search.required_permissions)

    if resolved.enable_mtbi:
        data = data_source_bundle(
            MtbiCliDataSource(
                executable=resolved.mtbi_cli_executable,
                engine=resolved.mtbi_engine,
                region=resolved.mtbi_region,
                timeout_seconds=resolved.cli_timeout_seconds,
                max_output_bytes=resolved.cli_max_output_bytes,
            ),
            timeout_seconds=resolved.cli_timeout_seconds + 5,
        )
        registered_tools.extend(data.tools)
        enabled_tool_names.extend(data.tool_names)
        enabled_permissions.update(data.required_permissions)

    extended_execution = (
        resolved.enable_coding
        or resolved.enable_web_search
        or resolved.enable_mtbi
    )
    return Agent(
        definition=AgentDefinition(
            id="starter-agent",
            version="1.0.0",
            instructions=(
                "Answer clearly. Use declared Tools when required and follow selected Skills."
            ),
            model=resolved.model,
            tools=tuple(enabled_tool_names),
            skills=("text-analysis",),
            permissions=frozenset(enabled_permissions),
            max_steps=20 if extended_execution else 8,
            max_tool_calls=50 if extended_execution else 8,
        ),
        model=build_provider(resolved),
        tools=registered_tools,
        skill_registry=registry,
        runtime=AgentRuntime(strategy=ReActStrategy()) if react else None,
        resources=resources,
    )
