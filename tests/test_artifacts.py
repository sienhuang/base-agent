import json
from uuid import UUID, uuid4

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    Artifact,
    ArtifactStore,
    EventType,
    InMemoryArtifactStore,
    Message,
    ModelRequest,
    ModelResponse,
    OpenAIChatProvider,
    RuntimeCheckpoint,
    ToolCall,
    ToolContext,
    UnsupportedAttachmentError,
    WaitForInput,
    tool,
)
from base_agent.artifacts import ArtifactAccessError, ArtifactManager
from base_agent.runtime import AgentRuntime
from base_agent.stores import InMemoryEventStore
from base_agent.testing import FakeModel


@pytest.mark.asyncio
async def test_in_memory_artifact_store_builds_stable_content_references() -> None:
    store = InMemoryArtifactStore()
    attachment = await store.add_attachment(
        name="input.csv",
        media_type="text/csv",
        content=b"id,value\n1,alpha\n",
        metadata={"source": "upload"},
    )
    run_id = uuid4()
    artifact = await store.create_artifact(
        run_id,
        name="report.json",
        media_type="application/json",
        content=b'{"count":1}',
    )

    assert isinstance(store, ArtifactStore)
    assert attachment.size_bytes == 17
    assert len(attachment.checksum_sha256) == 64
    assert await store.read(attachment.id) == b"id,value\n1,alpha\n"
    assert await store.read(artifact.id) == b'{"count":1}'
    assert await store.list_artifacts(run_id) == (artifact,)
    assert await store.list_artifacts(uuid4()) == ()


@tool
async def inspect_attachment(attachment_id: UUID, context: ToolContext) -> Artifact:
    """Read an attachment and create a small report."""
    content = await context.artifacts.read_attachment(attachment_id)
    return await context.artifacts.create(
        name="inspection.txt",
        media_type="text/plain",
        content=f"bytes={len(content)}".encode(),
        metadata={"attachment_id": str(attachment_id)},
    )


@pytest.mark.asyncio
async def test_attachment_and_artifact_flow_through_model_tool_run_and_events() -> None:
    artifact_store = InMemoryArtifactStore()
    secret = b"binary-secret-that-must-not-enter-events"
    attachment = await artifact_store.add_attachment(
        name="payload.bin",
        media_type="application/octet-stream",
        content=secret,
    )
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="inspect_attachment",
                        arguments={"attachment_id": str(attachment.id)},
                    ),
                )
            ),
            ModelResponse(content="Inspection complete."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="artifacts",
            instructions="Inspect the attachment.",
            tools=("inspect_attachment",),
        ),
        model=model,
        tools=(inspect_attachment,),
        artifact_store=artifact_store,
    )

    result = await agent.run("inspect", attachments=(attachment,))
    run_id = UUID(str(result.metadata["run_id"]))
    run = await agent.get_run(run_id)
    events = await agent.events(run_id)

    assert result.status is AgentResultStatus.COMPLETED
    assert result.attachments == (attachment,)
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.name == "inspection.txt"
    assert await agent.read_content(artifact.id) == f"bytes={len(secret)}".encode()
    assert run.attachments == result.attachments
    assert run.artifacts == result.artifacts
    assert await agent.list_artifacts(run_id) == result.artifacts
    assert model.requests[0].attachments == (attachment,)
    assert model.requests[1].attachments == (attachment,)
    assert EventType.ATTACHMENT_ADDED in [event.type for event in events]
    assert EventType.ARTIFACT_CREATED in [event.type for event in events]

    serialized_history = json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    serialized_requests = "".join(request.model_dump_json() for request in model.requests)
    assert secret.decode() not in serialized_history
    assert secret.decode() not in serialized_requests
    assert secret.decode() not in (model.requests[1].messages[-1].content or "")


@tool
async def request_input(question: str) -> WaitForInput:
    """Ask for a value."""
    return WaitForInput(prompt=question)


@pytest.mark.asyncio
async def test_waiting_checkpoint_preserves_attachment_and_artifact_references() -> None:
    artifact_store = InMemoryArtifactStore()
    attachment = await artifact_store.add_attachment(
        name="input.txt", media_type="text/plain", content=b"checkpoint-content"
    )
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="inspect_attachment",
                        arguments={"attachment_id": str(attachment.id)},
                    ),
                    ToolCall(
                        id="call-2",
                        name="request_input",
                        arguments={"question": "Continue?"},
                    ),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="artifact-wait",
            instructions="Create then ask.",
            tools=("inspect_attachment", "request_input"),
        ),
        model=model,
        tools=(inspect_attachment, request_input),
        artifact_store=artifact_store,
    )

    waiting = await agent.run("work", attachments=(attachment,))
    run_id = UUID(str(waiting.metadata["run_id"]))
    checkpoint = await agent.checkpoint_store.load(run_id)
    restored = RuntimeCheckpoint.model_validate_json(checkpoint.model_dump_json())

    assert waiting.status is AgentResultStatus.WAITING
    assert restored.attachments == (attachment,)
    assert restored.artifacts == waiting.artifacts
    assert await agent.read_content(restored.artifacts[0].id) == b"bytes=18"
    assert "checkpoint-content" not in checkpoint.model_dump_json()

    completed = await agent.resume(run_id, "yes")
    assert completed.status is AgentResultStatus.COMPLETED
    assert completed.attachments == (attachment,)
    assert completed.artifacts == waiting.artifacts


@pytest.mark.asyncio
async def test_artifact_manager_blocks_cross_run_content_access() -> None:
    store = InMemoryArtifactStore()
    foreign = await store.create_artifact(
        uuid4(), name="foreign.txt", media_type="text/plain", content=b"private"
    )
    runtime = AgentRuntime()
    context = runtime.create_context(
        AgentProfile(id="access", instructions="Work."), "work"
    )
    manager = ArtifactManager(
        context=context,
        store=store,
        event_store=InMemoryEventStore(),
    )

    with pytest.raises(ArtifactAccessError, match="another Run"):
        await manager.read_artifact(foreign.id)


@pytest.mark.asyncio
async def test_openai_chat_adapter_rejects_unmapped_attachments_explicitly() -> None:
    class UnusedClient:
        pass

    store = InMemoryArtifactStore()
    attachment = await store.add_attachment(
        name="input.txt", media_type="text/plain", content=b"input"
    )
    provider = OpenAIChatProvider(model="test", client=UnusedClient())  # type: ignore[arg-type]

    with pytest.raises(UnsupportedAttachmentError, match="does not map attachments"):
        await provider.complete(
            ModelRequest(
                messages=(Message.user("inspect"),),
                attachments=(attachment,),
            )
        )
