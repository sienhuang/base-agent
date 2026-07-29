from pathlib import Path
from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    InMemoryArtifactStore,
    ModelResponse,
    ToolCall,
    ToolExecutor,
    ToolRegistry,
    ToolResultStatus,
    basic_tools,
)
from base_agent.testing import FakeModel
from base_agent.toolkits import interaction_tools, utility_tools, workspace_tools


@pytest.mark.asyncio
async def test_interaction_tool_uses_runtime_waiting_contract() -> None:
    tools = interaction_tools()
    result = await ToolExecutor(ToolRegistry(tools)).execute(
        ToolCall(
            id="ask-1",
            name="ask_user",
            arguments={"question": "Which region?", "metadata": {"kind": "region"}},
        ),
        granted_permissions=frozenset({"interaction:ask"}),
    )

    assert result.status is ToolResultStatus.WAITING
    assert result.message == "Which region?"
    assert result.data == {
        "prompt": "Which region?",
        "metadata": {"kind": "region"},
    }


@pytest.mark.asyncio
async def test_utility_tools_calculate_without_eval_or_code_execution() -> None:
    tools = utility_tools()
    executor = ToolExecutor(ToolRegistry(tools))

    successful = await executor.execute(
        ToolCall(id="math-1", name="calculate", arguments={"expression": "(2 + 3) * 4"})
    )
    rejected = await executor.execute(
        ToolCall(
            id="math-2",
            name="calculate",
            arguments={"expression": "__import__('os').getcwd()"},
        )
    )

    assert successful.status is ToolResultStatus.SUCCESS
    assert successful.data == {"expression": "(2 + 3) * 4", "result": 20}
    assert rejected.status is ToolResultStatus.ERROR
    assert rejected.message == "expression contains unsupported syntax"


@pytest.mark.asyncio
async def test_workspace_tools_are_bounded_and_root_confined(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nbeta alpha\n", encoding="utf-8")
    tools = workspace_tools(tmp_path)
    executor = ToolExecutor(ToolRegistry(tools))
    read_permissions = frozenset({"workspace:read"})

    listing = await executor.execute(
        ToolCall(id="list-1", name="workspace_list", arguments={}),
        granted_permissions=read_permissions,
    )
    search = await executor.execute(
        ToolCall(
            id="search-1",
            name="workspace_search_text",
            arguments={"query": "alpha", "file_pattern": "*.txt"},
        ),
        granted_permissions=read_permissions,
    )
    escaped = await executor.execute(
        ToolCall(
            id="read-1",
            name="workspace_read_text",
            arguments={"path": "../outside.txt"},
        ),
        granted_permissions=read_permissions,
    )
    denied_write = await executor.execute(
        ToolCall(
            id="write-1",
            name="workspace_write_text",
            arguments={"path": "created.txt", "content": "created"},
        ),
        granted_permissions=read_permissions,
    )
    written = await executor.execute(
        ToolCall(
            id="write-2",
            name="workspace_write_text",
            arguments={"path": "created.txt", "content": "created"},
        ),
        granted_permissions=frozenset({"workspace:write"}),
    )

    assert listing.status is ToolResultStatus.SUCCESS
    assert listing.data["entries"][0]["path"] == "notes.txt"
    assert search.status is ToolResultStatus.SUCCESS
    assert [match["line"] for match in search.data["matches"]] == [1, 2]
    assert escaped.status is ToolResultStatus.ERROR
    assert "configured workspace root" in (escaped.message or "")
    assert denied_write.status is ToolResultStatus.DENIED
    assert written.status is ToolResultStatus.SUCCESS
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created"


@pytest.mark.asyncio
async def test_artifact_tool_creates_run_owned_output() -> None:
    artifact_store = InMemoryArtifactStore()
    tools = basic_tools(include_interaction=False, include_memory=False)
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="artifact-1",
                        name="create_text_artifact",
                        arguments={
                            "name": "answer.txt",
                            "content": "forty-two",
                        },
                    ),
                )
            ),
            ModelResponse(content="Artifact created."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="toolkit-artifact",
            instructions="Create an Artifact.",
            tools=("create_text_artifact",),
            permissions=frozenset({"artifact:write"}),
        ),
        model=model,
        tools=tools,
        artifact_store=artifact_store,
    )

    result = await agent.run("create answer")

    assert result.status is AgentResultStatus.COMPLETED
    assert len(result.artifacts) == 1
    assert result.artifacts[0].name == "answer.txt"
    assert await agent.read_content(result.artifacts[0].id) == b"forty-two"
    assert UUID(str(result.metadata["run_id"])) == result.artifacts[0].run_id


def test_basic_toolkit_has_unique_names_and_workspace_is_opt_in(tmp_path: Path) -> None:
    without_workspace = basic_tools()
    with_workspace = basic_tools(workspace_root=tmp_path)

    without_names = tuple(tool.definition.name for tool in without_workspace)
    with_names = tuple(tool.definition.name for tool in with_workspace)

    assert len(with_names) == len(set(with_names))
    assert "workspace_read_text" not in without_names
    assert "workspace_read_text" in with_names
    assert "ask_user" in without_names
    assert "search_memory" in without_names
