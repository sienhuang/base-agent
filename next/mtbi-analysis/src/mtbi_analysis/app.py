"""Application composition root."""

from __future__ import annotations

from base_agent import LocalArtifactStore, ResourceSpec
from base_agent.providers import ModelProvider

from mtbi_analysis.config import Settings
from mtbi_analysis.definitions import build_agents
from mtbi_analysis.flow import MTBIAnalysisFlow, MTBIAnalysisResult
from mtbi_analysis.provider import build_provider
from mtbi_analysis.runner import MTBICommandRunner, SubprocessMTBICommandRunner
from mtbi_analysis.tools import build_mtbi_toolkit, check_mtbi_environment
from mtbi_analysis.workspace import ArtifactWorkspace


class MTBIAnalysisApplication:
    def __init__(
        self,
        flow: MTBIAnalysisFlow,
        *,
        workspace: ArtifactWorkspace,
        runner: MTBICommandRunner,
    ) -> None:
        self.flow = flow
        self.workspace = workspace
        self.runner = runner

    async def run(self, prompt: str) -> MTBIAnalysisResult:
        await check_mtbi_environment(self.workspace, self.runner)
        return await self.flow.run(prompt)


def build_application(
    settings: Settings | None = None,
    *,
    provider: ModelProvider | None = None,
    runner: MTBICommandRunner | None = None,
) -> MTBIAnalysisApplication:
    resolved = settings or Settings.from_env()
    workspace = ArtifactWorkspace(
        resolved.workspace,
        max_artifact_bytes=resolved.max_artifact_bytes,
        max_csv_rows=resolved.max_csv_rows,
    )
    resolved_runner = runner or SubprocessMTBICommandRunner(
        resolved.mtbi_cli_bin,
        timeout_seconds=resolved.mtbi_cli_timeout_seconds,
    )
    toolkit = build_mtbi_toolkit(workspace, resolved_runner)
    artifact_store = LocalArtifactStore(workspace.root / ".artifacts")
    query_agent, analysis_agent = build_agents(
        provider or build_provider(resolved),
        toolkit,
        artifact_store=artifact_store,
        max_tool_observation_bytes=resolved.max_tool_observation_bytes,
        max_run_observation_bytes=resolved.max_run_observation_bytes,
        resources=(
            ResourceSpec(
                name="workspace",
                factory=lambda _context: workspace,
                eager=True,
            ),
        ),
    )
    return MTBIAnalysisApplication(
        MTBIAnalysisFlow(
            query_agent=query_agent,
            analysis_agent=analysis_agent,
            workspace=workspace,
        ),
        workspace=workspace,
        runner=resolved_runner,
    )
