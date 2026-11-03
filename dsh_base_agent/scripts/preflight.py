"""Initialize and close the pinned real DSH Runtime without a model request."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from dsh_base_agent import Agent, AgentDefinition, BaseAgentApp, LocalState, tool
from dsh_base_agent._dsh import (
    GeneratedSdkProfilePatches,
    SdkRuntimeConfig,
    SdkRuntimeFactory,
)


@tool
async def preflight_echo(value: str) -> dict[str, str]:
    """Return one value during manual MCP verification."""

    return {"value": value}


async def main() -> None:
    with TemporaryDirectory(prefix="dsh-base-agent-preflight-") as root:
        app = BaseAgentApp(
            state=LocalState(root),
            runtime_factory=SdkRuntimeFactory(
                SdkRuntimeConfig(workspace=Path.cwd()),
                patch_resolver=GeneratedSdkProfilePatches(),
            ),
        )
        agent = Agent(
            AgentDefinition(
                id="preflight",
                version="1.0.0",
                instructions="Initialization preflight only.",
                tools=("preflight_echo",),
            ),
            app=app,
            tools=(preflight_echo,),
        )
        try:
            await agent.prepare()
            print("real_dsh_preflight=ready")
        finally:
            await app.close()


if __name__ == "__main__":
    asyncio.run(main())
