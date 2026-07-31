"""Concrete composition helpers for isolated code execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from base_agent.resources import ResourceSpec
from base_agent.sandbox.tools import sandbox_tools
from base_agent.tools import FunctionTool

if TYPE_CHECKING:
    from base_agent.sandbox.docker import DockerSandboxConfig


@dataclass(frozen=True, slots=True)
class CodingBundle:
    """Tools and Resources required for one explicitly enabled coding workspace."""

    tools: tuple[FunctionTool, ...]
    resources: tuple[ResourceSpec, ...]
    tool_names: tuple[str, ...]
    required_permissions: frozenset[str]


def coding_bundle(
    resource: ResourceSpec,
    *,
    allow_read: bool = True,
    allow_write: bool = True,
    allow_execute: bool = True,
    execute_timeout_seconds: float = 65.0,
) -> CodingBundle:
    """Compose selected Sandbox tools around one application-supplied Resource."""
    selected_names = {
        name
        for name, enabled in (
            ("sandbox_read_text", allow_read),
            ("sandbox_write_text", allow_write),
            ("sandbox_execute", allow_execute),
        )
        if enabled
    }
    if not selected_names:
        raise ValueError("coding bundle must enable at least one action")

    selected_tools = tuple(
        candidate
        for candidate in sandbox_tools(
            resource_name=resource.name,
            execute_timeout_seconds=execute_timeout_seconds,
        )
        if candidate.definition.name in selected_names
    )
    return CodingBundle(
        tools=selected_tools,
        resources=(resource,),
        tool_names=tuple(candidate.definition.name for candidate in selected_tools),
        required_permissions=frozenset().union(
            *(candidate.permissions for candidate in selected_tools)
        ),
    )


def docker_coding_bundle(
    config: DockerSandboxConfig,
    *,
    resource_name: str = "coding-sandbox",
    eager: bool = False,
    allow_read: bool = True,
    allow_write: bool = True,
    allow_execute: bool = True,
    execute_timeout_seconds: float | None = None,
) -> CodingBundle:
    """Compose a disposable Docker-backed CodingBundle without importing Docker at package load."""
    from base_agent.sandbox.docker import docker_sandbox_resource

    resource = docker_sandbox_resource(
        config,
        name=resource_name,
        eager=eager,
    )
    return coding_bundle(
        resource,
        allow_read=allow_read,
        allow_write=allow_write,
        allow_execute=allow_execute,
        execute_timeout_seconds=(
            config.command_timeout_seconds + 5
            if execute_timeout_seconds is None
            else execute_timeout_seconds
        ),
    )
